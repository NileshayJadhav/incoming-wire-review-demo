"""Local wire evaluation: frozen synthetic regression or labeled report scoring.

No network calls occur in the CLI. The opt-in evaluate_model(model, ...) API
uses an already-authorized injected model; it never reads/stores credentials.
Real records are never treated as gold without a reviewer-attested manifest.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import time

try:
    from . import fact_extractor as extractor_module
    from .criteria import evaluate_sample, load_rules
    from .fact_extractor import extract_sample, validate_facts
except ImportError:
    import fact_extractor as extractor_module
    from criteria import evaluate_sample, load_rules
    from fact_extractor import extract_sample, validate_facts

LAB = Path(__file__).resolve().parent
FIXTURES = LAB / "evaluation"
CONTROLS = ("fee_charged", "travel", "ofac", "posting")
STATUSES = ("SUPPORTED", "NOT_APPLICABLE", "OPEN")
EVALUATION_VERSION = "wire-evaluation-1.0"
FIELD_PATH = re.compile(r"^(incoming|statements|returns)\[(\d+)\]\.([a-z_]+)$")


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _normal(value):
    return " ".join(str(value).split())


def _ratio(a, b):
    return round(a / b, 6) if b else None


def _index(items, label):
    if not isinstance(items, list):
        raise ValueError(f"{label} must be a list.")
    result, duplicates = {}, []
    for item in items:
        if not isinstance(item, dict) or item.get("sample_id") is None:
            raise ValueError(f"{label} has an invalid sample ID.")
        key = str(item["sample_id"])
        if key in result:
            duplicates.append(key)
        result[key] = item
    return result, sorted(set(duplicates))


def field_at(facts, path):
    """Read the cited value at a declared path; missing and explicit null agree."""
    match = FIELD_PATH.fullmatch(path)
    if not match:
        raise ValueError(f"Unsupported expected field path: {path}")
    records = facts.get(match[1], []) if isinstance(facts, dict) else []
    if not isinstance(records, list) or int(match[2]) >= len(records):
        return None
    record = records[int(match[2])]
    item = record.get(match[3]) if isinstance(record, dict) else None
    return item.get("value") if isinstance(item, dict) else None


def _equal(expected, actual, field):
    if expected is None or actual is None:
        return expected is actual
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if field.rsplit(".", 1)[-1] in {"amount", "fee", "principal_amount"}:
        try:
            a, b = Decimal(str(expected)), Decimal(str(actual))
            return a.is_finite() and b.is_finite() and a == b
        except InvalidOperation:
            return False
    return type(expected) is type(actual) and (_normal(expected) == _normal(actual) if isinstance(expected, str) else expected == actual)


def _aligned_field_paths(label, facts):
    """Align records by kind/reference before scoring; array order is not truth.

    Amount is deliberately excluded as an identity discriminator, so a wrong
    amount remains a field error. Stable ties preserve order for identical
    duplicate records. Partial real labels without identity keys stay positional.
    """
    expected = {}
    for path, value in label.get("expected_fields", {}).items():
        match = FIELD_PATH.fullmatch(path)
        expected.setdefault(match[1], {}).setdefault(int(match[2]), {})[match[3]] = value
    mapping = {}
    weights = {"wire_number": 16, "kind": 8, "date": 2, "account_suffix": 1}
    for group, records in expected.items():
        actual = facts.get(group, [])
        if not isinstance(actual, list):
            continue
        candidates = []
        for old_index, fields in records.items():
            for new_index, record in enumerate(actual):
                if not isinstance(record, dict):
                    continue
                score = sum(weight for field, weight in weights.items() if fields.get(field) is not None
                            and _equal(fields[field], field_at(facts, f"{group}[{new_index}].{field}"), field))
                # With one incoming record or partial labels, preserve positional
                # behavior unless a known identity provides positive evidence.
                candidates.append((score, old_index == new_index, old_index, new_index))
        assigned_old, assigned_new = set(), set()
        for score, positional, old_index, new_index in sorted(candidates, key=lambda p: (-p[0], -p[1], p[2], p[3])):
            if old_index in assigned_old or new_index in assigned_new:
                continue
            if not score and not positional:
                continue
            mapping[(group, old_index)] = new_index
            assigned_old.add(old_index); assigned_new.add(new_index)
    return {path: f"{match[1]}[{mapping.get((match[1], int(match[2])), int(match[2]))}].{match[3]}"
            for path in label.get("expected_fields", {}) for match in [FIELD_PATH.fullmatch(path)]}


def _manifest(gold):
    if not isinstance(gold, dict) or gold.get("schema_version") != 1:
        raise ValueError("Expected-label manifest schema_version must be 1.")
    origin = gold.get("data_origin")
    if origin not in {"synthetic", "real"}:
        raise ValueError("Manifest data_origin must explicitly be synthetic or real.")
    provenance = gold.get("provenance", {})
    reviewer, reviewed_on = provenance.get("reviewer"), provenance.get("reviewed_on")
    reviewed = provenance.get("human_validated") is True and isinstance(reviewer, str) and bool(reviewer.strip())
    try:
        datetime.fromisoformat(reviewed_on.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        reviewed = False
    if origin == "real" and not reviewed:
        raise ValueError("Real-sample accuracy cannot be graded without human_validated=true, reviewer and reviewed_on in the expected manifest. Use operational assessment without --gold instead.")
    cases, duplicates = _index(gold.get("cases"), "Expected cases")
    if not cases or duplicates:
        raise ValueError("Expected manifest must have unique nonempty sample IDs.")
    for case in cases.values():
        expected = case.get("expected_checks")
        if not isinstance(expected, dict) or set(expected) != set(CONTROLS) or any(v not in STATUSES for v in expected.values()):
            raise ValueError("Each expected case must explicitly label all four controls using SUPPORTED, NOT_APPLICABLE or OPEN.")
        fields = case.get("expected_fields", {})
        if not isinstance(fields, dict) or any(not FIELD_PATH.fullmatch(p) for p in fields):
            raise ValueError("Expected fields must use explicit incoming/statements/returns[index].field paths.")
        values = case.get("expected_values", {})
        if not isinstance(values, dict) or any(k not in {"fee", "classification"} for k in values):
            raise ValueError("Expected workbook values may only declare fee and classification; four check symbols are checked separately.")
        records = case.get("expected_record_counts", {})
        if not isinstance(records, dict) or any(k not in {"incoming", "statements", "returns"} or type(v) is not int or v < 0 for k, v in records.items()):
            raise ValueError("Expected record counts must declare nonnegative integers by incoming/statements/returns role.")
    return cases, reviewed


def source_identity(report):
    """Bind real labels to the source workbook, rules and per-sample PDFs."""
    documents = {}
    for sample in sources_from_report(report):
        for doc in sample.get("documents", []):
            documents[str(sample.get("sample_id")) + "/" + str(doc.get("relative_path"))] = doc.get("sha256")
    return {"source_workbook_sha256": report.get("source_workbook_sha256"),
            "rules_sha256": report.get("rules_sha256"), "document_sha256": documents}


def label_template(report):
    """Blank reviewer-owned labels; never copies model conclusions as gold."""
    cases, _ = _index(report.get("results", []), "Report results")
    return {"schema_version": 1, "dataset_id": "REPLACE_WITH_REVIEWER_GOLD_ID", "data_origin": "real",
            "source_identity": source_identity(report),
            "provenance": {"human_validated": False, "reviewer": None, "reviewed_on": None,
                           "label_method": "INDEPENDENT_PHYSICAL_PDF_AND_CRITERION_REVIEW_REQUIRED"},
            "instructions": "Independently read physical source PDFs and the supplied criteria before labeling. Do not use prior worksheet/model results as gold. Fill all four expected_checks with SUPPORTED/NOT_APPLICABLE/OPEN; add observed field values at explicit paths, and use null only for fields confirmed missing. Complete reviewer/date and human_validated only after review. This blank template cannot be scored.",
            "cases": [{"sample_id": key, "split": "held_out", "expected_checks": {c: None for c in CONTROLS},
                       "expected_fields": {}, "rationale": "REVIEWER_TO_COMPLETE"} for key in cases]}


def sources_from_report(report):
    inventory = report.get("document_inventory", {})
    return inventory.get("samples", []) if isinstance(inventory, dict) else []


def citation_audit(result, facts, sample):
    """Independent page-membership/reference audit plus production revalidation.

    Verbatim source membership is checked independently. The production value/
    party/amount validator supplies a separately labeled defense-in-depth check;
    passing this audit alone is not a semantic accuracy measurement.
    """
    if not isinstance(sample, dict):
        return {"status": "NOT_VERIFIED_NO_SOURCE_PAGES", "checked": 0, "invalid": [],
                "production_revalidation": "NOT_RUN", "supported_without_valid_citations": []}
    pages = {}
    for doc in sample.get("documents", []):
        for page in doc.get("pages", []):
            pages[(doc.get("relative_path"), page.get("page"))] = (page.get("text", ""), doc.get("role"))
    raw = {group: copy.deepcopy(facts.get(group, [])) for group in ("incoming", "statements", "returns")}
    raw["issues"] = []
    revalidated = validate_facts(raw, sample)
    by_path, invalid, checked = {}, [], 0
    for group, role in (("incoming", "incoming"), ("statements", "statement"), ("returns", "return")):
        for index, record in enumerate(raw[group]):
            if not isinstance(record, dict):
                invalid.append({"field": f"{group}[{index}]", "reason": "INVALID_RECORD"})
                continue
            for field, item in record.items():
                if item is None:
                    continue
                path = f"{group}[{index}].{field}"
                by_path[path] = item
                checked += 1
                page = pages.get((item.get("source_file"), item.get("page"))) if isinstance(item, dict) else None
                reason = None
                if not isinstance(item, dict) or type(item.get("page")) is not int or item["page"] < 1:
                    reason = "INVALID_PHYSICAL_PAGE_OR_CITATION_SHAPE"
                elif page is None or page[1] != role:
                    reason = "UNKNOWN_SOURCE_PAGE_OR_WRONG_ROLE"
                elif not isinstance(item.get("quote"), str) or not item["quote"].strip() or _normal(item["quote"]).casefold() not in _normal(page[0]).casefold():
                    reason = "QUOTE_NOT_VERBATIM_ON_CITED_PHYSICAL_PAGE"
                elif field_at(revalidated, path) is None:
                    reason = "PRODUCTION_VALUE_OR_PARTY_REVALIDATION_REJECTED"
                if reason:
                    invalid.append({"field": path, "reason": reason})
    evidence = result.get("evidence", [])
    refs = {}
    for item in evidence if isinstance(evidence, list) else []:
        path = item.get("id") if isinstance(item, dict) else None
        if path in refs:
            invalid.append({"field": path, "reason": "DUPLICATE_RESULT_EVIDENCE_ID"})
        refs[path] = item
        expected = by_path.get(path)
        if not expected or any(item.get(k) != expected.get(k) for k in ("value", "source_file", "page", "quote")):
            invalid.append({"field": path, "reason": "RESULT_EVIDENCE_DOES_NOT_MATCH_EXTRACTED_FACT"})
    invalid_ids = {entry["field"] for entry in invalid}
    unsupported = []
    for control in CONTROLS:
        check = result.get("checks", {}).get(control, {})
        if check.get("status") not in {"SUPPORTED", "NOT_APPLICABLE"}:
            continue
        ids = check.get("evidence", [])
        if not isinstance(ids, list) or not ids or any(i not in refs or i in invalid_ids for i in ids):
            unsupported.append(control)
    return {"status": "PASS" if not invalid and not unsupported else "FAIL", "checked": checked,
            "invalid": invalid, "production_revalidation": "RUN", "supported_without_valid_citations": unsupported}


def score_report(report, gold=None, source_samples=None, mode="EXISTING_REPORT"):
    """Grade structured report only; absent real labels produce NOT_GRADED."""
    if not isinstance(report, dict):
        raise ValueError("Review report must be an object.")
    expected, human_validated = _manifest(gold) if gold is not None else ({}, False)
    if gold is not None and gold.get("data_origin") == "real":
        identity = gold.get("source_identity")
        real = source_identity(report)
        hash_value = lambda v: isinstance(v, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", v))
        valid = isinstance(identity, dict) and hash_value(identity.get("source_workbook_sha256")) and hash_value(identity.get("rules_sha256")) and isinstance(identity.get("document_sha256"), dict) and bool(identity.get("document_sha256"))
        if not valid or identity != real or any(not hash_value(v) for v in identity["document_sha256"].values()):
            raise ValueError("Real expected labels must match complete source_identity hashes for this workbook, rule version and all per-sample PDFs.")
    results, duplicates = _index(report.get("results", []), "Report results")
    extracted, duplicate_facts = _index(report.get("extracted_facts", []), "Extracted facts")
    sources, duplicate_sources = _index(source_samples if source_samples is not None else sources_from_report(report), "Source samples")
    critical, mismatches, field_mismatches, details = [], [], [], []
    for kind, values in (("DUPLICATE_SAMPLE_RESULTS", duplicates), ("DUPLICATE_EXTRACTED_FACTS", duplicate_facts), ("DUPLICATE_SOURCE_SAMPLES", duplicate_sources)):
        if values:
            critical.append({"code": kind, "samples": values})
    missing, unexpected = sorted(set(expected) - set(results)), sorted(set(results) - set(expected)) if expected else []
    if missing:
        critical.append({"code": "MISSING_EXPECTED_SAMPLES", "samples": missing})
    if unexpected:
        critical.append({"code": "UNEXPECTED_UNGRADED_SAMPLES", "samples": unexpected})
    counts = {key: Counter() for key in CONTROLS}
    confusion = {key: {s: {a: 0 for a in STATUSES + ("MISSING_OR_INVALID",)} for s in STATUSES} for key in CONTROLS}
    matched_fields, total_fields, checked_citations, invalid_citations = 0, 0, 0, 0
    workbook_matched, workbook_total, workbook_mismatches, verified_samples = 0, 0, [], 0
    for sample_id in sorted(set(expected) | set(results)):
        result = results.get(sample_id, {})
        facts = extracted.get(sample_id, {}).get("facts", {})
        label = expected.get(sample_id)
        case_mismatches, case_fields = [], []
        for control in CONTROLS:
            check = result.get("checks", {}).get(control, {})
            actual = check.get("status", "MISSING_OR_INVALID")
            if actual not in STATUSES:
                actual = "MISSING_OR_INVALID"
            if actual in STATUSES:
                symbol = {"SUPPORTED": "X", "NOT_APPLICABLE": "-", "OPEN": "OPEN"}[actual]
                if check.get("symbol") != symbol or result.get("values", {}).get(control) != symbol:
                    critical.append({"code": "CONTROL_SYMBOL_INCONSISTENT", "sample_id": sample_id, "control": control})
            counts[control]["actual_" + actual] += 1
            if not label:
                continue
            wanted = label["expected_checks"][control]
            confusion[control][wanted][actual] += 1
            counts[control]["graded"] += 1
            counts[control]["correct"] += actual == wanted
            counts[control]["expected_not_supported"] += wanted != "SUPPORTED"
            counts[control]["expected_supported"] += wanted == "SUPPORTED"
            counts[control]["graded_actual_supported"] += actual == "SUPPORTED"
            if actual != wanted:
                entry = {"sample_id": sample_id, "control": control, "expected": wanted, "actual": actual}
                case_mismatches.append(entry); mismatches.append(entry)
                if actual == "SUPPORTED":
                    counts[control]["false_supported"] += 1
                    critical.append({"code": "FALSE_SUPPORTED", **entry})
                elif actual == "NOT_APPLICABLE":
                    critical.append({"code": "FALSE_NOT_APPLICABLE", **entry})
        if label:
            for group, wanted in label.get("expected_record_counts", {}).items():
                records = facts.get(group, [])
                actual = len(records) if isinstance(records, list) else None
                if actual != wanted:
                    entry = {"sample_id": sample_id, "field": group + ".record_count", "expected": wanted, "actual": actual}
                    field_mismatches.append(entry); case_fields.append(entry)
                    critical.append({"code": "EXTRACTION_RECORD_COUNT_MISMATCH", **entry})
            for field, wanted in label.get("expected_values", {}).items():
                workbook_total += 1
                actual = result.get("values", {}).get(field)
                if _equal(wanted, actual, "values." + field):
                    workbook_matched += 1
                else:
                    entry = {"sample_id": sample_id, "field": field, "expected": wanted, "actual": actual}
                    workbook_mismatches.append(entry)
                    critical.append({"code": "WRONG_WORKBOOK_VALUE", **entry})
            aligned = _aligned_field_paths(label, facts)
            for field, wanted in label.get("expected_fields", {}).items():
                actual = field_at(facts, aligned[field])
                total_fields += 1
                if _equal(wanted, actual, field):
                    matched_fields += 1
                else:
                    entry = {"sample_id": sample_id, "field": field, "expected": wanted, "actual": actual}
                    case_fields.append(entry); field_mismatches.append(entry)
        audit = citation_audit(result, facts, sources.get(sample_id))
        checked_citations += audit["checked"]
        invalid_citations += len({entry["field"] for entry in audit["invalid"]})
        verified_samples += audit["status"] in {"PASS", "FAIL"}
        if audit["invalid"] or audit["supported_without_valid_citations"]:
            critical.append({"code": "CITATION_AUDIT_FAILED", "sample_id": sample_id,
                             "invalid_count": len(audit["invalid"]), "controls": audit["supported_without_valid_citations"]})
        if audit["status"].startswith("NOT_VERIFIED") and any(result.get("checks", {}).get(k, {}).get("status") in {"SUPPORTED", "NOT_APPLICABLE"} for k in CONTROLS):
            critical.append({"code": "SOURCE_PAGES_REQUIRED_FOR_ACCEPTED_CONTROLS", "sample_id": sample_id})
        details.append({"sample_id": sample_id, "name": label.get("name") if label else None,
                        "split": label.get("split") if label else None, "graded": label is not None,
                        "control_mismatches": case_mismatches, "field_mismatches": case_fields,
                        "citation_audit": audit})
    metrics = {}
    for key, count in counts.items():
        metrics[key] = {**dict(count), "accuracy": _ratio(count["correct"], count["graded"]),
                        "false_supported": count["false_supported"],
                        "false_supported_rate_among_expected_not_supported": _ratio(count["false_supported"], count["expected_not_supported"]),
                        "supported_precision": _ratio(confusion[key]["SUPPORTED"]["SUPPORTED"], count["graded_actual_supported"]),
                        "supported_recall": _ratio(confusion[key]["SUPPORTED"]["SUPPORTED"], count["expected_supported"]),
                        "confusion_expected_rows_actual_columns": confusion[key]}
    check_total = sum(c["graded"] for c in counts.values())
    check_correct = sum(c["correct"] for c in counts.values())
    extraction_metadata = [m for m in report.get("extraction", []) if isinstance(m, dict)]
    calls_known = not any(m.get("model_call_count_known") is False for m in extraction_metadata)
    calls = sum(m.get("model_calls", 0) for m in extraction_metadata if type(m.get("model_calls", 0)) is int)
    status = "NOT_GRADED_NO_EXPECTED_LABELS" if not expected else "PASS" if not critical and not mismatches and not field_mismatches else "FAIL"
    if report.get("ok") is not True or report.get("source_unchanged") is not True:
        critical.append({"code": "RUN_NOT_COMPLETE_OR_SOURCES_CHANGED"})
        status = "FAIL" if expected else status
    return {"schema_version": 1, "evaluation_version": EVALUATION_VERSION,
            "evaluated_at_utc": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "status": status, "operational_audit_status": "PASS" if not critical else "FAIL",
            "dataset_id": gold.get("dataset_id") if gold else None, "run_id": report.get("run_id"),
            "label_provenance": gold.get("provenance") if gold else None,
            "human_validated_gold": human_validated,
            "production_readiness": "NOT_ESTABLISHED_REVIEWER_APPROVAL_AND_REPRESENTATIVE_END_TO_END_EVALUATION_REQUIRED",
            "scope": "Frozen page text; OCR, folder mapping and workbook rendering are excluded." if mode in {"OFFLINE_MOCKED_REGRESSION", "LIVE_MODEL_TEXT_EXTRACTION", "CACHED_MODEL_TEXT_EXTRACTION", "CACHED_MODEL_REGRESSION_AFTER_OBSERVED_FAILURES"} else "Existing report; only attested expected labels are graded. Citation membership uses supplied report page text, not a fresh read of original PDFs.",
            "model_accuracy_claim": "NOT_MEASURED_MOCKED_EXTRACTION" if mode == "OFFLINE_MOCKED_REGRESSION" else "NOT_MEASURED_NO_GOLD" if not expected else "LABELED_REPORT_COMPARISON_REVIEW_PROVENANCE_AND_SCOPE",
            "samples": {"reported": len(results), "expected": len(expected), "graded": len(set(expected) & set(results)),
                        "missing": missing, "unexpected": unexpected},
            "controls": metrics, "check_accuracy": _ratio(check_correct, check_total),
            "fields": {"matched": matched_fields, "graded": total_fields, "exact_accuracy": _ratio(matched_fields, total_fields), "mismatches": field_mismatches},
            "workbook_values": {"matched": workbook_matched, "graded": workbook_total, "exact_accuracy": _ratio(workbook_matched, workbook_total), "mismatches": workbook_mismatches},
            "citations": {"checked": checked_citations, "invalid": invalid_citations,
                          "samples_verified": verified_samples, "samples_total": len(details),
                          "valid_rate": _ratio(checked_citations - min(checked_citations, invalid_citations), checked_citations)},
            "timings": report.get("timings", {}), "model_calls": calls if calls_known else None,
            "known_model_calls_lower_bound": calls, "model_call_count_complete": calls_known,
            "critical_failures": critical, "control_mismatches": mismatches, "cases": details,
            "limitations": ["Synthetic developer labels require independent human validation before an accuracy claim.",
                            "Offline mocked extraction verifies scoring, source validation and criteria regression, not model behavior or prompt-injection resistance.",
                            "A real-sample report without human-validated expected labels is operationally audited but never scored for accuracy.",
                            "No automatic audit sign-off, compliance approval or production-release decision is made."]}


def _synthetic_inputs(dataset_path=None, gold_path=None, rules_path=None, split="all"):
    dataset_path = Path(dataset_path or FIXTURES / "synthetic_cases.json")
    gold_path = Path(gold_path or FIXTURES / "synthetic_expected.json")
    rules_path = Path(rules_path or FIXTURES / "synthetic_rules.json")
    dataset, gold = _load(dataset_path), _load(gold_path)
    _manifest(gold)
    if dataset.get("data_origin") != "synthetic" or gold.get("data_origin") != "synthetic":
        raise ValueError("Built-in regression/live-text harness only accepts explicitly synthetic fixtures.")
    if dataset.get("dataset_id") != gold.get("dataset_id") or _sha(dataset_path) != gold.get("source_dataset_sha256") or _sha(rules_path) != gold.get("rules_sha256"):
        raise ValueError("Dataset, expected-label or frozen-rule identity/hash mismatch.")
    cases, duplicate = _index(dataset.get("cases"), "Synthetic source cases")
    labels, _ = _manifest(gold)
    if duplicate or set(cases) != set(labels):
        raise ValueError("Synthetic sources and expected labels must have exactly matching unique sample IDs.")
    selected = [case for case in cases.values() if split == "all" or case.get("split") == split]
    if not selected:
        raise ValueError("No synthetic cases selected.")
    gold = {**gold, "cases": [labels[str(case["sample_id"])] for case in selected]}
    return selected, gold, load_rules(rules_path)


def run_offline(dataset_path=None, gold_path=None, rules_path=None, split="all"):
    """Execute frozen mocked responses through real source validator/criteria."""
    cases, gold, rules = _synthetic_inputs(dataset_path, gold_path, rules_path, split)
    started, results, extracted = time.monotonic(), [], []
    for case in cases:
        facts = validate_facts(copy.deepcopy(case["mock_raw"]), case["sample"])
        extracted.append({"sample_id": case["sample_id"], "facts": facts})
        results.append(evaluate_sample(case["context"], facts, rules))
    report = {"ok": True, "source_unchanged": True, "status": "DRAFT_REQUIRES_HUMAN_REVIEW", "run_id": "synthetic-offline",
              "results": results, "extracted_facts": extracted, "rules_snapshot": rules,
              "document_inventory": {"samples": [case["sample"] for case in cases]}, "extraction": [],
              "timings": {"offline_regression_seconds": round(time.monotonic() - started, 4)}}
    return score_report(report, gold, mode="OFFLINE_MOCKED_REGRESSION"), report


def evaluate_model(model, *, model_name, cache_dir, dataset_path=None, gold_path=None,
                   rules_path=None, split="held_out", force_refresh=False):
    """Opt-in API for existing approved model; never creates a provider/key.

    Uses the real extraction prompt/schema/quote validator, then deterministic
    checks. Frozen page-text input means this is NOT an OCR/PDF end-to-end eval.
    Calls are bounded by the existing extractor's one-repair limit. Cached
    responses are reported distinctly from new paid extraction requests.
    """
    if model is None or not model_name:
        raise ValueError("An explicitly supplied approved model and model_name are required.")
    cases, gold, rules = _synthetic_inputs(dataset_path, gold_path, rules_path, split)
    started, results, extracted, metadata = time.monotonic(), [], [], []
    failure_type = None
    for case in cases:
        if failure_type is None:
            try:
                facts, meta = extract_sample(case["context"], case["sample"], model, cache_dir, model_name, force_refresh)
            except Exception as exc:
                # Never persist provider exception text: it can contain request
                # payloads, endpoints or secrets. Preserve the failed gate and
                # stop dispatching further paid/model work after one failure.
                failure_type = type(exc).__name__
                facts = {"incoming": [], "statements": [], "returns": [], "errors": ["Model evaluation failed; inspect provider configuration privately."]}
                meta = {"model_calls": 0, "model_call_count_known": False, "failure_type": failure_type}
        else:
            facts = {"incoming": [], "statements": [], "returns": [], "errors": ["Not run after earlier model evaluation failure."]}
            meta = {"model_calls": 0, "not_run_after_model_error": True}
        results.append(evaluate_sample(case["context"], facts, rules))
        extracted.append({"sample_id": case["sample_id"], "facts": facts})
        metadata.append({"sample_id": case["sample_id"], **meta})
    report = {"ok": failure_type is None, "source_unchanged": True, "status": "DRAFT_REQUIRES_HUMAN_REVIEW" if failure_type is None else "EVALUATION_INCOMPLETE", "run_id": "synthetic-model-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "results": results, "extracted_facts": extracted, "extraction": metadata,
              "rules_snapshot": rules, "document_inventory": {"samples": [case["sample"] for case in cases]},
              "timings": {"model_text_evaluation_seconds": round(time.monotonic() - started, 4)}}
    mode = "LIVE_MODEL_TEXT_EXTRACTION" if failure_type is not None or any(m.get("model_calls", 0) for m in metadata) else "CACHED_MODEL_TEXT_EXTRACTION"
    return score_report(report, gold, mode=mode), report


def replay_model_cache(cache_dir, *, model_name, dataset_path=None, gold_path=None,
                       rules_path=None, split="held_out"):
    """Revalidate the exact existing raw model cache without model or repairs.

    Missing/mismatched caches fail explicitly. Every selected signature binds
    the extractor prompt/schema/provider version, model and frozen page payload.
    Observed failed cases used to design fixes are now regression cases, not an
    untouched held-out model-accuracy estimate. Original cache bytes stay intact.
    """
    if not isinstance(model_name, str) or not model_name:
        raise ValueError("An explicit cached model_name is required.")
    cases, gold, rules = _synthetic_inputs(dataset_path, gold_path, rules_path, split)
    cache_dir = Path(cache_dir).resolve()
    if not cache_dir.is_dir():
        raise ValueError("Required model cache directory is missing; no model call or repair was attempted.")
    cached_inputs = []
    for case in cases:
        payload = extractor_module.extraction_payload(case["context"], case["sample"])
        signature = hashlib.sha256((extractor_module.VERSION + extractor_module.SYSTEM +
            json.dumps(extractor_module.SCHEMA, sort_keys=True) + model_name + payload).encode()).hexdigest()
        cache_file = cache_dir / (signature + ".json")
        if not cache_file.is_file():
            raise ValueError("Required model cache is missing for sample " + str(case["sample_id"]) + "; no model call or repair was attempted.")
        before = _sha(cache_file)
        cached = _load(cache_file)
        if cached.get("signature") != signature or cached.get("version") != extractor_module.VERSION or not isinstance(cached.get("raw"), dict):
            raise ValueError("Cached signature/version/raw does not match the frozen evaluation payload for sample " + str(case["sample_id"]) + ".")
        cached_inputs.append((case, cache_file, before, signature, cached["raw"]))
    started, results, extracted, metadata = time.monotonic(), [], [], []
    for case, cache_file, before, signature, raw in cached_inputs:
        facts = validate_facts(copy.deepcopy(raw), case["sample"])
        results.append(evaluate_sample(case["context"], facts, rules))
        extracted.append({"sample_id": case["sample_id"], "facts": facts})
        metadata.append({"sample_id": case["sample_id"], "model": model_name, "model_calls": 0,
                         "cache_hit": True, "cache_revalidation_only": True, "repair_attempted": False,
                         "signature": signature, "cache_sha256": before, "cache_unchanged": _sha(cache_file) == before})
    unchanged = all(m["cache_unchanged"] for m in metadata)
    report = {"ok": unchanged, "source_unchanged": unchanged,
              "status": "DRAFT_REQUIRES_HUMAN_REVIEW" if unchanged else "EVALUATION_SOURCE_CHANGED",
              "run_id": "synthetic-cache-regression-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "results": results, "extracted_facts": extracted, "extraction": metadata,
              "rules_snapshot": rules, "document_inventory": {"samples": [case["sample"] for case in cases]},
              "timings": {"cache_revalidation_seconds": round(time.monotonic() - started, 4)},
              "methodology": "These originally held_out cases informed fixes after initial observed model failures; this is regression after observed failures, not a fresh untouched holdout.",
              "model_calls": 0, "cache_write_performed": False}
    result = score_report(report, gold, mode="CACHED_MODEL_REGRESSION_AFTER_OBSERVED_FAILURES")
    result["limitations"].append(report["methodology"])
    result["cache_unchanged"] = unchanged
    result["cache_write_performed"] = False
    return result, report


def write_results(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--offline", action="store_true", help="Run synthetic mocked regression; zero model calls.")
    source.add_argument("--report", help="Score an existing Review_Report.json, or audit without accuracy if no --gold.")
    source.add_argument("--cache-replay", help="Strictly revalidate existing matching model cache; zero model/repair calls.")
    parser.add_argument("--gold", help="Expected-label JSON; real records require reviewer attestation.")
    parser.add_argument("--sources", help="Optional JSON list of source samples, or synthetic source dataset JSON.")
    parser.add_argument("--split", choices=("all", "development", "held_out"), default="all")
    parser.add_argument("--model-name", default="gpt-4o-mini", help="Model identity used to bind --cache-replay signatures.")
    parser.add_argument("--output", default=str(FIXTURES / "results" / "latest_evaluation.json"))
    parser.add_argument("--write-report", help="Save synthetic offline Review_Report to this new output path.")
    parser.add_argument("--label-template", help="Write an unfilled reviewer label template for the supplied --report; no conclusions are copied.")
    args = parser.parse_args(argv)
    if not args.report and args.label_template:
        parser.error("--label-template requires --report.")
    if args.report and args.write_report:
        parser.error("--write-report requires --offline or --cache-replay.")
    inputs = {p.resolve() for p in (FIXTURES / "synthetic_cases.json", FIXTURES / "synthetic_expected.json", FIXTURES / "synthetic_rules.json")}
    inputs.update(Path(p).resolve() for p in (args.report, args.gold, args.sources) if p)
    outputs = [Path(p).resolve() for p in (args.output, args.write_report, args.label_template) if p]
    if any(p in inputs for p in outputs) or len(outputs) != len(set(outputs)):
        parser.error("Evaluation outputs must be separate from source reports, labels, fixtures and each other.")
    if args.cache_replay and any(p.is_relative_to(Path(args.cache_replay).resolve()) for p in outputs):
        parser.error("Cache replay outputs must be outside the read-only cache directory.")
    if args.offline or args.cache_replay:
        if args.cache_replay:
            result, report = replay_model_cache(args.cache_replay, model_name=args.model_name, gold_path=args.gold, split=args.split)
        else:
            result, report = run_offline(gold_path=args.gold, split=args.split)
        if args.write_report:
            write_results(args.write_report, report)
    else:
        report, gold = _load(args.report), _load(args.gold) if args.gold else None
        if args.label_template:
            write_results(args.label_template, label_template(report))
        samples = None
        if args.sources:
            source_data = _load(args.sources)
            samples = [case["sample"] for case in source_data["cases"]] if isinstance(source_data, dict) and "cases" in source_data else source_data
        result = score_report(report, gold, samples)
    output = write_results(args.output, result)
    print(json.dumps({"status": result["status"], "operational_audit_status": result["operational_audit_status"],
                      "mode": result["mode"], "samples": result["samples"], "check_accuracy": result["check_accuracy"],
                      "field_accuracy": result["fields"]["exact_accuracy"], "critical_failures": len(result["critical_failures"]),
                      "model_calls": result["model_calls"], "output": output.as_posix()}, indent=2))
    return 1 if result["status"] == "FAIL" or result["operational_audit_status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
