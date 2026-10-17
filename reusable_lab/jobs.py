"""One local background review at a time, with durable progress and checkpoints.

Credentials and model objects stay in memory. Job files contain only the user's
paths, input hashes, progress and results. Cancellation is cooperative: a current
OCR/provider call may finish, but no next sample or writer is dispatched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import reload
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

LAB = Path(__file__).resolve().parent
JOBS = LAB / "work" / "jobs"
# Component code refreshes may reload this module. Keep the same locks and live
# worker registry so refreshing the UI cannot orphan or duplicate a running job.
_LOCK = globals().get("_LOCK") or threading.RLock()
_THREADS = globals().get("_THREADS", {})
_EVENTS = globals().get("_EVENTS", {})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise ValueError("Use the exact 16-character job ID returned by the review tool.")
    path = (JOBS / job_id).resolve()
    if not path.is_relative_to(JOBS.resolve()):
        raise ValueError("Job path is outside the local job folder.")
    return path


def _read(job_id):
    path = _path(job_id) / "state.json"
    if not path.is_file():
        raise ValueError("This job ID was not found on this laptop.")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(state):
    from .engine import _atomic_json
    _atomic_json(_path(state["job_id"]) / "state.json", state)


def _refresh():
    # Never reload validators while another worker is using their globals.
    from . import document_reader, source_guard, fact_extractor, criteria, workbook_io, engine
    for module in (document_reader, source_guard, fact_extractor, criteria, workbook_io, engine):
        reload(module)


def _snapshot(request):
    from . import engine
    from .document_reader import Limits, _bounded_bytes
    p = engine.prepare(*(request[k] for k in
                         ("workbook_path", "documents_folder", "sample_request", "rules_path", "mapping_path")))
    selected = [d for s in p["documents"]["samples"] for d in s["documents"]]
    bounds = Limits()
    if any(d["bytes"] > bounds.max_file_bytes for d in selected) or sum(d["bytes"] for d in selected) > bounds.max_total_bytes:
        raise ValueError("Selected PDF files exceed the bounded file/total byte limits.")
    def bounded_hash(path):
        return hashlib.sha256(_bounded_bytes(Path(path), bounds.max_file_bytes)).hexdigest()
    return {"workbook": p["book"]["source_hash"], "rules": p["rules_hash"], "mapping": p["mapping_hash"],
            "selected_ids": [s["sample_id"] for s in p["samples"]],
            "documents": [(s["sample_id"], [(d["relative_path"], d["role"], bounded_hash(d["path"]))
                           for d in s["documents"]], s.get("missing", []), s.get("ambiguous", []))
                          for s in p["documents"]["samples"]],
            "code": {name: engine._hash(LAB / name) for name in
                     ("document_reader.py", "fact_extractor.py", "source_guard.py", "criteria.py", "engine.py", "jobs.py",
                      "workbook_io.py", "write_draft.mjs", "zip_preserve.py", "portable_writer.py")}}


def _fingerprint(request, snapshot):
    return hashlib.sha256(json.dumps([request, snapshot], sort_keys=True, default=str).encode()).hexdigest()


def _active():
    return next((job_id for job_id, thread in _THREADS.items() if thread.is_alive()), None)


def _public(state):
    result = state.get("result") or {}
    status = state["status"]
    progress = dict(state.get("progress", {}))
    if status in ("RUNNING", "CANCELLING"):
        try:
            started = datetime.fromisoformat(state.get("attempt_started_at", state["created_at"]))
            progress["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
        except (ValueError, TypeError):
            pass
    answer = {"ok": status not in ("BLOCKED", "CANCELLED", "INTERRUPTED"),
              "status": status, "job_id": state["job_id"], "progress": progress,
              "progress_file": (_path(state["job_id"]) / "state.json").as_posix(),
              "created_at": state["created_at"], "updated_at": state["updated_at"]}
    if status == "DRAFT_REQUIRES_HUMAN_REVIEW":
        answer.update(result)
    else:
        completed = progress.get("completed_samples", 0)
        total = progress.get("total_samples", "?")
        note = {"RUNNING": "Review is running in the background.",
                "CANCELLING": "Cancellation requested. The current OCR/model call may finish; saved sample checkpoints will be retained.",
                "CANCELLED": "Review cancelled; no completed draft is claimed. Completed sample checkpoints can be resumed.",
                "INTERRUPTED": "The local server stopped before this job finished. Resume explicitly to reuse saved checkpoints.",
                "BLOCKED": result.get("error", "Review failed; no completed draft is claimed.")}.get(status, status)
        answer["presentation_markdown"] = (f"{note}\n\nJob ID: `{state['job_id']}`\n"
            f"Progress: {completed}/{total} samples; stage: {progress.get('stage', 'starting')}.\n\n"
            f"Use `status {state['job_id']}`, `cancel {state['job_id']}` or `resume {state['job_id']}` in this chat.")
        if result.get("error"):
            answer["error"] = result["error"]
    return answer


def _launch(state, model, extractor=None):
    from . import engine
    job_id = state["job_id"]
    event = threading.Event()
    _EVENTS[job_id] = event

    def cancel_check():
        if event.is_set():
            raise engine.ReviewCancelled()

    def progress(update):
        with _LOCK:
            current = _read(job_id)
            # A page update retains the overall sample count and earlier IDs.
            current["progress"].update(update)
            if update.get("stage") == "sample_completed":
                current["progress"]["model_extraction_calls_so_far"] = current["progress"].get("model_extraction_calls_so_far", 0) + update.get("model_calls", 0)
            current["updated_at"] = _now()
            _write(current)

    def run():
        try:
            request = dict(state["request"])
            # Resume checks source membership/content again inside the worker.
            if _fingerprint(request, _snapshot(request)) != state["fingerprint"]:
                result = {"ok": False, "status": "BLOCKED", "error": "An input or validator changed. Start a new review with the current sources; old checkpoints were preserved."}
            else:
                result = engine.review_batch(**request, model=model, extractor=extractor,
                    checkpoint_dir=_path(job_id) / "checkpoints", progress=progress, cancel_check=cancel_check)
        except Exception:
            # Provider exceptions can contain credentials; never serialize them.
            result = {"ok": False, "status": "BLOCKED", "error": "Background review failed; no completed draft is claimed. Inspect local configuration."}
        with _LOCK:
            current = _read(job_id)
            current.update(status=result.get("status", "BLOCKED"), result=result, updated_at=_now())
            _write(current)
            _EVENTS.pop(job_id, None)

    thread = threading.Thread(target=run, name="wire-review-" + job_id, daemon=True)
    _THREADS[job_id] = thread
    thread.start()


def start_review(workbook_path, documents_folder, sample_request="all", rules_path="", mapping_path="", *,
                 model=None, model_name="gpt-4o-mini", force_refresh=False, workers=2, extractor=None, _reload=True):
    request = dict(workbook_path=workbook_path, documents_folder=documents_folder,
                   sample_request=sample_request, rules_path=rules_path, mapping_path=mapping_path,
                   model_name=model_name, force_refresh=force_refresh, workers=workers)
    try:
        with _LOCK:
            active = _active()
            if active:
                state = _read(active)
                if request == state["request"]:
                    return _public(state) | {"existing_job": True}
                return {"ok": False, "status": "BLOCKED", "active_job_id": active,
                        "error": f"Job {active} is already running. Check its status or cancel it before starting a different review."}
            if model is None and extractor is None:
                raise ValueError("Select the existing saved OpenAI key before starting a review.")
            if type(workers) is not int or not 1 <= workers <= 2:
                raise ValueError("Use one or two bounded extraction workers.")
            if _reload:
                _refresh()
            snapshot = _snapshot(request)
            # JSON-normalize before hashing; tuples deserialize to lists.
            snapshot = json.loads(json.dumps(snapshot))
            job_id = uuid.uuid4().hex[:16]
            _path(job_id).mkdir(parents=True, exist_ok=False)
            state = {"job_id": job_id, "status": "RUNNING", "request": request,
                     "snapshot": snapshot, "fingerprint": _fingerprint(request, snapshot),
                     "created_at": _now(), "attempt_started_at": _now(), "updated_at": _now(), "result": None,
                     "progress": {"stage": "starting", "completed_samples": 0,
                                  "total_samples": len(snapshot["selected_ids"]), "completed_ids": []}}
            _write(state)
            _launch(state, model, extractor)
            return _public(state)
    except ValueError as exc:
        return {"ok": False, "status": "BLOCKED", "error": str(exc)[:500]}
    except Exception:
        return {"ok": False, "status": "BLOCKED", "error": "Job setup failed; no completed draft is claimed."}


def control(job_id, action="status", *, model=None, extractor=None, _reload=True):
    try:
        if action not in ("status", "cancel", "resume"):
            raise ValueError("Action must be status, cancel or resume.")
        with _LOCK:
            state = _read(job_id)
            active = _THREADS.get(job_id)
            running = bool(active and active.is_alive())
            if state["status"] in ("RUNNING", "CANCELLING") and not running:
                state.update(status="INTERRUPTED", updated_at=_now())
                _write(state)
            if action == "cancel" and running:
                _EVENTS[job_id].set()
                state.update(status="CANCELLING", updated_at=_now())
                _write(state)
            elif action == "resume":
                if running or state["status"] == "DRAFT_REQUIRES_HUMAN_REVIEW":
                    return _public(state)
                if _active():
                    raise ValueError("Another review is running. Finish or cancel it before resuming this job.")
                if model is None and extractor is None:
                    raise ValueError("Select the same saved OpenAI key to resume this job.")
                if _reload:
                    _refresh()
                if _fingerprint(state["request"], _snapshot(state["request"])) != state["fingerprint"]:
                    raise ValueError("An input or validator changed. Start a new review with current sources; old checkpoints were preserved.")
                state.update(status="RUNNING", result=None, updated_at=_now(), attempt_started_at=_now())
                state["progress"].update(stage="resuming", completed_samples=0, completed_ids=[], model_extraction_calls_so_far=0)
                _write(state)
                _launch(state, model, extractor)
            return _public(state)
    except ValueError as exc:
        return {"ok": False, "status": "BLOCKED", "error": str(exc)[:500]}
    except Exception:
        return {"ok": False, "status": "BLOCKED", "error": "Job control failed; inspect the local job ID and configuration."}
