"""Portable result-cell writer for installations without Codex artifact-tool.

This backend changes only the seven explicitly mapped values. The established
ZIP preservation validator checks every unrelated worksheet byte and ZIP part.
It does not calculate formulas or produce a visual preview; Excel recalculates
when opened. The existing local artifact backend remains the default.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import zipfile

from . import zip_preserve as preserve


def write_draft(request_file):
    req, source, destination, preview, part, expected = preserve.load_request(request_file)
    preview.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".portable_authored_", suffix=".xlsx", dir=preview)
    os.close(handle)
    authored = Path(name)
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(authored, "w") as target:
            edited = preserve.transplant(original.read(part), expected)
            for info in original.infolist():
                target.writestr(copy.copy(info), edited if info.filename == part else original.read(info.filename))
        qa = preserve.splice(request_file, authored)
        qa.update(status="WRITTEN", backend="portable_preserving_xml", workbook=str(destination),
                  preview_dir=str(preview), previews=[], source_previews=[],
                  formulas_recalculated=False,
                  preview_note="Portable backend preserves source formatting; no rendered preview is generated.")
        qa_file = preview / "preservation_qa.json"
        qa_file.write_text(json.dumps(qa, indent=2), encoding="utf-8")
        return qa
    finally:
        authored.unlink(missing_ok=True)
