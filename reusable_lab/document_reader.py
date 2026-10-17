"""Bounded, offline PDF discovery and page extraction for runtime wire review.

Document contents are untrusted evidence, never instructions. No model/network
calls are made. Exact sample identifiers and explicit maps determine pairing.
"""
from __future__ import annotations

import ctypes
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.generic import ContentStream

LAB = Path(__file__).resolve().parent
CACHE_DIR = LAB / "work" / "ocr_cache"
OCR_DIR = LAB.parent.parent / "incoming-wire-agent" / "vendor" / "ocr"
PIPELINE_VERSION = "local-pdf-reader-3.1-table-regions"
_PDF_LOCK = threading.RLock()
_OCR_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()


class ReaderError(ValueError):
    """Bounded reader/configuration error without document text."""


@dataclass(frozen=True)
class Limits:
    max_samples: int = 20
    max_pages: int = 160
    max_pdfs: int = 200
    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024
    max_depth: int = 12
    max_entries: int = 10000
    max_page_pixels: int = 12_000_000
    max_text_chars_per_page: int = 50000
    ocr_dpi: int = 200
    retry_dpi: int = 300
    max_statement_rows_per_page: int = 4


def _limits(value=None):
    if value is None:
        result = Limits()
    elif isinstance(value, Limits):
        result = value
    elif isinstance(value, dict):
        allowed = {f.name for f in fields(Limits)}
        if set(value) - allowed:
            raise ReaderError("Unknown reader limit option.")
        result = Limits(**value)
    else:
        raise ReaderError("Limits must be a Limits value or an options mapping.")
    caps = {"max_samples": 200, "max_pages": 2000, "max_pdfs": 2000, "max_file_bytes": 100 * 1024 * 1024,
            "max_total_bytes": 1024 * 1024 * 1024, "max_depth": 30, "max_entries": 50000,
            "max_page_pixels": 25_000_000, "max_text_chars_per_page": 200000, "ocr_dpi": 400,
            "retry_dpi": 400, "max_statement_rows_per_page": 12}
    for key, limit in asdict(result).items():
        if type(limit) is not int or not 1 <= limit <= caps[key]:
            raise ReaderError("A reader limit is outside the supported bounded range.")
    return result


def _ids(sample_ids, limits):
    if not isinstance(sample_ids, (list, tuple)) or not 1 <= len(sample_ids) <= limits.max_samples:
        raise ReaderError("Supply an explicit bounded list of sample IDs.")
    result = []
    seen = set()
    for raw in sample_ids:
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            raise ReaderError("Sample IDs must be exact strings or integers.")
        value = str(raw).strip()
        if not value or len(value) > 80 or value in {".", ".."} or re.search(r"[\\/\x00-\x1f]", value):
            raise ReaderError("A sample ID is empty, path-shaped or too long.")
        if value.casefold() in seen:
            raise ReaderError("Sample IDs must be unique without case-only duplicates.")
        result.append(value)
        seen.add(value.casefold())
    return result


def _diagnostic(code, message, **extra):
    return {"code": code, "status": "OPEN", "message": message, **extra}


def _bounded_bytes(path, limit):
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ReaderError("A PDF exceeds the per-file byte limit.")
    return value


def _is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _safe_path(root, relative):
    if not isinstance(relative, str):
        raise ReaderError("An explicit document path must be relative text.")
    path = Path(relative)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ReaderError("An explicit document path must stay under the evidence root.")
    current = root
    for part in path.parts:
        current = current / part
        if _is_link(current):
            raise ReaderError("Linked or unavailable document paths are not followed.")
    resolved = current.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
        raise ReaderError("An explicit document is missing, outside the root or not a PDF.")
    if any(p.casefold() in {"outputs", ".git", "node_modules", "__pycache__"} for p in path.parts):
        raise ReaderError("Output/generated directories are excluded from source evidence.")
    return resolved


def _enumerate(root, limits):
    files, diagnostics = [], []
    count = 0
    stack = [(root, 0)]
    excluded = {"outputs", ".git", "node_modules", "__pycache__"}
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            diagnostics.append(_diagnostic("DIRECTORY_UNREADABLE", "A source subdirectory could not be enumerated.", path=directory.relative_to(root).as_posix()))
            continue
        for path in entries:
            count += 1
            if count > limits.max_entries:
                raise ReaderError("Evidence-directory entry limit exceeded; narrow the source root.")
            relative = path.relative_to(root).as_posix()
            if path.name.casefold() in excluded:
                continue
            if _is_link(path):
                diagnostics.append(_diagnostic("LINK_EXCLUDED", "A linked path was excluded.", path=relative))
                continue
            if not path.resolve().is_relative_to(root):
                diagnostics.append(_diagnostic("ROOT_ESCAPE_EXCLUDED", "A path resolving outside the source root was excluded.", path=relative))
                continue
            if path.is_dir():
                if depth >= limits.max_depth:
                    diagnostics.append(_diagnostic("DEPTH_LIMIT", "A deeper source directory was excluded.", path=relative))
                else:
                    stack.append((path, depth + 1))
            elif path.is_file() and path.suffix.casefold() == ".pdf":
                files.append(path)
                if len(files) > limits.max_pdfs:
                    raise ReaderError("PDF inventory limit exceeded; narrow the evidence root.")
    return sorted(files, key=lambda p: p.relative_to(root).as_posix().casefold()), diagnostics


def _filename_id(stem, sample_ids):
    # Longest identifier wins at a strict delimiter boundary: 1 never matches 10.
    for sid in sorted(sample_ids, key=len, reverse=True):
        boundary = r"(?=$|\s|[.](?=\s|$|incoming\b|statement\b|return\b)|[-_](?=\s|incoming\b|statement\b|return\b|send\b))"
        if re.match(r"^(?:sample[-_ ]*)?" + re.escape(sid.casefold()) + boundary, stem.casefold()):
            return sid
    return None


