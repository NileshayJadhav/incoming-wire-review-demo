"""Offline SharePoint staging checks. All HTTP and token values are fictional."""
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

import pytest

from reusable_lab.cloud import sharepoint_adapter
from reusable_lab.cloud.sharepoint_adapter import (
    GRAPH, GraphReader, HTTPResult, StagingError, _canonical_folder,
    _public_dns, _safe_name, _verify_file, stage_sharepoint_folder,
)

HOST = "fictional.sharepoint.com"
PDF = b"%PDF-1.4\nfictional offline test\n%%EOF"


def result(value, status=200, headers=None):
    return HTTPResult(status, headers or {}, json.dumps(value).encode() if isinstance(value, dict) else value)


class FakeGraph:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, headers, limit):
        self.calls.append((url, dict(headers), limit))
        response = self.responses[url]
        return response() if callable(response) else response


def file_item(name="1. Fictional - Incoming.pdf", identity="file1", content=PDF):
    return {"name": name, "id": identity, "file": {}, "size": len(content), "eTag": "fictional-etag"}


def routes(items=None):
    items = items if items is not None else [file_item()]
    urls = {
        GRAPH + "/drives/drive1/items/root1": result({"id": "root1", "folder": {}}),
        GRAPH + "/drives/drive1/items/root1/children?$top=200": result({"value": items}),
    }
    for item in items:
        if item.get("file") is not None:
            urls[GRAPH + "/drives/drive1/items/" + item["id"] + "/content"] = result(PDF)
            urls[GRAPH + "/drives/drive1/items/" + item["id"] + "?$select=id,size,eTag"] = result({"id": item["id"], "size": item["size"], "eTag": item.get("eTag")})
    return urls


def stage(tmp_path, monkeypatch, responses):
    monkeypatch.setenv("IWT_GRAPH_TOKEN", "fictional-offline-token")
    fake = FakeGraph(responses)
    manifest = stage_sharepoint_folder(destination_root=tmp_path, allowed_tenant_hosts=[HOST],
                                        drive_id="drive1", folder_id="root1", transport=fake)
    return manifest, fake


def test_complete_snapshot_hashes_and_never_saves_token(tmp_path, monkeypatch):
    manifest, fake = stage(tmp_path, monkeypatch, routes())
    assert manifest["status"] == "COMPLETE"
    assert manifest["files"][0]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert Path(manifest["files"][0]["local_path"]).read_bytes() == PDF
    saved = Path(manifest["manifest_path"]).read_text()
    assert "fictional-offline-token" not in saved and "Authorization" not in saved
    assert manifest["permissions_changed"] is False
    assert all(url.startswith(GRAPH) for url, _, _ in fake.calls)


def test_recursive_pagination(tmp_path, monkeypatch):
    child = {"id": "folder2", "name": "Statements", "folder": {}}
    data = routes([child])
    data[GRAPH + "/drives/drive1/items/root1/children?$top=200"] = result({"value": [child], "@odata.nextLink": GRAPH + "/drives/drive1/items/root1/children?$skiptoken=fictional"})
    data[GRAPH + "/drives/drive1/items/root1/children?$skiptoken=fictional"] = result({"value": []})
    data[GRAPH + "/drives/drive1/items/folder2/children?$top=200"] = result({"value": [file_item()]})
    data.update({k: v for k, v in routes().items() if "/file1" in k})
    manifest, _ = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "COMPLETE"
    assert manifest["files"][0]["relative_path"].startswith("Statements/")


@pytest.mark.parametrize("next_url", ["http://graph.microsoft.com/v1.0/evil", "https://127.0.0.1/v1.0/evil", "https://evil.example/v1.0/evil", "https://graph.microsoft.com@evil.example/v1.0/evil", "https://graph.microsoft.com:8443/v1.0/evil", "https://graph.microsoft.com/beta/evil"])
def test_pagination_cannot_send_token_elsewhere(tmp_path, monkeypatch, next_url):
    data = routes()
    data[GRAPH + "/drives/drive1/items/root1/children?$top=200"] = result({"value": [], "@odata.nextLink": next_url})
    manifest, fake = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "INCOMPLETE" and not manifest["files"]
    assert next_url not in [x[0] for x in fake.calls]


def test_download_redirect_uses_no_bearer_token(tmp_path, monkeypatch):
    data = routes()
    download = "https://" + HOST + "/download/fictional?temporary=not-a-real-secret"
    data[GRAPH + "/drives/drive1/items/file1/content"] = result(b"", 302, {"location": download})
    data[download] = result(PDF)
    manifest, fake = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "COMPLETE"
    assert [headers for url, headers, _ in fake.calls if url == download] == [{}]
    assert "temporary=" not in Path(manifest["manifest_path"]).read_text()


