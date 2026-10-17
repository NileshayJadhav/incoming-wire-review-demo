"""Read-only, bounded Microsoft Graph staging for the local review engine.

Authentication is supplied by the user through an existing delegated token.
This module neither signs in, requests consent, redeems sharing links, nor
writes to Microsoft 365. Opaque sharing links are deliberately unsupported:
the Graph Shares API currently documents write permissions even for GET.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import uuid
import zipfile

GRAPH = "https://graph.microsoft.com/v1.0"
VERSION = "sharepoint-staging-1.0"


class StagingError(ValueError):
    """Sanitized failure, never including response bodies, tokens or URLs."""


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: dict
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_host(host):
    if not isinstance(host, str) or host != host.strip() or not re.fullmatch(r"[a-z0-9-]+\.sharepoint\.com", host):
        raise StagingError("Configure an exact commercial SharePoint tenant hostname, such as contoso.sharepoint.com.")
    return host


def _url_parts(url):
    try:
        value = urlsplit(url)
        if value.scheme != "https" or value.username or value.password or value.port not in (None, 443) or value.fragment:
            raise ValueError()
        if not value.hostname:
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise StagingError("Only canonical HTTPS URLs without credentials, fragments or custom ports are accepted.") from None


def _public_dns(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
            raise StagingError("The approved cloud hostname resolved to a non-public address.")
    except OSError:
        raise StagingError("The approved cloud hostname could not be resolved.") from None


def _http_get(url, headers, max_bytes):
    """Bounded GET; redirects and proxy inheritance are disabled intentionally."""
    _public_dns(_url_parts(url).hostname)
    request = Request(url, headers=headers, method="GET")
    opener = build_opener(_NoRedirect(), ProxyHandler({}))
    try:
        response = opener.open(request, timeout=45)
    except HTTPError as error:
        response = error
    except (URLError, OSError):
        raise StagingError("The cloud GET request failed; check network access and authentication.") from None
    with response:
        status = response.code
        result_headers = {k.lower(): v for k, v in response.headers.items()}
        if status in (301, 302, 303, 307, 308):
            return HTTPResult(status, result_headers, b"")
        length = result_headers.get("content-length")
        if length and (not length.isdigit() or int(length) > max_bytes):
            raise StagingError("A cloud response exceeded the configured byte limit.")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise StagingError("A cloud response exceeded the configured byte limit.")
        return HTTPResult(status, result_headers, body)


def _safe_name(value):
    if (not isinstance(value, str) or not value or len(value) > 200 or value in (".", "..")
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in value)
            or value[-1] in ". " or "%" in value
            or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value)):
        raise StagingError("A cloud item has a filename that is unsafe on Windows; rename it in SharePoint before staging.")
    return value


def _canonical_folder(url, hosts):
    parts = _url_parts(url)
    if parts.hostname not in hosts:
        raise StagingError("The folder hostname is outside the explicitly approved SharePoint tenant allowlist.")
    path = unquote(parts.path)
    # Copy-link URLs that contain a real /sites or /teams path can be resolved
    # without the Shares API. Other :f: links contain opaque sharing tokens.
    if path.startswith("/:f:/r/"):
        path = path[len("/:f:/r"):]
    elif path.startswith("/:"):
        raise StagingError("Opaque sharing links require the Graph Shares API, which documents write scopes. Use the folder's canonical site/library path or an existing drive ID and folder ID instead.")
    query = parse_qs(parts.query)
    if path.casefold().endswith("/forms/allitems.aspx"):
        selected = query.get("id", query.get("RootFolder", []))
        if len(selected) != 1 or not selected[0].startswith("/"):
            raise StagingError("This library-view URL needs one unambiguous folder id query parameter.")
        path = selected[0]
    chunks = path.strip("/").split("/")
    if len(chunks) < 3 or chunks[0] not in ("sites", "teams"):
        raise StagingError("Use a folder URL under /sites/site-name/library or /teams/site-name/library, or supply drive and folder IDs.")
    for chunk in chunks:
        _safe_name(chunk)
    return parts.hostname, "/" + "/".join(chunks[:2]), "/" + "/".join(chunks)


def _identity(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9!_,.\-]{1,256}", value):
        raise StagingError(f"The {label} is missing or malformed.")
    return quote(value, safe="")


class GraphReader:
    def __init__(self, token, hosts, transport=None):
        if not isinstance(token, str) or not token.strip() or "\r" in token or "\n" in token:
            raise StagingError("Provide an existing delegated Graph token through the configured environment variable; never paste it into chat.")
        self._token = token
        self.hosts = tuple(_validate_host(x) for x in hosts)
        self.transport = transport or _http_get

    def _get(self, url, max_bytes=2 * 1024 * 1024):
        parts = _url_parts(url)
        if parts.hostname != "graph.microsoft.com" or not parts.path.startswith("/v1.0/"):
            raise StagingError("A Graph continuation URL tried to leave the approved Graph endpoint.")
        response = self.transport(url, {"Authorization": "Bearer " + self._token, "Accept": "application/json"}, max_bytes)
        if len(response.body) > max_bytes:
            raise StagingError("Graph metadata exceeded the configured byte limit.")
        if response.status != 200:
            raise StagingError(f"Graph GET failed with HTTP {response.status}; no permissions were changed.")
        try:
            result = json.loads(response.body)
        except (ValueError, TypeError):
            raise StagingError("Graph returned unreadable metadata.") from None
        if not isinstance(result, dict):
            raise StagingError("Graph returned invalid metadata.")
        return result

    def pages(self, url, max_pages=500):
        seen = set()
        for _ in range(max_pages):
            if url in seen:
                raise StagingError("Graph pagination repeated a continuation URL.")
            seen.add(url)
            payload = self._get(url)
            values = payload.get("value")
            if not isinstance(values, list):
                raise StagingError("Graph returned an invalid item collection.")
            yield from values
            url = payload.get("@odata.nextLink")
            if not url:
                return
        raise StagingError("Graph pagination exceeded the configured page limit.")

    def resolve(self, folder_url=None, drive_id=None, folder_id=None):
        if folder_url and (drive_id or folder_id):
            raise StagingError("Choose a canonical folder URL or drive/folder IDs, not both.")
        if folder_url:
            host, site_path, folder_path = _canonical_folder(folder_url, self.hosts)
            site = self._get(GRAPH + "/sites/" + host + ":" + quote(site_path, safe="/"))
            site_id = _identity(site.get("id"), "site ID")
            matches = []
            for drive in self.pages(GRAPH + "/sites/" + site_id + "/drives"):
                location = _url_parts(drive.get("webUrl"))
                base = unquote(location.path).rstrip("/")
                if location.hostname == host and (folder_path == base or folder_path.startswith(base + "/")):
                    matches.append((len(base), drive, folder_path[len(base):].strip("/")))
            if not matches:
                raise StagingError("The folder did not match an accessible document library; use existing drive/folder IDs if site discovery is unavailable.")
            matches.sort(key=lambda x: x[0], reverse=True)
            _, drive, relative = matches[0]
            drive_id = drive.get("id")
            drive_part = _identity(drive_id, "drive ID")
            endpoint = GRAPH + "/drives/" + drive_part + "/root"
            if relative:
                endpoint += ":/" + quote(relative, safe="/")
            item = self._get(endpoint)
        else:
            drive_part = _identity(drive_id, "drive ID")
            item = self._get(GRAPH + "/drives/" + drive_part + "/items/" + _identity(folder_id, "folder ID"))
        if not isinstance(item.get("folder"), dict) or item.get("remoteItem"):
            raise StagingError("The selected source must be an actual accessible folder, not a file or cross-library shortcut.")
        _identity(item.get("id"), "folder ID")
        return drive_id, item

    def inventory(self, drive_id, root, max_files, max_file_bytes, max_total_bytes):
        drive_part = _identity(drive_id, "drive ID")
        files, skipped, visited, paths = [], [], set(), set()
        pending = [(root, PurePosixPath(), 0)]
        total = 0
        item_count = 0
        while pending:
            folder, parent, depth = pending.pop()
            identity = folder["id"]
            if depth > 20 or identity in visited:
                raise StagingError("The folder tree is cyclic or exceeds 20 levels.")
            visited.add(identity)
            endpoint = GRAPH + "/drives/" + drive_part + "/items/" + _identity(identity, "folder ID") + "/children?$top=200"
            for item in self.pages(endpoint):
                item_count += 1
                if item_count > max_files * 4 + 100:
                    raise StagingError("The folder tree exceeds the bounded item inventory limit.")
                if not isinstance(item, dict):
                    raise StagingError("Graph returned an invalid child item.")
                name = _safe_name(item.get("name"))
                path = parent / name
                if len(str(path)) > 500 or str(path).casefold() in paths:
                    raise StagingError("Cloud paths collide on Windows or exceed the path length limit.")
                paths.add(str(path).casefold())
                _identity(item.get("id"), "item ID")
                if item.get("remoteItem") or item.get("package"):
                    raise StagingError("Cross-library shortcuts and package items must be replaced with actual files in the selected folder.")
                if isinstance(item.get("folder"), dict):
                    pending.append((item, path, depth + 1))
                elif isinstance(item.get("file"), dict):
                    if path.suffix.casefold() not in (".pdf", ".xlsx"):
                        skipped.append({"relative_path": str(path), "reason": "Only .pdf and .xlsx are staged."})
                        continue
                    size = item.get("size")
                    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > max_file_bytes:
                        raise StagingError("A supported file has an invalid size or exceeds the per-file limit.")
                    total += size
                    if len(files) >= max_files or total > max_total_bytes:
                        raise StagingError("The selected files exceed the configured file count or total byte limit.")
                    files.append({"relative_path": str(path), "item_id": item["id"], "drive_id": drive_id,
                                  "expected_bytes": size, "etag": item.get("eTag"),
                                  "modified": item.get("lastModifiedDateTime")})
                else:
                    raise StagingError("A child has no supported file or folder facet.")
        return sorted(files, key=lambda x: x["relative_path"].casefold()), skipped

    def content(self, record, max_bytes):
        url = GRAPH + "/drives/" + _identity(record["drive_id"], "drive ID") + "/items/" + _identity(record["item_id"], "item ID") + "/content"
        headers = {"Authorization": "Bearer " + self._token}
        if record.get("etag"):
            headers["If-Match"] = str(record["etag"])
        response = self.transport(url, headers, max_bytes)
        if response.status in (301, 302, 303, 307, 308):
            # The preauthenticated download URL receives NO Graph bearer token.
            location = response.headers.get("location", response.headers.get("Location"))
            parts = _url_parts(location)
            if parts.hostname not in self.hosts:
                raise StagingError("The file download redirected outside the approved SharePoint tenant allowlist.")
            response = self.transport(location, {}, max_bytes)
        if response.status != 200:
            raise StagingError(f"File download failed with HTTP {response.status}; the batch remains incomplete.")
        if len(response.body) > max_bytes or len(response.body) != record["expected_bytes"]:
            raise StagingError("File bytes differ from the inventory size or exceed the configured limit; stage again from current source files.")
        current = self._get(GRAPH + "/drives/" + _identity(record["drive_id"], "drive ID") + "/items/" + _identity(record["item_id"], "item ID") + "?$select=id,size,eTag")
        if current.get("size") != record["expected_bytes"] or (record.get("etag") and current.get("eTag") != record["etag"]):
            raise StagingError("The SharePoint file changed during download; stage a fresh snapshot.")
        return response.body


def _verify_file(path, content):
    if path.suffix.casefold() == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise StagingError("A .pdf source did not contain a PDF header.")
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if len(members) > 5000 or sum(x.file_size for x in members) > 300 * 1024 * 1024:
                    raise StagingError("An Excel archive exceeds the bounded expanded-size limit.")
                names = set(archive.namelist())
                if not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names) or "xl/vbaproject.bin" in {x.casefold() for x in names}:
                    raise StagingError("The .xlsx source is not a supported macro-free Excel workbook.")
                if any(name.startswith(("/", "\\")) or ":" in name or ".." in PurePosixPath(name.replace("\\", "/")).parts for name in names):
                    raise StagingError("The workbook archive contains an unsafe internal path.")
        except zipfile.BadZipFile:
            raise StagingError("A .xlsx source did not contain a valid Excel ZIP container.") from None


def stage_sharepoint_folder(*, destination_root, allowed_tenant_hosts, folder_url=None,
                            drive_id=None, folder_id=None, token_env="IWT_GRAPH_TOKEN",
                            max_files=250, max_total_bytes=250 * 1024 * 1024,
                            max_file_bytes=30 * 1024 * 1024, transport=None):
    """Stage a complete snapshot or return INCOMPLETE. Never starts a review.

    ``transport`` is a test seam taking (url, headers, max_bytes). A real token
    is read only at explicit invocation, never on import and never persisted.
    Only COMPLETE manifests may be passed to the local review tool.
    """
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", token_env):
        raise StagingError("The token environment variable name is invalid.")
    if not allowed_tenant_hosts or not (1 <= max_files <= 1000) or not (0 < max_file_bytes <= 30 * 1024 * 1024) or not (max_file_bytes <= max_total_bytes <= 500 * 1024 * 1024):
        raise StagingError("Supply approved tenant hosts and bounded positive file/byte limits.")
    root = Path(destination_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / ("sharepoint_" + uuid.uuid4().hex[:16])
    staging.mkdir(exist_ok=False)
    local = staging / "files"
    local.mkdir()
    manifest = {"version": VERSION, "status": "INCOMPLETE", "created_utc": datetime.now(timezone.utc).isoformat(),
                "local_folder": str(local), "files": [], "skipped": [], "errors": [],
                "source_is_read_only": True, "permissions_changed": False}
    try:
        reader = GraphReader(os.environ.get(token_env), allowed_tenant_hosts, transport)
        resolved_drive, folder = reader.resolve(folder_url, drive_id, folder_id)
        records, manifest["skipped"] = reader.inventory(resolved_drive, folder, max_files, max_file_bytes, max_total_bytes)
        if not records:
            raise StagingError("The selected folder contains no supported PDF or XLSX source files.")
        total_bytes = 0
        for record in records:
            target = local.joinpath(*PurePosixPath(record["relative_path"]).parts)
            if not target.resolve().is_relative_to(local.resolve()):
                raise StagingError("A cloud path tried to leave the staging folder.")
            content = reader.content(record, min(max_file_bytes, max_total_bytes - total_bytes))
            _verify_file(target, content)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(content)
            total_bytes += len(content)
            manifest["files"].append({**record, "bytes": len(content), "local_path": str(target), "sha256": hashlib.sha256(content).hexdigest()})
        manifest.update(status="COMPLETE", total_bytes=total_bytes)
    except StagingError as error:
        manifest["errors"].append(str(error))
    except (OSError, ValueError, KeyError, TypeError):
        manifest["errors"].append("Staging failed while validating or saving the bounded snapshot; no review was started.")
    manifest_path = staging / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Stage an approved SharePoint folder read-only for local review.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--folder-url")
    source.add_argument("--drive-id")
    parser.add_argument("--folder-id")
    parser.add_argument("--tenant-host", action="append", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--token-env", default="IWT_GRAPH_TOKEN")
    args = parser.parse_args()
    try:
        result = stage_sharepoint_folder(destination_root=args.destination, allowed_tenant_hosts=args.tenant_host,
                                         folder_url=args.folder_url, drive_id=args.drive_id, folder_id=args.folder_id,
                                         token_env=args.token_env)
    except StagingError as error:
        print(json.dumps({"status": "INCOMPLETE", "errors": [str(error)]}))
        return 1
    print(json.dumps({"status": result["status"], "manifest_path": result["manifest_path"], "local_folder": result["local_folder"],
                      "files": len(result["files"]), "errors": result["errors"]}, indent=2))
    return 0 if result["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