def _declared_unknown_id(stem):
    prefix = re.match(r"^(?:sample[-_ ]*)?(\d+)(?=[ ._-])", stem, re.I)
    if prefix:
        return prefix.group(1)
    if " - " in stem:
        candidate = stem.split(" - ", 1)[0].removeprefix("sample-").strip()
        if candidate.casefold() not in {"incoming", "statement", "return", "outgoing"}:
            return candidate
    return None


def _role(filename):
    name = re.sub(r"[_-]+", " ", Path(filename).stem).casefold()
    if re.search(r"\b(?:return(?:ed)?|send\s+back)\b", name):
        return "return"
    if re.search(r"\bstatement\b", name):
        return "statement"
    if re.search(r"\bincoming\b", name):
        return "incoming"
    return "unclassified"


def _record(path, root, role, basis):
    return {"relative_path": path.relative_to(root).as_posix(), "path": str(path),
            "role": role, "pairing_basis": basis, "bytes": path.stat().st_size}


def discover_documents(source_dir, sample_ids, documents=None, limits=None):
    """Inventory exact-ID matches; mapping is {ID: {role: relative path/list}}.

    Explicit sample entries override automatic discovery for that sample. Missing
    or ambiguous paths are diagnostic OPEN results, never a fuzzy fallback.
    """
    try:
        bounds = _limits(limits)
        ids = _ids(sample_ids, bounds)
        root = Path(source_dir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ReaderError("The evidence root is not a directory.")
        all_files, diagnostics = _enumerate(root, bounds)
        groups = {sid: {"sample_id": sid, "documents": [], "missing": [], "ambiguous": [], "diagnostics": []} for sid in ids}
        explicit = {}
        if documents is not None:
            if not isinstance(documents, dict):
                raise ReaderError("Explicit documents must be a mapping keyed by exact sample ID.")
            for key, value in documents.items():
                matching = next((sid for sid in ids if sid.casefold() == str(key).casefold()), None)
                if matching:
                    if matching in explicit:
                        raise ReaderError("An explicit sample mapping is repeated with different casing.")
                    explicit[matching] = value
        for sid, config in explicit.items():
            if isinstance(config, str): config = {"auto": [config]}
            elif isinstance(config, list): config = {"auto": config}
            if not isinstance(config, dict):
                groups[sid]["diagnostics"].append(_diagnostic("INVALID_MAPPING", "Explicit document mapping has an unsupported shape."))
                continue
            for role, paths in config.items():
                if role not in {"incoming", "statement", "return", "unclassified", "auto"}:
                    groups[sid]["diagnostics"].append(_diagnostic("UNKNOWN_ROLE", "An explicit document role is unsupported."))
                    continue
                paths = [paths] if isinstance(paths, str) else paths
                if not isinstance(paths, list):
                    groups[sid]["diagnostics"].append(_diagnostic("INVALID_MAPPING", "Document role entries must be a path or a list."))
                    continue
                for relative in paths:
                    try:
                        path = _safe_path(root, relative)
                        groups[sid]["documents"].append(_record(path, root, _role(path.name) if role == "auto" else role, "explicit_mapping"))
                    except (ReaderError, OSError) as exc:
                        groups[sid]["diagnostics"].append(_diagnostic("MAPPED_DOCUMENT_UNAVAILABLE", str(exc) if isinstance(exc, ReaderError) else "An explicit document is unavailable."))
        for path in all_files:
            relative = path.relative_to(root)
            filename_id = _filename_id(path.stem, ids)
            folder_ids = set()
            for part in relative.parts[:-1]:
                for sid in sorted(ids, key=len, reverse=True):
                    if part.casefold() in {sid.casefold(), "sample-" + sid.casefold(), "sample_" + sid.casefold(), "sample " + sid.casefold()}:
                        folder_ids.add(sid)
                        break
            declared = filename_id or _declared_unknown_id(path.stem)
            conflict = len(folder_ids) > 1 or bool(folder_ids and declared and declared.casefold() not in {s.casefold() for s in folder_ids})
            if conflict:
                for sid in folder_ids | ({filename_id} if filename_id else set()):
                    if sid not in explicit:
                        groups[sid]["ambiguous"].append(_diagnostic("CONFLICTING_SAMPLE_IDS", "Filename and folder sample identifiers conflict; explicit mapping is required.", path=relative.as_posix()))
                continue
            sid = filename_id or next(iter(folder_ids), None)
            if sid and sid not in explicit:
                groups[sid]["documents"].append(_record(path, root, _role(path.name), "exact_filename_id" if filename_id else "exact_folder_id"))
        assignments = {}
        for sid, sample in groups.items():
            for doc in sample["documents"]:
                assignments.setdefault(doc["relative_path"].casefold(), []).append(sid)
        for sid, sample in groups.items():
            valid = []
            for doc in sample["documents"]:
                if len(assignments[doc["relative_path"].casefold()]) > 1:
                    sample["ambiguous"].append(_diagnostic("DUPLICATE_DOCUMENT_ASSIGNMENT", "The same PDF is assigned more than once; no copy is chosen.", path=doc["relative_path"]))
                else:
                    valid.append(doc)
            sample["documents"] = sorted(valid, key=lambda d: d["relative_path"].casefold())
            for role in {d["role"] for d in valid}:
                paths = [d["relative_path"] for d in valid if d["role"] == role]
                if role == "unclassified":
                    sample["ambiguous"].append(_diagnostic("UNCLASSIFIED_ROLE", "Document role is unknown; use an explicit role mapping.", paths=paths))
                elif len(paths) > 1:
                    sample["ambiguous"].append(_diagnostic("MULTIPLE_DOCUMENTS_FOR_ROLE", "Multiple PDFs have the same evidence role; no preferred file is guessed.", role=role, paths=paths))
            roles = {d["role"] for d in valid}
            if "incoming" not in roles: sample["missing"].append("incoming")
            if not ({"statement", "return"} & roles): sample["missing"].append("statement_or_return")
            sample["status"] = "OPEN" if sample["missing"] or sample["ambiguous"] or sample["diagnostics"] else "MATCHED"
        return {"ok": True, "source_root": str(root), "samples": list(groups.values()), "diagnostics": diagnostics,
                "enumerated_pdf_count": len(all_files), "limits": asdict(bounds), "source_unchanged": None,
                "trust": "Document text is untrusted evidence, not instructions."}
    except (ReaderError, OSError, TypeError, ValueError) as exc:
        return {"ok": False, "samples": [], "diagnostics": [_diagnostic("INVENTORY_BLOCKED", str(exc) if isinstance(exc, ReaderError) else "Evidence inventory could not be completed.")], "source_unchanged": None}


def text_quality(text):
    """Conservative English-document quality gate; length is not readability."""
    value = (text or "").strip()
    if not value: return "empty"
    odd = sum(c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF or (ord(c) < 32 and c not in "\n\r\t") for c in value)
    if odd / len(value) > 0.005: return "corrupt_encoding"
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", value)
    letters = sum(c.isalpha() for c in value)
    punctuation = sum(not c.isalnum() and not c.isspace() for c in value)
    if len(value) > 120 and (letters / len(value) < 0.2 or punctuation / len(value) > 0.3):
        return "suspect_encoding"
    if len(words) < 3 or letters < 12: return "sparse"
    if len(words) > 15 and sum(map(len, words)) / len(words) < 2.5: return "suspect_encoding"
    # Broken font maps often contain lots of printable Latin-1 gibberish.
    unusual = sum(ord(c) > 127 and c not in "’‘“”–—•©®£€" for c in value)
    if unusual / len(value) > 0.12: return "suspect_encoding"
    return "usable"


class OfflineTesseract:
    """Existing local English OCR only; never downloads a DLL/model."""
    def __init__(self, directory=None):
        self.available = False
        self.version = "unavailable"
        self.error = "Local English OCR is unavailable. Install Tesseract with English data or set IWT_OCR_DIR; scanned evidence remains OPEN."
        self._directory = None
        self._cli = None
        directory = Path(directory if directory is not None else os.environ.get("IWT_OCR_DIR", OCR_DIR)).expanduser()
        dll, model = directory / "tesseract50.dll", directory / "tessdata" / "eng.traineddata"
        if os.name != "nt" or not dll.is_file() or not model.is_file():
            self._initialize_cli(directory)
            return
        try:
            self._directory = os.add_dll_directory(str(directory))
            self._lept = ctypes.CDLL(str(directory / "leptonica-1.82.0.dll"))
            self.lib = ctypes.CDLL(str(dll))
            lib = self.lib
            lib.TessVersion.restype = ctypes.c_char_p
            lib.TessBaseAPICreate.restype = ctypes.c_void_p
            lib.TessBaseAPIInit3.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            lib.TessBaseAPIInit3.restype = ctypes.c_int
            lib.TessBaseAPISetImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            lib.TessBaseAPIGetUTF8Text.argtypes = [ctypes.c_void_p]
            lib.TessBaseAPIGetUTF8Text.restype = ctypes.c_void_p
            lib.TessBaseAPIMeanTextConf.argtypes = [ctypes.c_void_p]
            lib.TessBaseAPIMeanTextConf.restype = ctypes.c_int
            lib.TessBaseAPIGetTsvText.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.TessBaseAPIGetTsvText.restype = ctypes.c_void_p
            lib.TessBaseAPISetPageSegMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.TessBaseAPISetVariable.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            lib.TessBaseAPISetSourceResolution.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.TessDeleteText.argtypes = [ctypes.c_void_p]
            lib.TessBaseAPIEnd.argtypes = [ctypes.c_void_p]
            lib.TessBaseAPIDelete.argtypes = [ctypes.c_void_p]
            self.model_dir = directory / "tessdata"
            self.version = lib.TessVersion().decode("ascii", "replace") + ":eng:" + hashlib.sha256(model.read_bytes()).hexdigest()
            self.available = True
            self.error = None
        except (OSError, AttributeError):
            self._initialize_cli(directory)

    def _initialize_cli(self, directory):
        """Use an existing Tesseract executable without writing page data to disk."""
        candidates = [directory / ("tesseract.exe" if os.name == "nt" else "tesseract")]
        installed = shutil.which("tesseract")
        if installed:
            candidates.append(Path(installed))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        model_dir = directory / "tessdata" if (directory / "tessdata" / "eng.traineddata").is_file() else None
        for executable in dict.fromkeys(candidates):
            if not executable.is_file() or executable.suffix.casefold() in {".bat", ".cmd", ".ps1"}:
                continue
            args = [str(executable)]
            try:
                version = subprocess.run(args + ["--version"], capture_output=True, timeout=10,
                                         check=False, creationflags=creationflags)
                langs = subprocess.run(args + ["--list-langs"] + (["--tessdata-dir", str(model_dir)] if model_dir else []),
                                       capture_output=True, timeout=10, check=False, creationflags=creationflags)
                listed = langs.stdout.decode("utf-8", "replace")
                if version.returncode != 0 or langs.returncode != 0 or "eng" not in listed.splitlines():
                    continue
                effective_dir = model_dir
                if effective_dir is None:
                    location = re.search(r'in\s+"([^"\r\n]+)"', listed)
                    if location:
                        effective_dir = Path(location[1])
                english = effective_dir / "eng.traineddata" if effective_dir is not None else None
                model_signature = hashlib.sha256(english.read_bytes()).hexdigest() if english is not None and english.is_file() else hashlib.sha256(listed.encode()).hexdigest()
                binary_stat = executable.stat()
                signature = version.stdout[:200] + str(executable.resolve()).encode() + str(binary_stat.st_mtime_ns).encode()
                self.version = "tesseract-cli:" + hashlib.sha256(signature).hexdigest() + ":eng:" + model_signature
                self._cli = (str(executable), str(model_dir) if model_dir else None, creationflags)
                self.available, self.error = True, None
                return
            except (OSError, subprocess.TimeoutExpired):
                continue

    def _recognize_cli(self, image, single_line=False, numeric=False, dpi=None):
        executable, model_dir, creationflags = self._cli
        args = [executable, "stdin", "stdout", "-l", "eng", "--psm", "7" if single_line else "3"]
        if model_dir:
            args += ["--tessdata-dir", model_dir]
        if dpi:
            args += ["--dpi", str(dpi)]
        if numeric:
            args += ["-c", "tessedit_char_whitelist=0123456789.,-+()$"]
        args += ["tsv"]
        buffer = io.BytesIO()
        rgb = image.convert("RGB")
        try:
            rgb.save(buffer, format="PNG")
            with _OCR_LOCK:
                try:
                    result = subprocess.run(args, input=buffer.getvalue(), capture_output=True, timeout=120,
                                            check=False, creationflags=creationflags)
                except (OSError, subprocess.TimeoutExpired):
                    raise ReaderError("Local Tesseract OCR failed or exceeded its time limit; verify the source page.") from None
            if result.returncode != 0:
                raise ReaderError("Local Tesseract OCR could not read the page; verify the source page.")
            words = _tsv_words(result.stdout.decode("utf-8", "replace"), rgb.width, rgb.height)
            lines = {}
            for word in words:
                lines.setdefault(tuple(word["line"]), []).append(word)
            text = "\n".join(" ".join(w["text"] for w in sorted(line, key=lambda w: w["box"][0])) for line in lines.values())
            confidence = round(sum(w["confidence"] for w in words) / len(words)) if words else 0
            return {"text": text, "confidence": confidence, "words": words, "image_size": [rgb.width, rgb.height]}
        finally:
            rgb.close()
            buffer.close()

    def recognize(self, image):
        return self._recognize(image)

    def recognize_region(self, image, numeric=False, dpi=300):
        """Single observed row/cell; numeric alphabet never repairs missing digits."""
        return self._recognize(image, single_line=True, numeric=numeric, dpi=dpi)

    def _recognize(self, image, single_line=False, numeric=False, dpi=None):
        if not self.available: raise ReaderError(self.error)
        if self._cli is not None:
            return self._recognize_cli(image, single_line=single_line, numeric=numeric, dpi=dpi)
        with _OCR_LOCK:
            lib = self.lib
            api = lib.TessBaseAPICreate()
            try:
                if lib.TessBaseAPIInit3(api, str(self.model_dir).encode(), b"eng") != 0:
                    raise ReaderError("The local English OCR model could not be initialized.")
                if single_line: lib.TessBaseAPISetPageSegMode(api, 7)
                if numeric: lib.TessBaseAPISetVariable(api, b"tessedit_char_whitelist", b"0123456789.,-+()$")
                rgb = image.convert("RGB")
                buffer = ctypes.create_string_buffer(rgb.tobytes())
                lib.TessBaseAPISetImage(api, buffer, rgb.width, rgb.height, 3, rgb.width * 3)
                if dpi: lib.TessBaseAPISetSourceResolution(api, dpi)
                pointer = lib.TessBaseAPIGetUTF8Text(api)
                try:
                    text = ctypes.string_at(pointer).decode("utf-8", "replace") if pointer else ""
                    confidence = lib.TessBaseAPIMeanTextConf(api)
                finally:
                    if pointer: lib.TessDeleteText(pointer)
                pointer = lib.TessBaseAPIGetTsvText(api, 0)
                try:
                    tsv = ctypes.string_at(pointer).decode("utf-8", "replace") if pointer else ""
                finally:
                    if pointer: lib.TessDeleteText(pointer)
                words = _tsv_words(tsv, rgb.width, rgb.height)
                return {"text": text, "confidence": confidence, "words": words,
                        "image_size": [rgb.width, rgb.height]}
            finally:
                lib.TessBaseAPIEnd(api)
                lib.TessBaseAPIDelete(api)


def _tsv_words(tsv, width, height):
    """Retain observed OCR words and raster coordinates, never inferred columns."""
    result = []
    try:
        columns = ["level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width", "height", "conf", "text"]
        # Tesseract's C API emits rows without the CLI's header line.
        names = None if tsv.startswith("level\t") else columns
        for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", fieldnames=names, quoting=csv.QUOTE_NONE):
            if row.get("level") != "5" or not (row.get("text") or "").strip(): continue
            left, top, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            confidence = float(row["conf"])
            if w <= 0 or h <= 0 or left < 0 or top < 0 or left+w > width or top+h > height: continue
            if len(result) >= 5000: break
            result.append({"text": row["text"], "confidence": confidence,
                           "box": [left, top, left+w, top+h],
                           "line": [int(row[k]) for k in ("block_num", "par_num", "line_num")]})
    except (KeyError, TypeError, ValueError):
        return []
    return result


def _statement_candidates(result, limit):
    """Find printed Amount/Balance header geometry and dated wire/fee rows."""
    groups = {}
    for word in result.get("words", []):
        groups.setdefault(tuple(word["line"]), []).append(word)
    lines = [sorted(words, key=lambda w: w["box"][0]) for words in groups.values()]
    headers = []
    for line in lines:
        amount = next((w for w in line if w["text"].strip().casefold() == "amount" and w["confidence"] >= 70), None)
        balance = next((w for w in line if w["text"].strip().casefold() == "balance" and w["confidence"] >= 70), None)
        description = next((w for w in line if w["text"].strip().casefold() == "description" and w["confidence"] >= 70), None)
        if amount and balance and description and description["box"][2] < amount["box"][0] < balance["box"][0]:
            headers.append({"words": line, "amount": amount, "balance": balance, "description": description,
                            "top": min(w["box"][1] for w in line),
                            "text": "\t".join(w["text"] for w in line)})
    candidates = []
    for line in lines:
        text = " ".join(w["text"] for w in line)
        if not re.search(r"\bwire\b", text, re.I): continue
        date = next((w for w in line if re.fullmatch(r"[\"'‘’“”]?\d{2}[-/]\d{2}[-/]\d{4}", w["text"])), None)
        prior = [h for h in headers if date and h["top"] < date["box"][1]]
        if not date or not prior: continue
        header = max(prior, key=lambda h: h["top"])
        candidates.append({"date": date, "header": header})
    return candidates[:limit], len(candidates) > limit


def _tight_cell(image, box):
    """Trim white margins and separated thin table rules; retain printed glyphs."""
    box = tuple(int(x) for x in box)
    region = image.crop(box).convert("RGB")
    mask = region.convert("L").point(lambda pixel: 255 if pixel < 180 else 0)
    # A table separator can be included in a word's oversized OCR box. Keep
    # the glyph-height ink cluster, rather than deleting rows through text.
    occupied = [y for y in range(mask.height) if mask.crop((0, y, mask.width, y+1)).getbbox()]
    if not occupied:
        region.close(); mask.close(); return None, list(box)
    clusters = []
    for y in occupied:
        if not clusters or y > clusters[-1][-1]+1: clusters.append([y])
        else: clusters[-1].append(y)
    main = max(clusters, key=len)
    # Only exclude distant thin edge separators. Separate substantive ink
    # clusters are retained, including dotted/descender glyph components.
    retained = [c for c in clusters if len(c) > 3 or min(abs(c[-1]-main[0]), abs(c[0]-main[-1])) <= 3]
    first, last = min(c[0] for c in retained), max(c[-1] for c in retained)
    bounded_mask = mask.crop((0, first, mask.width, last+1))
    ink = bounded_mask.getbbox()
    bounded_mask.close(); mask.close()
    tight = (ink[0], first+ink[1], ink[2], first+ink[3])
    cropped = region.crop(tight)
    region.close()
    padded = ImageOps.expand(cropped, border=20, fill="white")
    cropped.close()
    return padded, [box[0]+tight[0], box[1]+tight[1], box[0]+tight[2], box[1]+tight[3]]


def _statement_regions(page, data, number, bounds, ocr, result, cancel_check=None):
    candidates, truncated = _statement_candidates(result, bounds.max_statement_rows_per_page)
    if not candidates or not hasattr(ocr, "recognize_region"): return [], [], False
    retry_bounds = Limits(**{**asdict(bounds), "ocr_dpi": max(bounds.ocr_dpi, bounds.retry_dpi)})
    if cancel_check: cancel_check()
    image, image_source = _ocr_image(page, data, number, retry_bounds)
    rows, attempts = [], []
    try:
        old_width, old_height = result.get("image_size", [image.width, image.height])
        sx, sy = image.width / old_width, image.height / old_height
        for candidate in candidates:
            if cancel_check: cancel_check()
            header, date = candidate["header"], candidate["date"]
            # Header positions determine disjoint Amount and Balance cells.
            amount, balance, desc = (header[k]["box"] for k in ("amount", "balance", "description"))
            amount_center, balance_center = (amount[0]+amount[2])/2, (balance[0]+balance[2])/2
            boundary = (amount_center+balance_center)/2
            amount_left = max(desc[2], amount_center-(balance_center-amount_center)*.8)
            date_left = max(0, date["box"][0]-8)
            desc_left = desc[0]-8
            y1 = max(0, (date["box"][1]-4)*sy)
            y2 = min(image.height, (date["box"][3]+10)*sy)
            boxes = {"date": [date_left*sx,y1,(desc_left-4)*sx,y2],
                     "description": [desc_left*sx,y1,amount_left*sx,y2],
                     "amount": [amount_left*sx,y1,boundary*sx,y2],
                     "balance": [boundary*sx,y1,image.width,y2]}
            observed, confidence, coordinates = {}, {}, {}
            for name, box in boxes.items():
                if cancel_check: cancel_check()
                crop, actual_box = _tight_cell(image, box)
                coordinates[name] = actual_box
                if crop is None:
                    observed[name], confidence[name] = "", 0
                    continue
                try:
                    recognized = ocr.recognize_region(crop, numeric=name in {"amount", "balance"}, dpi=retry_bounds.ocr_dpi)
                finally: crop.close()
                observed[name] = " ".join(recognized.get("text", "").split())
                confidence[name] = recognized.get("confidence", 0)
            usable = (bool(re.fullmatch(r"\d{2}[-/]\d{2}[-/]\d{4}", observed["date"]))
                      and bool(re.search(r"\bwire\b", observed["description"], re.I))
                      and bool(re.fullmatch(r"[+-]?\$?\d[\d,]*\.\d{2}", observed["amount"]))
                      and min(confidence[k] for k in ("date", "description", "amount")) >= 70)
            attempt = {"method": "ocr_statement_row", "dpi": retry_bounds.ocr_dpi,
                       "field_confidences": confidence, "usable": usable}
            attempts.append(attempt)
            # An unresolved crop does not become source text for the model.
            if not usable: continue
            rows.append({"row_text": "\t".join(observed[k] for k in ("date", "description", "amount", "balance")),
                         "header_text": header["text"], "date_text": observed["date"],
                         "description_text": observed["description"], "amount_text": observed["amount"],
                         "balance_text": observed["balance"], "amount_column": "amount",
                         "confidence": min(confidence[k] for k in ("date", "description", "amount")),
                         "field_confidences": confidence, "column_boxes": coordinates,
                         "bbox_pixels": [int(date_left*sx),int(y1),image.width,int(y2)],
                         "image_size": list(image.size), "dpi": retry_bounds.ocr_dpi,
                         "image_source": image_source, "method": "ocr_statement_row",
                         "transaction_rows_complete": False, "covered_page_transactions": False})
        return rows, attempts, truncated
    finally: image.close()


def _matrix(a, b):
    return (a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3],
            a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5])