def test_redirect_outside_tenant_is_blocked(tmp_path, monkeypatch):
    data = routes()
    data[GRAPH + "/drives/drive1/items/file1/content"] = result(b"", 302, {"location": "https://evil.example/leak"})
    manifest, fake = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "INCOMPLETE"
    assert all("evil.example" not in url for url, _, _ in fake.calls)


@pytest.mark.parametrize("name", ["../escape.pdf", "a\\escape.pdf", "NUL.pdf", "COM1.xlsx", "a:stream.pdf", "trailing .", "%2e%2e.pdf", "..", "bad\x00.pdf"])
def test_unsafe_windows_names_are_rejected(name):
    with pytest.raises(StagingError):
        _safe_name(name)


def test_case_collision_fails_before_download(tmp_path, monkeypatch):
    manifest, fake = stage(tmp_path, monkeypatch, routes([file_item("A.pdf"), file_item("a.pdf", "file2")]))
    assert manifest["status"] == "INCOMPLETE"
    assert not any(url.endswith("/content") for url, _, _ in fake.calls)


def test_partial_download_is_manifested_never_complete(tmp_path, monkeypatch):
    data = routes([file_item("A.pdf"), file_item("B.pdf", "file2")])
    data[GRAPH + "/drives/drive1/items/file2/content"] = result(b"", 403)
    manifest, _ = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "INCOMPLETE" and len(manifest["files"]) == 1
    assert "HTTP 403" in manifest["errors"][0]


def test_changed_etag_rejects_snapshot(tmp_path, monkeypatch):
    data = routes()
    data[GRAPH + "/drives/drive1/items/file1?$select=id,size,eTag"] = result({"id": "file1", "size": len(PDF), "eTag": "changed"})
    manifest, _ = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "INCOMPLETE" and not manifest["files"]
    assert "changed during" in manifest["errors"][0]


def test_missing_token_returns_clear_incomplete_manifest(tmp_path, monkeypatch):
    monkeypatch.delenv("IWT_GRAPH_TOKEN", raising=False)
    manifest = stage_sharepoint_folder(destination_root=tmp_path, allowed_tenant_hosts=[HOST], drive_id="drive1", folder_id="root1", transport=FakeGraph({}))
    assert manifest["status"] == "INCOMPLETE"
    assert "existing delegated" in manifest["errors"][0]


def test_canonical_direct_and_library_view_urls():
    base = "https://" + HOST
    expected = (HOST, "/sites/Audit", "/sites/Audit/Shared Documents/Wires")
    assert _canonical_folder(base + "/sites/Audit/Shared%20Documents/Wires", [HOST]) == expected
    assert _canonical_folder(base + "/:f:/r/sites/Audit/Shared%20Documents/Wires?csf=1", [HOST]) == expected
    assert _canonical_folder(base + "/sites/Audit/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FAudit%2FShared%20Documents%2FWires", [HOST]) == expected


def test_opaque_share_link_never_redeemed():
    with pytest.raises(StagingError, match="write scopes"):
        _canonical_folder("https://" + HOST + "/:f:/s/Audit/OpaqueSharingToken", [HOST])


def test_canonical_path_cannot_escape_site_or_host():
    with pytest.raises(StagingError):
        _canonical_folder("https://evil.example/sites/Audit/Docs", [HOST])
    with pytest.raises(StagingError):
        _canonical_folder("https://" + HOST + "/sites/Audit/Docs/%2e%2e", [HOST])


def test_canonical_url_resolves_site_library_and_folder():
    data = {
        GRAPH + "/sites/" + HOST + ":/sites/Audit": result({"id": "site1"}),
        GRAPH + "/sites/site1/drives": result({"value": [{"id": "drive1", "webUrl": "https://" + HOST + "/sites/Audit/Shared%20Documents"}]}),
        GRAPH + "/drives/drive1/root:/Wires": result({"id": "root1", "folder": {}}),
    }
    reader = GraphReader("fictional", [HOST], FakeGraph(data))
    assert reader.resolve(folder_url="https://" + HOST + "/sites/Audit/Shared%20Documents/Wires") == ("drive1", {"id": "root1", "folder": {}})


def test_repeated_page_is_bounded(tmp_path, monkeypatch):
    data = routes()
    url = GRAPH + "/drives/drive1/items/root1/children?$top=200"
    data[url] = result({"value": [], "@odata.nextLink": url})
    manifest, fake = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "INCOMPLETE" and len(fake.calls) == 2


def test_remote_shortcuts_fail_closed(tmp_path, monkeypatch):
    item = {**file_item(), "remoteItem": {"id": "other-library"}}
    manifest, _ = stage(tmp_path, monkeypatch, routes([item]))
    assert manifest["status"] == "INCOMPLETE"


def test_bad_pdf_header_rejected(tmp_path, monkeypatch):
    data = routes()
    data[GRAPH + "/drives/drive1/items/file1/content"] = result(b"x" * len(PDF))
    manifest, _ = stage(tmp_path, monkeypatch, data)
    assert manifest["status"] == "INCOMPLETE" and not manifest["files"]


