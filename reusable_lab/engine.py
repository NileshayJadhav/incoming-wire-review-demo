"""Reusable local incoming-wire review orchestration. Sources are never overwritten."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

LAB = Path(__file__).resolve().parent
ROOT = LAB.parent
NODE = Path(os.environ.get("IWT_NODE_EXECUTABLE") or shutil.which("node") or "node")
DEFAULT_RULES = LAB / "rules" / "user_confirmed_2025.json"


class ReviewCancelled(ValueError):
    """Cooperative stop between stages; completed checkpoints remain reusable."""


def _atomic_json(path, value):
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _checkpoint_signature(context, sample, prepared, model_name):
    # Bind saved work to both current sources and current validators. A changed
    # PDF, layout, rule, reader, or evaluator never inherits an earlier result.
    def evidence_only(value):
        if isinstance(value, dict):
            return {k: evidence_only(v) for k, v in value.items() if k not in
                    {"from_cache", "elapsed_seconds", "timings", "ocr_cache_directory"}}
        if isinstance(value, list):
            return [evidence_only(v) for v in value]
        return value
    payload = {"context": context, "sample": evidence_only(sample), "source": prepared["book"]["source_hash"],
               "rules": prepared["rules_hash"], "mapping": prepared["mapping_hash"], "model": model_name,
               "code": {name: _hash(LAB / name) for name in
                        ("document_reader.py", "fact_extractor.py", "source_guard.py", "criteria.py", "workbook_io.py")}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _input_file(value, suffix, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Provide the {label} path explicitly.")
    path = Path(value.strip().strip('"')).expanduser()
    if not path.is_absolute() or not path.is_file() or path.suffix.casefold() != suffix:
        raise ValueError(f"{label} must be an existing absolute {suffix} file path on this laptop.")
    if path.stat().st_size > 30 * 1024 * 1024:
        raise ValueError(f"{label} exceeds the 30 MiB input limit.")
    return path.resolve()


def _config(path):
    if not path:
        return {}
    file = _input_file(path, ".json", "batch mapping")
    if file.stat().st_size > 1024 * 1024:
        raise ValueError("Batch mapping exceeds 1 MiB.")
    value = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not set(value).issubset({"layout", "documents"}):
        raise ValueError("Batch mapping accepts only layout and documents objects.")
    return value


def _select(samples, request):
    ids = [s["sample_id"] for s in samples]
    if not isinstance(request, str):
        raise ValueError("Samples must be all or comma-separated exact sample IDs.")
    if request.strip().casefold() == "all":
        selected = ids
    else:
        selected = [x.strip() for x in request.split(",")]
        if not selected or any(not x for x in selected) or len(set(selected)) != len(selected):
            raise ValueError("Provide unique, nonempty exact sample IDs separated by commas.")
        if not set(selected).issubset(ids):
            raise ValueError("A requested sample ID is not present in the selected workbook.")
    if len(selected) > 20:
        raise ValueError(f"This request selects {len(selected)} samples; the limit is 20 per run. Replace Samples: all with up to 20 comma-separated exact sample IDs for each batch.")
    return [s for s in samples if s["sample_id"] in selected]


def prepare(workbook_path, documents_folder, sample_request="all", rules_path="", mapping_path=""):
    from .workbook_io import discover_workbook
    from .document_reader import discover_documents
    from .criteria import load_rules
    # Conversational tools sometimes pass the user's selection label literally.
    # Normalize only these explicit built-in choices, never infer a file location.
    if isinstance(mapping_path, str) and mapping_path.strip().casefold() in ("automatic", "auto", "none", "blank"):
        mapping_path = ""
    if isinstance(rules_path, str) and rules_path.strip().casefold() in ("default", "default rules", "none", "blank"):
        rules_path = ""
    source = _input_file(workbook_path, ".xlsx", "test sheet")
    if not isinstance(documents_folder, str) or not documents_folder.strip():
        raise ValueError("Provide the documents folder path explicitly.")
    folder = Path(documents_folder.strip().strip('"')).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError("Documents folder must be an existing absolute local folder; SharePoint URLs are not supported.")
    folder = folder.resolve()
    mapping_file = _input_file(mapping_path, ".json", "batch mapping") if mapping_path else None
    mapping_hash = _hash(mapping_file) if mapping_file else None
    mapping = _config(str(mapping_file)) if mapping_file else {}
    if mapping_file and _hash(mapping_file) != mapping_hash:
        raise ValueError("Batch mapping changed while it was read.")
    source_before = _hash(source)
    book = discover_workbook(source, mapping.get("layout"))
    if _hash(source) != source_before or book["source_hash"] != source_before:
        raise ValueError("Test sheet changed while its layout was read.")
    selected = _select(book["samples"], sample_request)
    rules_file = _input_file(rules_path, ".json", "testing rules") if rules_path else DEFAULT_RULES
    rules_hash = _hash(rules_file)
    rules = load_rules(rules_file)
    if _hash(rules_file) != rules_hash:
        raise ValueError("Testing rules changed while they were read.")
    if rules.get("provenance", {}).get("test_only") is True:
        fixture_root = (LAB / "test_fixtures").resolve()
        if not source.is_relative_to(fixture_root) or not folder.is_relative_to(fixture_root):
            raise ValueError("Fictional QA criteria are restricted to the supplied fictional test fixtures.")
    documents = discover_documents(folder, [s["sample_id"] for s in selected], documents=mapping.get("documents"))
    if not documents.get("ok"):
        raise ValueError("Document inventory could not be completed; check the folder, mapping and PDF limits.")
    return {"source": source, "folder": folder, "book": book, "samples": selected,
            "rules": rules, "rules_path": rules_file, "rules_hash": rules_hash,
            "mapping": mapping, "mapping_path": mapping_file, "mapping_hash": mapping_hash, "documents": documents}


def inventory_batch(workbook_path, documents_folder, sample_request="all", rules_path="", mapping_path=""):
    try:
        # Like the review component, load local parser fixes on the next tool
        # invocation without requiring a Langflow restart or credential changes.
        from importlib import reload
        from . import workbook_io
        reload(workbook_io)
        p = prepare(workbook_path, documents_folder, sample_request, rules_path, mapping_path)
        rows = {s["sample_id"]: s["row"] for s in p["samples"]}
        return {"ok": True, "status": "INVENTORIED", "workbook": p["source"].as_posix(), "documents_folder": p["folder"].as_posix(),
                "sheet_name": p["book"]["sheet_name"], "header_row": p["book"]["header_row"], "columns": p["book"]["columns"],
                "samples": [{"sample_id": s["sample_id"], "row": rows[s["sample_id"]],
                             "documents": [{"file": d["relative_path"], "role": d["role"]} for d in s["documents"]],
                             "missing": s.get("missing", []), "ambiguous": s.get("ambiguous", [])} for s in p["documents"]["samples"]],
                "warnings": p["book"].get("warnings", []) + p["documents"].get("diagnostics", []),
                "rules_file": p["rules_path"].as_posix(), "model_calls": 0}
    except Exception as exc:
        return _failure(exc)


def _failure(exc):
    # Local validation errors are written by us, provider exceptions never arrive here unredacted.
    message = str(exc) if isinstance(exc, ValueError) else "Local review failed; no completed draft is claimed."
    return {"ok": False, "status": "BLOCKED", "error_type": type(exc).__name__, "error": message[:500]}


def presentation(report):
    lines = ["Created a **draft for human review** from the selected test sheet and current PDFs.", "",
             f"Output folder: `{Path(report['workbook']).parent.as_posix()}`", "",
             f"Workbook: [{Path(report['workbook']).name}]({Path(report['workbook']).as_posix()})",
             f"Evidence report: [Review_Report.json]({Path(report['report']).as_posix()})", "",
             "| Sample | Fee | Fee charged | Classification | Travel | OFAC | Posting |",
             "|---|---:|---|---|---|---|---|"]
    for r in report["results"]:
        v = r["values"]
        row = [r["sample_id"], "OPEN" if v["fee"] is None else str(v["fee"]), *(str(v[k]) for k in ("fee_charged", "classification", "travel", "ofac", "posting"))]
        lines.append("| " + " | ".join(x.replace("|", "\\|").replace("\n", " ") for x in row) + " |")
    manual = [r["sample_id"] for r in report["results"] if r["manual_review"]]
    lines.extend(["", "Manual review: " + (", ".join(manual) if manual else "No OPEN checks under the selected criteria; reviewer sign-off still required.")])
    for r in report["results"]:
        if r["manual_review"]:
            lines.append(f"- Sample {r['sample_id']}: {r['values']['comments']}")
    lines.extend(["", "PDF facts were extracted at run time and quotes checked against their cited pages. OCR/model extraction can still be wrong; inspect the evidence report before signing off.",
                  "X = supported under the selected reviewer criteria; - = not applicable with a recorded reason; OPEN = manual review required.",
                  "Original files were preserved. Rules are reviewer supplied, not independently verified company policy."])
    return "\n".join(lines)


def review_batch(workbook_path, documents_folder, sample_request="all", rules_path="", mapping_path="", *, model=None,
                 model_name="gpt-4o-mini", force_refresh=False, extractor=None, workers=1,
                 progress=None, cancel_check=None, checkpoint_dir=None):
    started = time.monotonic()
    timings = {}
    progress_lock = threading.Lock()
    finished_ids = []
    abort = threading.Event()

    def stop():
        if abort.is_set():
            raise ValueError("A sample failed; further extraction was stopped. No completed draft is claimed.")
        if cancel_check is not None:
            cancel_check()

    def notify(stage, **details):
        if progress is not None:
            with progress_lock:
                progress({"stage": stage, "elapsed_seconds": round(time.monotonic() - started, 2), **details})

    try:
        from .document_reader import read_documents, discover_documents
        from .fact_extractor import extract_sample
        from .criteria import evaluate_sample
        stop()
        notify("validating_inputs")
        p = prepare(workbook_path, documents_folder, sample_request, rules_path, mapping_path)
        if type(workers) is not int or not 1 <= workers <= 2:
            raise ValueError("Use one or two bounded extraction workers.")
        if model is None and extractor is None:
            raise ValueError("Select your existing OpenAI key in the Reusable Wire Review component before testing.")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        output = LAB / "outputs" / run_id
        output.mkdir(parents=True, exist_ok=False)
        checkpoints = Path(checkpoint_dir).resolve() if checkpoint_dir else None
        if checkpoints:
            if not checkpoints.is_relative_to((LAB / "work").resolve()):
                raise ValueError("Checkpoint directory must stay inside this project's work folder.")
            checkpoints.mkdir(parents=True, exist_ok=True)
        notify("reading_pdfs", total_samples=len(p["samples"]), completed_samples=0, run_id=run_id)
        read_start = time.monotonic()
        reader_options = {"documents": p["mapping"].get("documents")}
        if progress is not None:
            reader_options["progress"] = lambda details: notify(details.get("stage", "reading_pdfs"),
                                                               **{k: v for k, v in details.items() if k != "stage"})
        if cancel_check is not None:
            reader_options["cancel_check"] = stop
        docs = read_documents(p["folder"], [s["sample_id"] for s in p["samples"]], **reader_options)
        timings["pdf_read_seconds"] = round(time.monotonic() - read_start, 3)
        stop()
        if not docs.get("ok"):
            raise ValueError("PDF extraction could not complete within the input limits; no completed draft is claimed.")
        # A traversal gap can hide another candidate document for any selected sample.
        # Preserve those global coverage gaps in every sample before extracting facts.
        for sample in docs["samples"]:
            sample["diagnostics"] = list(sample.get("diagnostics", [])) + list(docs.get("diagnostics", []))
        by_id = {s["sample_id"]: s for s in docs["samples"]}
        results, extracted, extraction_meta = [], [], []
        extraction_start = time.monotonic()

        def process_sample(context):
            stop()
            sample = by_id[context["sample_id"]]
            notify("extracting_facts", current_sample=context["sample_id"],
                   total_samples=len(p["samples"]), completed_samples=len(finished_ids))
            cp = None
            signature = _checkpoint_signature(context, sample, p, model_name) if checkpoints else None
            if checkpoints:
                cp = checkpoints / (hashlib.sha256(context["sample_id"].encode()).hexdigest() + ".json")
            resumed = False
            facts = None
            if cp and cp.is_file() and not force_refresh:
                try:
                    saved = json.loads(cp.read_text(encoding="utf-8"))
                    if saved.get("signature") == signature:
                        # Saved quotations must still match the current physical
                        # pages. Checkpoints are work-in-progress, not evidence.
                        from .fact_extractor import validate_facts
                        facts = validate_facts(saved["facts"], sample)
                        old = saved["facts"]
                        facts["errors"] = list(dict.fromkeys(facts.get("errors", []) + old.get("errors", [])))
                        facts["uncertainties"] = facts.get("uncertainties", []) + old.get("uncertainties", [])
                        # Keep the original quote audit only for a fact whose
                        # current page/source/expanded quotation validated again.
                        audits = list(facts.get("source_enrichments", []))
                        for entry in old.get("source_enrichments", []):
                            if not isinstance(entry, dict) or entry.get("kind") != "STATEMENT_CITATION_EXPANDED":
                                continue
                            records = facts.get(entry.get("group"), [])
                            index = entry.get("record_index")
                            if type(index) is not int or not 0 <= index < len(records):
                                continue
                            item = records[index].get(entry.get("field"))
                            if item and (item["source_file"], item["page"], item["quote"]) == (
                                    entry.get("source_file"), entry.get("page"), entry.get("expanded_quote")) and entry not in audits:
                                audits.append(entry)
                        facts["source_enrichments"] = audits
                        meta = {"model_calls": 0, "resumed_checkpoint": True, "cache_hit": True,
                                "original_model_calls": saved.get("meta", {}).get("model_calls", 0),
                                "source_enrichments": audits, "source_enrichment_count": len(audits)}
                        resumed = True
                except (OSError, ValueError, TypeError, KeyError):
                    facts = None
            if facts is None:
                if extractor is not None:
                    facts, meta = extractor(context, sample)
                else:
                    options = {"cancel_check": stop} if cancel_check is not None else {}
                    facts, meta = extract_sample(context, sample, model, LAB / "work" / "fact_cache", model_name, force_refresh, **options)
            # An in-flight provider call may finish after cancellation. Preserve
            # that validated work, but do not dispatch another sample or writer.
            result = evaluate_sample(context, facts, p["rules"])
            if cp and not resumed:
                _atomic_json(cp, {"signature": signature, "facts": facts, "meta": meta})
            with progress_lock:
                finished_ids.append(context["sample_id"])
            notify("sample_completed", current_sample=context["sample_id"],
                   completed_samples=len(finished_ids), total_samples=len(p["samples"]),
                   completed_ids=list(finished_ids), resumed_checkpoint=resumed,
                   model_calls=meta.get("model_calls", 0))
            stop()
            return result, {"sample_id": context["sample_id"], "facts": facts}, {"sample_id": context["sample_id"], **meta}

        collected = {}
        if workers == 1:
            for context in p["samples"]:
                collected[context["sample_id"]] = process_sample(context)
        else:
            # Only two dispatches at a time; queued samples check cancellation
            # before any extraction request. Output order remains workbook order.
            def guarded(context):
                try:
                    return process_sample(context)
                except ReviewCancelled:
                    raise
                except Exception:
                    abort.set()
                    raise
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wire-facts") as pool:
                futures = {pool.submit(guarded, context): context["sample_id"] for context in p["samples"]}
                for future in as_completed(futures):
                    try:
                        collected[futures[future]] = future.result()
                    except Exception:
                        for pending in futures:
                            pending.cancel()
                        raise
        timings["facts_and_checks_seconds"] = round(time.monotonic() - extraction_start, 3)
        for context in p["samples"]:
            result, fact, meta = collected[context["sample_id"]]
            results.append(result); extracted.append(fact); extraction_meta.append(meta)
        stop()
        notify("writing_draft", completed_samples=len(results), total_samples=len(results))
        stop()
        write_start = time.monotonic()
        workbook, report_file = output / "Incoming_Wire_Testing_DRAFT.xlsx", output / "Review_Report.json"
        writer_results = []
        for result in results:
            values = dict(result["values"])
            if values["fee"] is not None:
                values["fee"] = float(Decimal(values["fee"]))
            writer_results.append({"sample_id": result["sample_id"], "row": result["row"], "values": values})
        request = {"source": str(p["source"]), "source_hash": p["book"]["source_hash"], "destination": str(workbook),
                   "preview_dir": str(output / "previews"), "sheet_name": p["book"]["sheet_name"], "columns": p["book"]["columns"],
                   "results": writer_results}
        request_file = output / "writer_request.json"
        request_file.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
        stop()
        writer = os.environ.get("IWT_WRITER", "artifact").strip().casefold()
        if writer == "portable":
            from .portable_writer import write_draft
            qa = write_draft(request_file)
        elif writer == "artifact":
            completed = subprocess.run([str(NODE), str(LAB / "write_draft.mjs"), str(request_file)], capture_output=True, text=True, timeout=240)
            if completed.returncode:
                raise ValueError("Draft workbook authoring failed; no completed draft is claimed. Source files remain preserved.")
            qa = json.loads(completed.stdout)
        else:
            raise ValueError("IWT_WRITER must be artifact or portable.")
        stop()
        timings["workbook_write_seconds"] = round(time.monotonic() - write_start, 3)
        notify("verifying_sources", completed_samples=len(results), total_samples=len(results))
        unchanged = (docs.get("source_unchanged") is True and
                     _hash(p["source"]) == p["book"]["source_hash"] and _hash(p["rules_path"]) == p["rules_hash"])
        if p["mapping_path"]:
            unchanged = unchanged and _hash(p["mapping_path"]) == p["mapping_hash"]
        for sample in docs["samples"]:
            for d in sample["documents"]:
                unchanged = (unchanged and d.get("source_unchanged") is True and
                             bool(d.get("sha256")) and _hash(d["path"]) == d["sha256"])
        final_inventory = discover_documents(p["folder"], [s["sample_id"] for s in p["samples"]], documents=p["mapping"].get("documents"))
        def membership(inventory):
            return [(s["sample_id"], sorted((d["relative_path"], d["role"]) for d in s["documents"]), s.get("missing", []), s.get("ambiguous", []))
                    for s in inventory["samples"]]
        unchanged = (unchanged and final_inventory.get("ok", False) and membership(final_inventory) == membership(docs)
                     and final_inventory.get("diagnostics", []) == docs.get("diagnostics", []))
        if not unchanged:
            raise ValueError("An input changed during the run. Do not use the draft; review the current sources again.")
        stop()
        timings["total_engine_seconds"] = round(time.monotonic() - started, 3)
        report = {"ok": True, "status": "DRAFT_REQUIRES_HUMAN_REVIEW", "run_id": run_id, "source_unchanged": True,
                  "workbook": workbook.as_posix(), "report": report_file.as_posix(), "source_workbook": p["source"].as_posix(),
                  "source_workbook_sha256": p["book"]["source_hash"], "documents_folder": p["folder"].as_posix(),
                  "layout": {k: p["book"][k] for k in ("sheet_name", "header_row", "columns")},
                  "contexts": p["samples"], "results": results, "extracted_facts": extracted, "extraction": extraction_meta,
                  "rules_snapshot": p["rules"], "rules_sha256": p["rules_hash"], "mapping_snapshot": p["mapping"],
                  "document_inventory": docs, "writer_qa": qa, "warnings": p["book"].get("warnings", []),
                  "timings": timings, "extraction_workers": workers,
                  "resumed_samples": [m["sample_id"] for m in extraction_meta if m.get("resumed_checkpoint")]}
        report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        md = presentation(report)
        (output / "presentation.txt").write_text(md, encoding="utf-8")
        notify("completed", completed_samples=len(results), total_samples=len(results),
               model_extraction_calls=sum(m.get("model_calls", 0) for m in extraction_meta), timings=timings)
        return {k: report[k] for k in ("ok", "status", "run_id", "source_unchanged", "workbook", "report")} | {
            "sample_count": len(results), "manual_review_samples": [r["sample_id"] for r in results if r["manual_review"]],
            "model_extraction_calls": sum(m.get("model_calls", 0) for m in extraction_meta),
            "timings": timings, "resumed_samples": report["resumed_samples"], "presentation_markdown": md}
    except ReviewCancelled:
        notify("cancelled", completed_samples=len(finished_ids))
        return {"ok": False, "status": "CANCELLED", "error": "Review cancelled. Completed sample checkpoints were preserved; no completed draft is claimed.",
                "completed_samples": len(finished_ids)}
    except Exception as exc:
        return _failure(exc)