def _full_page_scan(page, max_pixels=12_000_000):
    """Use a single directly painted, axis-aligned full-page scan only."""
    try:
        if not hasattr(page, "cropbox") or int(page.get("/Rotate", 0)) % 360 or len(page.images) != 1: return None
        box = page.cropbox
        left, bottom, right, top = map(float, (box.left, box.bottom, box.right, box.top))
        width, height = right-left, top-bottom
        objects = page.get("/Resources").get_object().get("/XObject")
        if objects is None: return None
        objects = objects.get_object()
        operations = ContentStream(page.get_contents(), page.pdf).operations
        matrix, stack, drawings = (1., 0., 0., 1., 0., 0.), [], []
        for operands, operator in operations:
            # A full-page background plus text/vector overlays is not a pure
            # scan; render its visible page so those overlays are retained.
            if operator in {b"Tj", b"TJ", b"'", b'"', b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"}:
                return None
            if operator == b"q": stack.append(matrix)
            elif operator == b"Q": matrix = stack.pop() if stack else (1., 0., 0., 1., 0., 0.)
            elif operator == b"cm": matrix = _matrix(tuple(float(x) for x in operands), matrix)
            elif operator == b"Do":
                obj = objects.get(operands[0])
                if obj is None or obj.get_object().get("/Subtype") != "/Image": return None
                if int(obj.get_object().get("/Width", 0)) * int(obj.get_object().get("/Height", 0)) > max_pixels: return None
                drawings.append(matrix)
        if len(drawings) != 1: return None
        a,b,c,d,e,f = drawings[0]
        if abs(b) > 0.001 or abs(c) > 0.001 or a <= 0 or d <= 0: return None
        if max(abs(e-left),abs(f-bottom),abs(e+a-right),abs(f+d-top)) > max(width,height)*0.025: return None
        image = page.images[0].image
        if image.width < 400 or image.height < 400: return None
        if abs((image.width/image.height)/(width/height)-1) > 0.04: return None
        return image.copy()
    except Exception:
        return None


def _ocr_image(page, data, page_number, limits):
    scan = _full_page_scan(page, limits.max_page_pixels)
    if scan is not None:
        target_width = float(page.cropbox.width) / 72 * limits.ocr_dpi
        factor = max(1., target_width / scan.width)
        factor = min(factor, math.sqrt(limits.max_page_pixels / (scan.width * scan.height)))
        size = (max(1, int(scan.width * factor)), max(1, int(scan.height * factor)))
        if size != scan.size: scan = scan.resize(size, Image.Resampling.LANCZOS)
        return scan, "embedded_full_page_scan_no_annotations"
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ReaderError("A local PDF renderer is unavailable for OCR.") from None
    document = pdfium.PdfDocument(data)
    try:
        pdfpage = document[page_number-1]
        width, height = pdfpage.get_size()
        scale = min(limits.ocr_dpi/72., math.sqrt(limits.max_page_pixels / (width*height)))
        bitmap = pdfpage.render(scale=scale, draw_annots=False, may_draw_forms=False)
        try: image = bitmap.to_pil().copy()
        finally: bitmap.close(); pdfpage.close()
        return image, "page_render_no_annotations"
    finally:
        document.close()


def _cache_key(sha, ocr, limits, role="unclassified"):
    signature = f"{PIPELINE_VERSION}|{ocr.version}|role={role}|dpi={limits.ocr_dpi}|retry={limits.retry_dpi}|rows={limits.max_statement_rows_per_page}|pixels={limits.max_page_pixels}|chars={limits.max_text_chars_per_page}"
    return sha + "_" + hashlib.sha256(signature.encode()).hexdigest()[:24], signature


def _read_cache(sha, ocr, limits, page_count, role="unclassified"):
    key, signature = _cache_key(sha, ocr, limits, role)
    path = CACHE_DIR / (key + ".json")
    try:
        if _is_link(path): return {}, path, signature
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("sha256") != sha or value.get("signature") != signature or value.get("page_count") != page_count: return {}, path, signature
        pages = value.get("pages", {})
        if not isinstance(pages, dict): return {}, path, signature
        for number, record in pages.items():
            if not str(number).isdigit() or not 1 <= int(number) <= page_count or not isinstance(record.get("text"), str) or len(record["text"]) > limits.max_text_chars_per_page:
                return {}, path, signature
        return pages, path, signature
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, path, signature


def _write_cache(path, sha, signature, pages, page_count):
    try:
        with _CACHE_LOCK:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            if _is_link(CACHE_DIR) or _is_link(LAB / "work") or not CACHE_DIR.resolve().is_relative_to((LAB / "work").resolve()): return
            temporary = path.with_suffix("." + uuid4().hex + ".tmp")
            temporary.write_text(json.dumps({"sha256": sha, "signature": signature, "page_count": page_count, "pages": pages}), encoding="utf-8")
            os.replace(temporary, path)
    except OSError:
        # Cache failure must not turn valid evidence into fabricated/missing text.
        return


class _PdfiumPage:
    def __init__(self, document, index):
        self.document, self.index = document, index

    def extract_text(self):
        page = self.document[self.index]
        text = page.get_textpage()
        try: return text.get_text_range()
        finally: text.close(); page.close()


class _PdfiumReader:
    def __init__(self, data):
        import pypdfium2 as pdfium
        self.document = pdfium.PdfDocument(data)
        self.pages = [_PdfiumPage(self.document, i) for i in range(len(self.document))]

    def close(self):
        self.document.close()


def _open_reader(data):
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted: raise ReaderError("Password-protected PDFs are not read.")
        len(reader.pages)  # Force catalog resolution before choosing this parser.
        return reader, "pypdf"
    except ReaderError:
        raise
    except Exception:
        try: return _PdfiumReader(data), "pdfium_fallback"
        except Exception: raise ReaderError("Neither installed local PDF parser could read this document.") from None


def _raise_cancellation(exc):
    # Root background jobs use this explicit exception to cross nested reader
    # handlers. Cancellation must never be converted into an OPEN PDF result.
    if type(exc).__name__ == "ReviewCancelled": raise exc


def _ocr_page(page, data, number, bounds, ocr, role, cancel_check=None):
    if cancel_check: cancel_check()
    if not ocr.available: return {"text": "", "confidence": 0, "failure": ocr.error, "attempts": []}
    attempts, result = [], {"text": "", "confidence": 0}
    try:
        image, image_source = _ocr_image(page, data, number, bounds)
        try:
            if cancel_check: cancel_check()
            result = {**ocr.recognize(image), "image_source": image_source, "image_size": list(image.size)}
        finally: image.close()
        attempts.append({"method": "ocr_full_page", "dpi": bounds.ocr_dpi, "confidence": result.get("confidence", 0),
                         "quality": text_quality(result.get("text"))})
        if role == "statement":
            rows, row_attempts, truncated = _statement_regions(page, data, number, bounds, ocr, result, cancel_check)
            attempts.extend(row_attempts)
            result["statement_rows"] = rows
            if truncated: result["row_limit_reached"] = True
            if rows:
                result["base_text"] = result["text"]
                result["text"] += "\n\n" + "\n\n".join(r["header_text"]+"\n"+r["row_text"] for r in rows)
            # Only one high-resolution full-page retry, when no usable region
            # already exists. Low results remain low; the confidence gate is
            # unchanged. Both successful and failed attempts are cached.
            elif (text_quality(result.get("text")) != "usable" or result.get("confidence", 0) < 70) and bounds.retry_dpi > bounds.ocr_dpi:
                if cancel_check: cancel_check()
                retry_bounds = Limits(**{**asdict(bounds), "ocr_dpi": bounds.retry_dpi})
                image, image_source = _ocr_image(page, data, number, retry_bounds)
                try:
                    if cancel_check: cancel_check()
                    retry = {**ocr.recognize(image), "image_source": image_source, "image_size": list(image.size)}
                finally: image.close()
                attempts.append({"method": "ocr_full_page_retry", "dpi": bounds.retry_dpi,
                                 "confidence": retry.get("confidence", 0), "quality": text_quality(retry.get("text"))})
                # A higher DPI alone is not evidence of a better extraction.
                if text_quality(retry.get("text")) == "usable" and retry.get("confidence", 0) > result.get("confidence", 0):
                    result = retry
                retry_rows, row_attempts, truncated = _statement_regions(page, data, number, retry_bounds, ocr, retry, cancel_check)
                attempts.extend(row_attempts)
                if retry_rows:
                    result["statement_rows"] = retry_rows
                    result["base_text"] = result["text"]
                    result["text"] += "\n\n" + "\n\n".join(r["header_text"]+"\n"+r["row_text"] for r in retry_rows)
                if truncated: result["row_limit_reached"] = True
    except Exception as exc:
        _raise_cancellation(exc)
        message = str(exc) if isinstance(exc, ReaderError) else "Local OCR retry failed; no additional text was inferred."
        attempts.append({"method": "ocr_failed", "error": message})
        if not result.get("text"): result["failure"] = message
        else: result["retry_error"] = message
    result["attempts"] = attempts
    return result


def read_documents(source_dir, sample_ids, documents=None, limits=None, progress=None, cancel_check=None):
    """Read matched PDFs locally, preserving 1-based page provenance and SHA256.

    Missing/ambiguous evidence and failed/low-quality OCR are OPEN diagnostics.
    Native or OCR text is never printed. A source change invalidates its pages.
    """
    inventory = discover_documents(source_dir, sample_ids, documents, limits)
    if not inventory["ok"]: return inventory
    bounds = _limits(limits)
    selected = [d for sample in inventory["samples"] for d in sample["documents"]]
    if sum(d["bytes"] for d in selected) > bounds.max_total_bytes:
        inventory["ok"] = False
        inventory["diagnostics"].append(_diagnostic("TOTAL_SIZE_LIMIT", "Selected PDFs exceed the total byte limit."))
        return inventory
    ocr = None
    total_pages = 0
    total_bytes = 0
    for sample in inventory["samples"]:
        for doc in sample["documents"]:
            if cancel_check: cancel_check()
            if progress: progress({"stage": "reading_pdfs", "sample_id": sample["sample_id"], "document": doc["relative_path"]})
            path = Path(doc["path"])
            doc.update(pages=[], diagnostics=[], source_unchanged=None, status="OPEN")
            try:
                if doc["bytes"] > bounds.max_file_bytes: raise ReaderError("A PDF exceeds the per-file byte limit.")
                if _is_link(path) or not path.resolve().is_relative_to(Path(inventory["source_root"])):
                    raise ReaderError("A PDF path became linked or escaped the evidence root.")
                data = _bounded_bytes(path, bounds.max_file_bytes)
                # Integrity applies to every bounded file read, including an
                # invalid PDF. A hash does not make its contents valid evidence.
                sha = hashlib.sha256(data).hexdigest()
                doc["sha256"] = sha
                total_bytes += len(data)
                if total_bytes > bounds.max_total_bytes: raise ReaderError("Selected PDFs exceed the total byte limit.")
                if not data.startswith(b"%PDF-"): raise ReaderError("A .pdf file does not have a valid PDF header.")
                with _PDF_LOCK:
                    logger = logging.getLogger("pypdf")
                    previous = logger.level
                    logger.setLevel(logging.CRITICAL + 1)
                    reader = None
                    try:
                        reader, backend = _open_reader(data)
                        doc["reader_backend"] = backend
                        count = len(reader.pages)
                        doc["page_count"] = count
                        if count < 1 or total_pages + count > bounds.max_pages:
                            raise ReaderError("Selected PDF page limit exceeded; remaining pages were not extracted.")
                        total_pages += count
                        if ocr is None: ocr = OfflineTesseract()
                        cached, cache_path, signature = _read_cache(sha, ocr, bounds, count, doc["role"])
                        new_cache = dict(cached)
                        for number, page in enumerate(reader.pages, 1):
                            if cancel_check: cancel_check()
                            if progress: progress({"stage": "reading_pdfs", "sample_id": sample["sample_id"],
                                                   "document": doc["relative_path"], "page": number, "document_pages": count})
                            try: native = page.extract_text() or ""
                            except Exception: native = ""
                            quality = text_quality(native)
                            page_record = {"page": number, "text": "", "method": "native", "quality": quality,
                                           "needs_review": False, "diagnostics": [], "from_cache": False}
                            if len(native) > bounds.max_text_chars_per_page:
                                page_record.update(quality="text_limit", needs_review=True)
                                page_record["diagnostics"].append(_diagnostic("TEXT_LIMIT", "Page text exceeded the bounded extraction size."))
                            elif quality == "usable":
                                page_record["text"] = native
                            else:
                                page_record.update(method="ocr", needs_review=True, native_quality=quality)
                                try:
                                    if str(number) in cached:
                                        result = cached[str(number)]
                                        page_record["from_cache"] = True
                                    else:
                                        result = _ocr_page(page, data, number, bounds, ocr, doc["role"], cancel_check)
                                        new_cache[str(number)] = result
                                    page_record["ocr_attempts"] = result.get("attempts", [])
                                    if result.get("failure"): raise ReaderError(result["failure"])
                                    text = result["text"]
                                    if len(text) > bounds.max_text_chars_per_page: raise ReaderError("OCR text exceeded the page-size limit.")
                                    q = text_quality(text)
                                    needs_review = q != "usable" or result.get("confidence", 0) < 70
                                    page_record.update(text=text if q in {"usable", "sparse"} else "", quality=q,
                                                       ocr_confidence=result.get("confidence"), image_source=result.get("image_source"), needs_review=needs_review)
                                    page_record["statement_rows"] = result.get("statement_rows", [])
                                    page_record["transaction_rows_complete"] = False
                                    page_record["covered_page_transactions"] = False
                                    if result.get("retry_error"):
                                        page_record["diagnostics"].append(_diagnostic("OCR_RETRY_FAILED", result["retry_error"]))
                                    if result.get("row_limit_reached"):
                                        page_record["diagnostics"].append(_diagnostic("STATEMENT_ROW_LIMIT", "Additional wire rows exceeded the bounded row retry limit."))
                                    if needs_review:
                                        page_record["diagnostics"].append(_diagnostic("OCR_REVIEW_REQUIRED", "OCR quality is low or incomplete; verify the physical PDF page."))
                                except Exception as exc:
                                    _raise_cancellation(exc)
                                    page_record.update(text="", quality="unavailable_or_failed")
                                    page_record["diagnostics"].append(_diagnostic("OCR_UNAVAILABLE_OR_FAILED", str(exc) if isinstance(exc, ReaderError) else "Local OCR could not read this page; no text was inferred."))
                            doc["pages"].append(page_record)
                        if new_cache: _write_cache(cache_path, sha, signature, new_cache, count)
                    finally:
                        if reader is not None and hasattr(reader, "close"):
                            reader.close()
                        logger.setLevel(previous)
                try:
                    after = hashlib.sha256(_bounded_bytes(path, bounds.max_file_bytes)).hexdigest() if path.is_file() and not _is_link(path) else None
                except (OSError, ReaderError):
                    after = None
                doc["source_unchanged"] = after == sha
                if after != sha:
                    doc["pages"] = []
                    doc["diagnostics"].append(_diagnostic("SOURCE_CHANGED", "The PDF changed during extraction; all extracted pages were withheld."))
                doc["status"] = "OPEN" if doc["diagnostics"] or any(p["diagnostics"] for p in doc["pages"]) else "EXTRACTED"
            except Exception as exc:
                _raise_cancellation(exc)
                doc["diagnostics"].append(_diagnostic("PDF_UNREADABLE_OR_LIMITED", str(exc) if isinstance(exc, ReaderError) else "PDF extraction failed; no evidence was inferred."))
                doc["status"] = "OPEN"
                # Even an unreadable file gets an integrity check if bytes were read.
                if doc.get("sha256"):
                    try: doc["source_unchanged"] = not _is_link(path) and hashlib.sha256(_bounded_bytes(path, bounds.max_file_bytes)).hexdigest() == doc["sha256"]
                    except (OSError, ReaderError): doc["source_unchanged"] = False
                    if not doc["source_unchanged"]: doc["pages"] = []
        if any(d.get("status") == "OPEN" for d in sample["documents"]): sample["status"] = "OPEN"
    integrity = [d.get("source_unchanged") for d in selected]
    inventory["source_unchanged"] = (False if any(value is False for value in integrity)
                                      else True if integrity and all(value is True for value in integrity)
                                      else None)
    inventory["extracted_page_count"] = sum(len(d["pages"]) for d in selected)
    inventory["ocr_cache_directory"] = str(CACHE_DIR)
    inventory["pipeline_version"] = PIPELINE_VERSION
    inventory["limitation"] = "OCR text, especially digits/table rows, requires source-page verification. Extraction is not a control conclusion."
    return inventory