def xlsx(macro=False, traversal=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "fictional")
        archive.writestr("xl/workbook.xml", "fictional")
        if macro:
            archive.writestr("xl/vbaProject.bin", "fictional")
        if traversal:
            archive.writestr("../escape", "fictional")
    return output.getvalue()


def test_excel_container_validation():
    _verify_file(Path("Fictional.xlsx"), xlsx())
    for content in (b"not excel", xlsx(macro=True), xlsx(traversal=True)):
        with pytest.raises(StagingError):
            _verify_file(Path("Fictional.xlsx"), content)


def test_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(StagingError, match="non-public"):
        _public_dns(HOST)


def test_public_dns_passes(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("20.190.128.1", 443))])
    _public_dns(HOST)


def test_permission_denial_is_sanitized(tmp_path, monkeypatch):
    data = routes()
    data[GRAPH + "/drives/drive1/items/root1"] = result({"error": {"message": "pretend-private-token"}}, 403)
    manifest, _ = stage(tmp_path, monkeypatch, data)
    assert "HTTP 403" in manifest["errors"][0]
    assert "pretend-private-token" not in json.dumps(manifest)


@pytest.mark.parametrize("case", ["valid", "original", "wrong_identity", "formula", "fee_string", "unknown_mark", "comment_formula", "late_invalid_row"])
def test_office_script_writer_guards_before_mutation(case):
    """Exercise script logic with fake workbook objects, not a tenant deployment.

    Only this script's known type declarations are erased for Node execution.
    Actual Office Scripts registration/type checking remains tenant-owned.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the offline Office Script logic test")
    script = (Path(sharepoint_adapter.__file__).parent / "WriteIncomingWireDraft.ts").read_text(encoding="utf-8")
    script = re.sub(r"interface (?:DraftSample|DraftPayload) \{.*?\n\}", "", script, flags=re.S)
    script = script.replace("workbook: ExcelScript.Workbook, expectedDraftName: string, draftPayloadJson: string): string", "workbook, expectedDraftName, draftPayloadJson)")
    script = script.replace(" as DraftPayload", "").replace("text: string", "text").replace("left: string, right: string", "left, right")
    script = script.replace("new Set<number>()", "new Set()").replace("new Set<string>()", "new Set()")
    harness = r'''
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
eval(input.script + '\nglobalThis.writeDraft = main;');
const headers = ['#','Date/ Time','Customer','Account','Amount','Fee','Fee Charged','Domestic/Foreign','Travel','OFAC Review','Transaction Posted Correctly to Acct.','Comments'];
const contexts = {6: ['1','2026-01-01','FICTIONAL MEMBER','00000001'],7: ['2','2026-01-01','FICTIONAL MEMBER TWO','00000002']};
const writes = [];
const sheet = {
 getUsedRange: () => ({getRowIndex:()=>0,getRowCount:()=>30}),
 getRangeByIndexes: (r,c,h,w) => ({
  getValues: () => [r === 5 ? headers : contexts[r]],
  getFormulas: () => [Array(7).fill(input.case === 'formula' ? '=1+1' : '')],
  setValues: values => writes.push({r,c,h,w,values})
 })
};
const workbook = {getName:()=>input.case === 'original' ? 'TestSheet.xlsx' : 'DRAFT_fictional.xlsx',getWorksheet:()=>sheet};
const sample = {row:7,sampleId:'1',customer:'FICTIONAL MEMBER',account:'00000001',fee:15,feeCharged:'X',classification:'Domestic',travel:'X',ofac:'X',posting:'OPEN',comments:'DRAFT. Fictional evidence gap.'};
if(input.case==='wrong_identity') sample.customer='OTHER MEMBER';
if(input.case==='fee_string') sample.fee='$15.00';
if(input.case==='unknown_mark') sample.ofac='PASS';
if(input.case==='comment_formula') sample.comments='=HYPERLINK("https://evil.example")';
const payload={sheetName:'Incoming Wires',headerRow:6,samples:[sample]};
if(input.case==='late_invalid_row') payload.samples.push({...sample,row:8,sampleId:'2',customer:'WRONG',account:'00000002'});
let output, error;
try {output=JSON.parse(writeDraft(workbook,'DRAFT_fictional.xlsx',JSON.stringify(payload)));} catch(e){error=e.message;}
process.stdout.write(JSON.stringify({output,error,writes}));
'''
    completed = subprocess.run([node, "-e", harness], input=json.dumps({"script": script, "case": case}), text=True, capture_output=True, check=True)
    observed = json.loads(completed.stdout)
    if case == "valid":
        assert observed["output"]["status"] == "DRAFT"
        assert len(observed["writes"]) == 1 and observed["writes"][0]["c"] == 5
        assert observed["writes"][0]["w"] == 7
    else:
        assert observed["error"] and not observed["writes"]
