"""Pure reviewer-criteria evaluation of newly extracted, cited facts.

No frozen observations, model calls or workbook writes occur here. The caller
must validate each quote against the supplied page text before calling this
module. This layer validates evidence shape and applies only the supplied rules.
Money uses Decimal internally; JSON-facing money values are decimal strings.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DEFAULT_RULES = Path(__file__).parent / "rules" / "user_confirmed_2025.json"
CONTROL_KEYS = ("fee_charged", "travel", "ofac", "posting")
SCOPED_FIELDS = {
    "incoming": {"principal_amount", "currency", "fee", "wire_number", "effective_date", "posted_date",
                 "beneficiary_name", "beneficiary_account", "beneficiary_address", "originator_name",
                 "originator_account", "originator_address", "sending_fi", "receiving_fi", "ofac_status",
                 "ofac_approved", "ofac_reason", "myadvantage", "classification", "account_correction", "returned"},
    "statement": {"wire_number", "amount", "date", "account_suffix", "account_name", "kind", "direction", "description", "currency"},
    "return": {"wire_number", "amount", "date", "description"},
}
EMPTY_TEXT = {"", "-", "?", "n/a", "na", "none", "null", "nan", "unknown", "not provided", "not available", "unavailable", "not applicable", "missing", "redacted", "[redacted]", "unreadable", "illegible"}


def load_rules(path: str | Path | None = None) -> dict:
    """Read an explicitly selected local rule file; defaults are reviewer criteria."""
    rules = json.loads(Path(path or DEFAULT_RULES).read_text(encoding="utf-8-sig"))
    error = _rules_error(rules)
    if error:
        raise ValueError(error)
    return rules


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." not in text:
        return text + ".00"
    return text + "0" * max(0, 2 - len(text.split(".")[1]))


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip().casefold() in EMPTY_TEXT:
        return None
    return value.strip()


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return date.fromisoformat(value.strip())
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value.strip()):
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        pass
    return None


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else None
    return _text(value)


def _account(value: Any) -> str | None:
    text = _identifier(value)
    if text is None or re.search(r"\d(?:\.\d+)?[Ee][+-]?\d+", text):
        return None
    # Formatting separators may be removed; missing/rounded digits are not rebuilt.
    return re.sub(r"[\s-]", "", text) if re.fullmatch(r"[\d\s-]+", text) else text


def _suffix(value: Any, length: int = 4) -> str | None:
    text = _identifier(value)
    if text is None:
        return None
    match = re.fullmatch(r"[*xX\s-]*(\d{" + str(length) + r"})", text)
    return match.group(1) if match else None


def _minor_name_match(left: Any, right: Any, allow_minor: bool = True) -> bool:
    left, right = _text(left), _text(right)
    if not left or not right:
        return False

    def parties(value: str):
        result = []
        for party in re.split(r"\s+AND\s+|\s*&\s*|[\r\n]+", value.upper()):
            parts = re.sub(r"[^\w\s]", " ", party, flags=re.UNICODE).replace("_", " ").split()
            if allow_minor:
                parts = [p for i, p in enumerate(parts) if not (0 < i < len(parts) - 1 and len(p) == 1)]
            if not parts:
                return None
            result.append(tuple(parts))
        return sorted(result)

    return parties(left) is not None and parties(left) == parties(right)


class Evidence:
    def __init__(self, uncertainties=()):
        self.items: dict[str, dict] = {}
        self.invalid: set[str] = set()
        self.uncertainties = uncertainties

    def uncertain(self, prefix: str, field: str, item: dict | None = None) -> bool:
        """Never use a rejected field merely because a caller retained its value."""
        match = re.fullmatch(r"(incoming|statements|returns)\[(\d+)\]", prefix)
        if not match:
            return True
        role = {"incoming": "incoming", "statements": "statement", "returns": "return"}[match[1]]
        for issue in self.uncertainties:
            if issue["scope"] == "global" or issue.get("role") != role:
                continue
            if issue["scope"] == "field":
                if issue.get("record_index") is not None and issue["record_index"] != int(match[2]):
                    continue
                if issue.get("field") == field:
                    return True
            elif issue["scope"] in {"document", "page"}:
                if issue.get("source_file") and (not item or item.get("source_file") != issue["source_file"]):
                    continue
                if issue["scope"] == "page" and (not item or item.get("page") != issue.get("page")):
                    continue
                return True
        return False

    def get(self, record: dict, field: str, prefix: str, converter=None):
        key = prefix + "." + field
        item = record.get(field) if isinstance(record, dict) else None
        if not isinstance(item, dict):
            self.invalid.add(key)
            return None
        if self.uncertain(prefix, field, item):
            self.invalid.add(key)
            return None
        if not (_text(item.get("source_file")) and type(item.get("page")) is int and item["page"] > 0
                and _text(item.get("quote"))):
            self.invalid.add(key)
            return None
        self.items[key] = {"id": key, "value": item.get("value"), "source_file": item["source_file"],
                           "page": item["page"], "quote": item["quote"]}
        value = item.get("value")
        if converter:
            value = converter(value)
        if value is None:
            self.invalid.add(key)
        return value

    def ids(self, prefix: str, fields=None):
        return [k for k in self.items if k.startswith(prefix + ".") and
                (fields is None or k.rsplit(".", 1)[-1] in fields)]


def _diagnostic_issues(facts: dict) -> list[str]:
    issues = []
    error_messages = []
    for key in ("errors", "extraction_errors"):
        value = facts.get(key)
        if not value:
            continue
        entries = value if isinstance(value, (list, tuple)) else [value]
        for entry in entries:
            message = re.sub(r"\s+", " ", entry).strip() if isinstance(entry, str) else ""
            error_messages.append(message or "Extraction reports errors.")
    if error_messages:
        reason = "Extraction errors: " + "; ".join(error_messages[:3])
        issues.append(reason if len(reason) <= 250 else reason[:247].rstrip() + "...")
    for key in ("unread_pages", "ambiguous_pages", "unread", "ambiguous"):
        if facts.get(key):
            issues.append("Extraction reports " + key.replace("_", " ") + ".")
    diagnostics = facts.get("diagnostics")
    if isinstance(diagnostics, dict):
        for key, value in diagnostics.items():
            if value and key.lower() not in {"info", "information", "debug", "notes"}:
                issues.append("Extraction has unresolved diagnostics.")
                break
    elif isinstance(diagnostics, list):
        if any(not isinstance(v, dict) or str(v.get("severity", v.get("level", "error"))).lower()
               not in {"info", "information", "debug"} for v in diagnostics):
            issues.append("Extraction has unresolved diagnostics.")
    elif diagnostics:
        issues.append("Extraction has unresolved diagnostics.")
    return issues


def _scoped_diagnostics(facts: dict) -> tuple[list[str], list[dict]]:
    """Only explicitly attributed diagnostics may bypass a global source gate.

    Legacy and unknown messages retain their previous fail-closed behavior.
    A structured record is attribution, never permission to ignore uncertainty.
    """
    clean, global_messages = [], []
    entries = facts.get("uncertainties", [])
    if not isinstance(entries, list):
        return ["Extraction uncertainty records have an invalid shape."], []
    for entry in entries:
        if not isinstance(entry, dict) or not _text(entry.get("message")):
            global_messages.append("An extraction uncertainty has an invalid shape.")
            continue
        entry = dict(entry)
        entry["message"] = re.sub(r"\s+", " ", entry["message"]).strip()
        scope, role = entry.get("scope"), entry.get("role")
        valid = scope in ("global", "document", "page", "field")
        if scope != "global":
            valid = valid and role in ("incoming", "statement", "return")
        if scope == "page":
            valid = valid and bool(_text(entry.get("source_file"))) and type(entry.get("page")) is int and entry["page"] > 0
        if scope == "field":
            valid = valid and bool(_text(entry.get("field"))) and entry["field"] in SCOPED_FIELDS.get(role, set()) and (entry.get("record_index") is None or
                    type(entry["record_index"]) is int and entry["record_index"] >= 0)
            if valid and entry.get("record_index") is not None:
                group = {"incoming": "incoming", "statement": "statements", "return": "returns"}[role]
                records = facts.get(group)
                valid = isinstance(records, list) and entry["record_index"] < len(records)
        if not valid:
            global_messages.append("An extraction uncertainty cannot be attributed safely: " + entry["message"])
            continue
        clean.append(entry)
        if scope == "global":
            global_messages.append(entry["message"])
    attributed = {entry["message"] for entry in clean}
    for key in ("errors", "extraction_errors"):
        value = facts.get(key, [])
        values = value if isinstance(value, (list, tuple)) else [value] if value else []
        for value in values:
            message = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else "Extraction reports errors."
            if message and message not in attributed:
                global_messages.append(message)
    # Old unscoped page/diagnostic containers cannot be cleared by a new field
    # attribution. Keep these global until their producer supplies precise scope.
    legacy = {key: value for key, value in facts.items()
              if key not in {"errors", "extraction_errors", "uncertainties"}}
    global_messages += _diagnostic_issues(legacy)
    return list(dict.fromkeys(global_messages)), clean


def _role_complete(facts: dict, role: str, uncertainties: list[dict]) -> bool:
    coverage = facts.get("coverage")
    complete = coverage.get(role) is True if isinstance(coverage, dict) else facts.get("inspected_all_pages") is True
    return complete and not any(issue["scope"] == "global" or
            issue.get("role") == role and issue["scope"] in {"document", "page"} for issue in uncertainties)


def _uncertainty_reason(issues: list[dict]) -> str:
    return "Source uncertainty: " + "; ".join(dict.fromkeys(issue["message"] for issue in issues))


def _rules_error(rules: Any) -> str | None:
    try:
        if not isinstance(rules, dict) or type(rules.get("schema_version")) is not int or rules.get("schema_version") != 1 or rules.get("status") != "reviewer_confirmed":
            return "A supported, reviewer-confirmed rule set is required."
        if not _text(rules.get("rule_set_id")):
            return "A versioned rule-set identifier is required for the review report."
        if rules["provenance"]["source_type"] != "reviewer_supplied":
            return "Rule provenance must identify reviewer-supplied criteria."
        start, end = _date(rules["effective_from"]), _date(rules["effective_to"])
        if not start or not end or start > end:
            return "The criterion effective period is missing or invalid."
        if rules["currency"] != "USD":
            return "Only explicit USD reviewer criteria are supported by this evaluator version."
        fee, travel, posting, ofac = (rules[k] for k in ("fee", "travel", "posting", "ofac"))
        if any(_decimal(v) is None or _decimal(v) < 0 for v in (fee["domestic_amount"], fee["myadvantage_expected_fee"], travel["threshold"], posting["amount_tolerance"])):
            return "A criterion amount is invalid."
        if _decimal(fee["domestic_amount"]) <= 0 or _decimal(travel["threshold"]) <= 0 or _decimal(fee["myadvantage_expected_fee"]) != 0:
            return "Standard fee and Travel threshold must be positive; the supported waiver is a zero fee."
        if fee["foreign_amount"] is not None:
            return "Foreign fee evaluation is not implemented for this rule schema."
        if fee["separate_statement_debit_required"] is not True or fee["waived_fee_charged_result"] != "NOT_APPLICABLE":
            return "The requested fee-evidence behavior is unsupported."
        if type(fee["accept_incoming_myadvantage_designation"]) is not bool:
            return "The waiver criterion must be explicit."
        if travel["operator"] not in {">=", ">"}:
            return "The Travel threshold operator is unsupported."
        required = {"originator_name", "originator_account", "originator_address", "beneficiary_name", "beneficiary_account", "beneficiary_address", "sending_fi", "receiving_fi"}
        if set(travel["required_fields"]) != required or len(travel["required_fields"]) != len(required):
            return "The Travel field specification is unsupported."
        if ofac["required_for_all"] is not True or ofac["false_positive_requires_approval_and_reason"] is not True:
            return "The OFAC criterion is unsupported."
        if not all(isinstance(ofac[k], list) and ofac[k] and all(_text(v) for v in ofac[k]) for k in ("passed_statuses", "false_positive_statuses")):
            return "The accepted OFAC statuses are missing."
        if posting["account_suffix_length"] != 4 or type(posting["allow_minor_name_variations"]) is not bool:
            return "The account/name comparison criterion is unsupported."
        if any(posting[k] is not True for k in ("account_corrections_require_manual_review", "date_variances_require_manual_review", "returns_require_manual_review")):
            return "Automatic resolution of corrections, date variances or returns is unsupported."
        if rules["symbols"] != {"supported": "X", "not_applicable": "-", "manual_review": "OPEN"}:
            return "The workbook result symbols are unsupported."
    except (KeyError, TypeError, ValueError):
        return "The rule set is incomplete or invalid."
    return None


def _check(status: str, reason: str, evidence=()):
    return {"status": status, "symbol": {"SUPPORTED": "X", "NOT_APPLICABLE": "-", "OPEN": "OPEN"}[status],
            "reason": reason, "evidence": list(dict.fromkeys(evidence))}


def _flag(record: dict, key: str, ev: Evidence, complete: bool) -> bool | None:
    # Negative absence is a completeness conclusion; no fictitious quote is required.
    if record.get(key) is None or (isinstance(record[key], dict) and record[key].get("value") is None):
        return False if complete else None
    return ev.get(record, key, "incoming[0]", lambda value: value if type(value) is bool else None)


def _kind(record: dict, prefix: str, ev: Evidence):
    value = record.get("kind")
    if isinstance(value, dict):
        value = ev.get(record, "kind", prefix, _text)
    elif isinstance(value, str) and value in {"principal", "fee"}:
        # A parser-derived label still needs a cited transaction description.
        if not ev.get(record, "description", prefix, _text):
            return None
    else:
        return None
    return value if value in {"principal", "fee"} else None


def _debit(record: dict, prefix: str, amount: Decimal, ev: Evidence) -> bool:
    direction_item = record.get("direction")
    if direction_item is not None and not (isinstance(direction_item, dict) and direction_item.get("value") is None):
        direction = ev.get(record, "direction", prefix, _text)
        return amount != 0 and direction is not None and direction.casefold() == "debit"
    amount_evidence = ev.items.get(prefix + ".amount", {})
    if amount < 0:
        # Do not let a claimed negative sign turn an unsigned or credited fee
        # into a debit. The parser also validates sign against the source.
        pattern = r"(?<![\w.])(?:[-\u2212]\s*(?:USD\s*)?\$?\s*([\d,]+(?:\.\d+)?)|\(\s*(?:USD\s*)?\$?\s*([\d,]+(?:\.\d+)?)\s*\))"
        for match in re.finditer(pattern, amount_evidence.get("quote", ""), re.I):
            text = next(part for part in match.groups() if part is not None)
            if _decimal(text.replace(",", "")) == abs(amount):
                return True
    if amount == 0:
        return False
    quoted = []
    for key in ("kind", "amount", "description"):
        item = ev.items.get(prefix + "." + key)
        if item:
            quoted.append(item["quote"])
            if key in {"kind", "description"} and isinstance(item["value"], str):
                quoted.append(item["value"])
    text = " ".join(quoted).casefold()
    # Positive debit-column amounts are valid; plain 'fee' does not establish sign.
    return bool(re.search(r"\b(?:debit(?:s|ed)?|withdrawals?)\b", text)) and not bool(re.search(r"\b(?:credit|refund|reversal)\b", text))


def _credit(record: dict, prefix: str, amount: Decimal | None, ev: Evidence) -> bool:
    """A positive unsigned debit-column number is not a principal credit."""
    if amount is None or amount <= 0:
        return False
    direction_item = record.get("direction")
    if direction_item is not None and not (isinstance(direction_item, dict) and direction_item.get("value") is None):
        direction = ev.get(record, "direction", prefix, _text)
        return direction is not None and direction.casefold() == "credit"
    quoted = " ".join(ev.items[prefix + "." + key]["quote"] for key in ("kind", "amount", "description")
                      if prefix + "." + key in ev.items).casefold()
    if re.search(r"\b(?:debits?|withdrawals?|outgoing|reversal)\b", quoted):
        return False
    if re.search(r"\b(?:credits?|credited|deposits?)\b", quoted):
        return True
    quote = ev.items.get(prefix + ".amount", {}).get("quote", "")
    for match in re.finditer(r"(?<![\w.])\+\s*(?:USD\s*)?\$?\s*([\d,]+(?:\.\d+)?)", quote, re.I):
        if _decimal(match.group(1).replace(",", "")) == amount:
            return True
    return False


def _workbook_comment(checks: dict, ev: Evidence) -> str:
    """Show every control and its cause; detailed diagnostics stay in the report."""
    labels = {"fee_charged": "Fee", "travel": "Travel", "ofac": "OFAC", "posting": "Posting"}
    gates = [key for key in ("source", "criteria", "context", "period") if checks.get(key, {}).get("status") == "OPEN"]

    def essential(key):
        reason = checks.get(key, {}).get("reason", "")
        lower = reason.casefold()
        if key == "source": return "source missing/ambiguous/unverified"
        if key == "criteria": return "confirmed criteria unavailable"
        if key == "context":
            unavailable, mismatched = [], []
            for part in lower.split(";"):
                fields = [name for name in ("principal", "name", "account", "date", "row") if name in part]
                target = mismatched if "differs" in part or "mismatch" in part else unavailable
                target += fields
            parts = []
            for fields, label in ((unavailable, "unverified"), (mismatched, "mismatch")):
                if fields: parts.append("input " + "/".join(dict.fromkeys(fields)) + " " + label)
            return "; ".join(parts) or "input identity unverified"
        if key == "period": return "date/rule period unresolved"
        if "source uncertainty:" in lower:
            pages = list(dict.fromkeys(str(issue.get("page")) for issue in ev.uncertainties
                         if issue.get("role") == "statement" and issue.get("page")))
            return "statement " + ("p." + "/".join(pages[:2]) + " " if pages else "") + "unverified"
        if key == "fee_charged":
            if "currency" in lower or "foreign" in lower: return "fee/currency rule unavailable"
            if "return" in lower: return "return needs review"
            if "exact incoming fee" in lower: return "incoming fee missing"
            if "standard charge" in lower: return "fee/waiver inconsistent"
            if "principal transaction" in lower: return "principal link unverified"
            return "separate debit unverified"
        if key == "travel":
            if "fields:" in reason:
                names = reason.split("fields:", 1)[1].strip(" .").split(", ")
                if len(names) == 1: return names[0] + " missing"
                categories = []
                for party in ("originator", "beneficiary"):
                    if any(name.startswith(party + "_") for name in names): categories.append(party + " data")
                if any(name.endswith("_fi") for name in names): categories.append("FI data")
                return "/".join(categories or ["required fields"]) + " missing"
            return "threshold currency unresolved"
        if key == "ofac": return "status/approval/reason unverified"
        if key == "posting":
            terms = []
            for token, label in (("principal", "principal"), ("date", "date"), ("suffix", "suffix"),
                                 ("credit direction", "credit"), ("name", "name"), ("correction", "correction"), ("return", "return"), ("currency", "currency")):
                if token in lower: terms.append(label)
            return "/".join(terms or ["linkage"]) + " unverified"
        return "required evidence unverified"

    summaries = []
    for key in CONTROL_KEYS:
        item = checks[key]
        text = labels[key] + " " + item["symbol"]
        if item["status"] == "OPEN" and not gates:
            text += " (" + essential(key) + ")"
        elif item["status"] == "NOT_APPLICABLE":
            text += " (waiver)" if key == "fee_charged" else " (below threshold)"
        summaries.append(text)
    core = "DRAFT. " + "; ".join(summaries) + "."
    if gates:
        core += " " + "; ".join(essential(key) for key in gates) + "."
    elif checks.get("classification", {}).get("status") == "OPEN":
        core += " Classification unresolved."
    if checks.get("source_diagnostics", {}).get("status") == "OPEN" and all(checks[k]["status"] != "OPEN" for k in CONTROL_KEYS):
        core += " Source warning."
    refs = []
    for role, prefix in (("incoming", "incoming["), ("statement", "statements["), ("return", "returns[")):
        pages = sorted({item["page"] for key, item in ev.items.items() if key.startswith(prefix)})
        if pages: refs.append(role + " p." + "/".join(map(str, pages[:2])))
    suffix = " Report has details."
    citation = " Refs: " + "; ".join(refs) + "." if refs else ""
    # Drop only duplicated citations to fit. Distinct control reasons survive.
    return core + (citation if len(core + citation + suffix) <= 260 else "") + suffix


def evaluate_sample(context: dict, facts: dict, rules: dict | None = None) -> dict:
    """Evaluate one workbook sample against generic extracted records.

    facts has incoming/statements/returns lists. Scalar source fields use
    {value, source_file, page, quote}; statement kind may instead be a parser enum
    with an evidenced description. Inspected-all-pages completeness permits
    absence conclusions only for correction/return flags. Missing matching facts
    never become positive evidence. The caller owns quote/source verification.
    """
    context = context if isinstance(context, dict) else {}
    facts = facts if isinstance(facts, dict) else {}
    diagnostics, uncertainties = _scoped_diagnostics(facts)
    ev, checks = Evidence(uncertainties), {}
    values = {"fee": None, "fee_charged": "OPEN", "classification": "OPEN", "travel": "OPEN", "ofac": "OPEN", "posting": "OPEN", "comments": ""}

    def finish():
        for key in CONTROL_KEYS:
            if key not in checks:
                checks[key] = _check("OPEN", "Required source evidence or applicable criteria are unavailable.")
            values[key] = checks[key]["symbol"]
        if uncertainties and "source" not in checks:
            checks["source_diagnostics"] = _check("OPEN", _uncertainty_reason(uncertainties))
        values["comments"] = _workbook_comment(checks, ev)
        return {"sample_id": context.get("sample_id"), "row": context.get("row"), "values": values,
                "checks": checks, "evidence": list(ev.items.values()),
                "manual_review": any(item["status"] == "OPEN" for item in checks.values()),
                "criteria_id": rules.get("rule_set_id") if isinstance(rules, dict) else None,
                "reviewer_signoff": "NOT_RECORDED"}

    try:
        rules = load_rules() if rules is None else rules
    except (OSError, ValueError, TypeError):
        rules = None
    rule_error = _rules_error(rules)
    incoming = facts.get("incoming")
    if not isinstance(incoming, list) or len(incoming) != 1 or not isinstance(incoming[0], dict):
        checks["source"] = _check("OPEN", "Exactly one unambiguous incoming record is required.")
        return finish()
    inc = incoming[0]
    get = lambda key, cast=None: ev.get(inc, key, "incoming[0]", cast)
    amount, fee = get("principal_amount", _decimal), get("fee", _decimal)
    values["fee"] = _money(fee)
    classification = get("classification", _text)
    class_key = re.sub(r"[^a-z]", "", classification.casefold()) if classification else ""
    classification = {"domestic": "Domestic", "domesticincomingwire": "Domestic", "foreign": "Foreign",
                      "foreignincomingwire": "Foreign", "international": "Foreign", "internationalincomingwire": "Foreign"}.get(class_key)
    if classification:
        values["classification"] = classification
    checks["classification"] = _check("SUPPORTED" if classification else "OPEN", "Classification is supported by the incoming record." if classification else "Incoming classification is missing or unsupported.", ev.ids("incoming[0]", {"classification"}))
    if rule_error:
        checks["criteria"] = _check("OPEN", rule_error)
        return finish()
    incoming_source_issues = [issue for issue in uncertainties if issue.get("role") == "incoming" and issue["scope"] in {"document", "page"}]
    if isinstance(facts.get("coverage"), dict) and facts["coverage"].get("incoming") is not True:
        diagnostics.append("Incoming source coverage is incomplete; identity and exceptions require source verification.")
    if diagnostics or incoming_source_issues:
        reasons = diagnostics + ([_uncertainty_reason(incoming_source_issues)] if incoming_source_issues else [])
        checks["source"] = _check("OPEN", " ".join(reasons))
        return finish()
    if not isinstance(facts.get("statements", []), list) or not isinstance(facts.get("returns", []), list):
        checks["source"] = _check("OPEN", "Statement/return record collections are invalid.")
        return finish()
    statements = facts.get("statements", [])
    if any(not isinstance(record, dict) for record in statements + facts.get("returns", [])):
        checks["source"] = _check("OPEN", "A transaction record has an invalid shape.")
        return finish()
    complete = _role_complete(facts, "incoming", uncertainties)
    currency = get("currency", _text)
    posted, effective = get("posted_date", _date), get("effective_date", _date)
    wire, account, name = get("wire_number", _identifier), get("beneficiary_account", _account), get("beneficiary_name", _text)
    ctx_date = _date(context.get("date_time") or context.get("date"))
    ctx_amount = _decimal(context.get("amount_decimal", context.get("amount")))
    ctx_name = _text(context.get("customer", context.get("beneficiary_name")))
    ctx_account = _account(context.get("account", context.get("beneficiary_account")))
    context_issues = []
    if type(context.get("row")) is not int or context["row"] < 1 or context.get("sample_id") is None:
        context_issues.append("sample ID/row is missing or invalid")
    if amount is None or amount <= 0 or ctx_amount is None:
        context_issues.append("workbook/incoming principal is unavailable or invalid")
    elif ctx_amount != amount:
        context_issues.append("workbook/incoming principal differs")
    if not name or not ctx_name:
        context_issues.append("workbook/incoming name is unavailable")
    elif not _minor_name_match(ctx_name, name, rules["posting"]["allow_minor_name_variations"]):
        context_issues.append("workbook/incoming name differs")
    if not account or not ctx_account:
        context_issues.append("workbook/incoming account is unavailable or imprecise")
    elif account != ctx_account:
        context_issues.append("workbook/incoming account differs")
    if not posted or not ctx_date:
        context_issues.append("workbook/incoming posted date is unavailable")
    elif posted != ctx_date:
        context_issues.append("workbook/incoming posted date differs")
    checks["context"] = _check("OPEN" if context_issues else "SUPPORTED", "; ".join(context_issues) + "." if context_issues else "Workbook inputs agree with the incoming record.", ev.ids("incoming[0]", {"principal_amount", "beneficiary_name", "beneficiary_account", "posted_date"}))
    period_ok = posted is not None and effective is not None and all(_date(rules["effective_from"]) <= day <= _date(rules["effective_to"]) for day in (posted, effective))
    checks["period"] = _check("SUPPORTED" if period_ok else "OPEN", "Both source dates fall within the confirmed criterion period." if period_ok else "Source effective/posted dates are missing or outside the confirmed criterion period; new applicable criteria are required.", ev.ids("incoming[0]", {"posted_date", "effective_date"}))
    if context_issues or not period_ok:
        return finish()
    correction, returned = _flag(inc, "account_correction", ev, complete), _flag(inc, "returned", ev, complete)
    # A rejected absence flag remains unknown even if the incoming PDF's other
    # pages are complete. A return document problem also prevents assuming none.
    if ev.uncertain("incoming[0]", "account_correction", inc.get("account_correction")):
        correction = None
    if ev.uncertain("incoming[0]", "returned", inc.get("returned")) or any(issue.get("role") == "return" for issue in uncertainties):
        returned = None
    if facts.get("returns"):
        returned = True
        for index, record in enumerate(facts["returns"]):
            for key in record:
                if isinstance(record[key], dict):
                    ev.get(record, key, f"returns[{index}]")
    checks["return_handling"] = _check("SUPPORTED" if returned is False else "OPEN", "No return is identified in the inspected source set." if returned is False else "A return is identified or absence cannot be established; manual handling is required.", ev.ids("incoming[0]", {"returned"}) + [k for k in ev.items if k.startswith("returns[")])
    checks["correction_handling"] = _check("SUPPORTED" if correction is False else "OPEN", "No correction is identified in the inspected incoming pages." if correction is False else "An account correction is identified or absence cannot be established; manual handling is required.", ev.ids("incoming[0]", {"account_correction"}))

    # Select by evidenced wire reference before comparing all other fields.
    candidates, kinds = [], {}
    for index, record in enumerate(statements):
        prefix = f"statements[{index}]"
        kinds[index] = _kind(record, prefix, ev)
        if kinds[index] == "principal":
            ref = ev.get(record, "wire_number", prefix, _identifier)
            if wire is not None and ref == wire:
                candidates.append((index, record))
    principal = None
    principal_ids, principal_issues = [], []
    if wire is None or len(candidates) != 1:
        principal_issues.append("unique evidenced principal with the incoming wire reference is not established")
    else:
        index, record = candidates[0]
        prefix = f"statements[{index}]"
        principal = {"index": index, "amount": ev.get(record, "amount", prefix, _decimal),
                     "date": ev.get(record, "date", prefix, _date),
                     "suffix": ev.get(record, "account_suffix", prefix, _suffix),
                     "name": ev.get(record, "account_name", prefix, _text)}
        ev.get(record, "description", prefix, _text)
        if principal["amount"] is None or principal["amount"] <= 0 or abs(principal["amount"] - amount) > _decimal(rules["posting"]["amount_tolerance"]):
            principal_issues.append("principal amount is missing or different")
        if not _credit(record, prefix, principal["amount"], ev):
            principal_issues.append("principal credit direction is missing or conflicting")
        if principal["date"] is None or principal["date"] != posted:
            principal_issues.append("statement date is missing or different")
        if not re.fullmatch(r"\d{4,}", account) or principal["suffix"] is None or principal["suffix"] != account[-4:]:
            principal_issues.append("masked statement account suffix is missing or different")
        if not _minor_name_match(name, principal["name"], rules["posting"]["allow_minor_name_variations"]):
            principal_issues.append("statement name is missing or outside accepted minor variations")
        if "currency" in record and ev.get(record, "currency", prefix, _text) != currency:
            principal_issues.append("statement/incoming currencies are unresolved or different")
        principal_ids = ev.ids(prefix)
    principal_ok = not principal_issues
    checks["principal_link"] = _check("SUPPORTED" if principal_ok else "OPEN", "; ".join(principal_issues) + "." if principal_issues else "Unique principal matches reference, amount, date, masked suffix and accepted name comparison; full account equality is not inferred.", principal_ids)

    fee_reason, fee_status, fee_ids = "Fee evidence is incomplete; manual review is required.", "OPEN", ev.ids("incoming[0]", {"fee", "currency", "classification"})
    myadvantage = get("myadvantage", lambda value: value if type(value) is bool else None)
    if currency != rules["currency"] or classification != "Domestic":
        fee_reason = "An applicable domestic currency/fee rule is not established; foreign fees are not inferred."
    elif returned is not False:
        fee_reason = "Returned or unresolved return handling requires manual fee review."
    elif fee is None:
        fee_reason = "The exact incoming fee is missing."
    elif rules["fee"]["accept_incoming_myadvantage_designation"] and myadvantage is True and fee == _decimal(rules["fee"]["myadvantage_expected_fee"]):
        fee_status, fee_reason = "NOT_APPLICABLE", "The reviewer accepts the incoming MyAdvantage designation and zero recorded fee as a waiver; no fee is due. This is not an observed zero statement charge."
        fee_ids += ev.ids("incoming[0]", {"myadvantage"})
    elif fee != _decimal(rules["fee"]["domestic_amount"]):
        fee_reason = "Recorded fee does not establish the standard charge or the accepted zero-fee waiver."
    elif not principal_ok:
        fee_reason = "The principal transaction is not uniquely supported, so a separate fee cannot be linked reliably."
    else:
        linked_fees = []
        for index, record in enumerate(statements):
            if kinds[index] != "fee":
                continue
            prefix = f"statements[{index}]"
            debit_amount = ev.get(record, "amount", prefix, _decimal)
            debit_date = ev.get(record, "date", prefix, _date)
            debit_suffix = ev.get(record, "account_suffix", prefix, _suffix)
            description = ev.get(record, "description", prefix, _text)
            ref_item = record.get("wire_number")
            ref_ok = ref_item is None or (isinstance(ref_item, dict) and ref_item.get("value") is None)
            if not ref_ok:
                ref_ok = ev.get(record, "wire_number", prefix, _identifier) == wire
            if debit_date == principal["date"] and debit_suffix == principal["suffix"] and ref_ok:
                valid = debit_amount is not None and abs(debit_amount) == fee and bool(description) and _debit(record, prefix, debit_amount, ev)
                linked_fees.append((prefix, valid))
        if len(linked_fees) == 1 and linked_fees[0][1]:
            fee_status, fee_reason = "SUPPORTED", "A unique separate statement debit of the required amount matches the supported principal's date and account."
            fee_ids += principal_ids + ev.ids(linked_fees[0][0])
        else:
            fee_reason = "No unique separate debit of the required amount is linked by date/account; absence is not proof of zero charge or waiver."
    checks["fee_charged"] = _check(fee_status, fee_reason, fee_ids)

    if currency != rules["currency"]:
        checks["travel"] = _check("OPEN", "Travel threshold currency is missing or outside the supplied criterion.", ev.ids("incoming[0]", {"currency"}))
    else:
        threshold = _decimal(rules["travel"]["threshold"])
        applicable = amount >= threshold if rules["travel"]["operator"] == ">=" else amount > threshold
        if not applicable:
            checks["travel"] = _check("NOT_APPLICABLE", "Principal is below the applicability condition in the supplied Travel criterion.", ev.ids("incoming[0]", {"principal_amount", "currency"}))
        else:
            missing = [key for key in rules["travel"]["required_fields"] if not get(key, _text)]
            checks["travel"] = _check("OPEN" if missing else "SUPPORTED", "Missing or invalid evidenced Travel fields: " + ", ".join(missing) + "." if missing else "All eight reviewer-required Travel fields are evidenced; this is not a regulatory compliance conclusion.", ev.ids("incoming[0]", set(rules["travel"]["required_fields"]) | {"principal_amount", "currency"}))

    status = get("ofac_status", _text)
    normalize = lambda value: re.sub(r"[^a-z]", "", value.casefold())
    passed = status is not None and normalize(status) in {normalize(v) for v in rules["ofac"]["passed_statuses"]}
    false_positive = status is not None and normalize(status) in {normalize(v) for v in rules["ofac"]["false_positive_statuses"]}
    resolved = false_positive and get("ofac_approved", lambda value: value if type(value) is bool else None) is True and bool(get("ofac_reason", _text))
    checks["ofac"] = _check("SUPPORTED" if passed or resolved else "OPEN", "The evidenced status meets the reviewer-confirmed OFAC predicate; no fresh sanctions screening was performed." if passed or resolved else "Passed status or False Positive with evidenced approval and reason is not established.", ev.ids("incoming[0]", {"ofac_status", "ofac_approved", "ofac_reason"}))
    posting_issues = list(principal_issues)
    if currency != rules["currency"]:
        posting_issues.append("posting currency is missing or outside the supplied criterion")
    if correction is not False:
        posting_issues.append("correction handling requires manual review")
    if returned is not False:
        posting_issues.append("return handling requires manual review")
    if effective != posted:
        posting_issues.append("effective/posted date variance requires manual review")
    checks["posting"] = _check("OPEN" if posting_issues else "SUPPORTED", "; ".join(posting_issues) + "." if posting_issues else "Unique posting matches wire reference, principal, date, masked suffix and accepted minor-name standard; no full-account claim is made.", principal_ids + ev.ids("incoming[0]", {"wire_number", "principal_amount", "posted_date", "effective_date", "beneficiary_name", "beneficiary_account", "account_correction", "returned"}))
    statement_issues = [issue for issue in uncertainties if issue.get("role") == "statement" and issue["scope"] in {"document", "page"}]
    if isinstance(facts.get("coverage"), dict) and facts["coverage"].get("statement") is not True and not statement_issues:
        statement_issues = [{"scope": "document", "role": "statement", "message": "Statement source coverage is incomplete."}]
    statement_fee_fields, statement_principal_fields = [], []
    for issue in uncertainties:
        if issue.get("role") != "statement" or issue["scope"] != "field":
            continue
        index = issue.get("record_index")
        # A rejected field on a fee row does not invalidate a separately cited
        # principal. A rejected/unknown row kind could hide a conflicting wire.
        is_fee = index is not None and index < len(statements) and kinds.get(index) == "fee"
        (statement_fee_fields if is_fee else statement_principal_fields).append(issue)
    posting_uncertainties = statement_issues + statement_principal_fields
    fee_uncertainties = posting_uncertainties + statement_fee_fields
    if posting_uncertainties:
        # An unread statement page may conceal a conflicting principal or fee.
        # Reliable incoming evidence still supports its own Travel/OFAC checks.
        checks["posting"] = _check("OPEN", _uncertainty_reason(posting_uncertainties), checks["posting"]["evidence"])
    if fee_uncertainties and checks["fee_charged"]["status"] != "NOT_APPLICABLE":
        checks["fee_charged"] = _check("OPEN", _uncertainty_reason(fee_uncertainties), checks["fee_charged"]["evidence"])
    return finish()
