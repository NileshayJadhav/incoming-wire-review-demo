"""Source-context defenses for sanctions fields and omitted statement entries.

The guards never create a fact, make a compliance decision, read credentials or
call a model. A literal quote/value match is insufficient when it is a customer
instruction, an approver's name used as a reason, or an omitted duplicate wire.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

_STATUS = re.compile(r"(?im)^\s*OFAC\s+(?:Status|Screening\s+(?:Status|Result)|Review\s+(?:Status|Result)|Result)\s*[:=-]?\s*(?:OFAC\s+)?(?P<value>False\s+Positive|Passed)\b")
_STATUS_LABEL = re.compile(r"(?im)^\s*OFAC\s+(?:Status|Screening\s+(?:Status|Result)|Review\s+(?:Status|Result)|Result)\s*[:=-]?\s*(?P<tail>[^\r\n]*)")
_APPROVAL = re.compile(r"(?im)^\s*OFAC\s+(?:Disposition\s+)?Approved\b(?P<tail>[^\r\n]*)")
_APPROVAL_STATUS = re.compile(r"(?im)^\s*OFAC\s+Approval\s+Status\s*[:=-]?\s*(?P<value>Approved)\b(?P<tail>[^\r\n]*)")
_APPROVAL_ANY = re.compile(r"(?im)^\s*OFAC\s+(?:Approval(?:\s+Status)?|(?:Disposition\s+)?(?:Not\s+)?Approved)\b[^\r\n]*")
_REASON = re.compile(r"(?im)^\s*(?P<label>(?:OFAC\s+(?:Disposition\s+)?)?Reason)\s*:\s*")
_FIELD_BOUNDARY = re.compile(r"(?i)\b(?:Released|Uploaded|Posted|Effective\s+Date|Reference\s+Number|Wire\s+(?:Number|Amount|Fee|Status)|Charge\s+Bearer|Core\s+Information|Basic\s+Settlement\s+Information|Audit\s+Trail|Sending\s+FI|Receiving\s+FI|Return\s+(?:Details|Information)|Rejection\s+Details|Payment\s+(?:Details|Information)|Transaction\s+Details|OFAC\s+hit)\b")
_INSTRUCTION = re.compile(r"(?is)\b(?:ignore|disregard|override)\b.{0,70}\b(?:rules?|instructions?|checks?|polic(?:y|ies)|system)\b|\b(?:mark|set|return|say)\b.{0,45}\b(?:OFAC|Travel|SUPPORTED)\b|\breveal\b.{0,50}\b(?:records?|customers?|keys?|secrets?)\b")
_SECTION = re.compile(r"(?im)^\s*(?P<untrusted>(?:UNTRUSTED\s+)?(?:CUSTOMER\s+)?(?:MEMO|NOTES?|REMARKS?|REM(?:ITTANCE)?\s+INFORMATION|CUSTOMER\s+MESSAGE|ORIGINATOR\s+(?:TO\s+BENEFICIARY\s+)?INSTRUCTIONS|BENEFICIARY\s+INSTRUCTIONS|Return\s+(?:Details|Information)|Rejection\s+Details|Payment\s+(?:Details|Information)|Transaction\s+Details))\b|^\s*(?P<trusted>General\s+Information|Audit\s+Trail|Core\s+Information|Incoming\s+Wire\s+Detail|Basic\s+Settlement\s+Information|Compliance\s+Review|OFAC\s+Review|Sanctions\s+Screening)\b")


def _normal(value):
    return " ".join(str(value).split()).casefold()


def _phrase(text):
    words = str(text).split()
    return re.compile(r"\s+".join(re.escape(word) for word in words), re.I) if words else None


def _blocked(page_text, start, end):
    line_start = page_text.rfind("\n", 0, start) + 1
    line_end = page_text.find("\n", end)
    line_end = len(page_text) if line_end < 0 else line_end
    if _INSTRUCTION.search(page_text[line_start:line_end]):
        return True
    # A labeled value inside a memo remains a memo. A later heading on the
    # same page cannot establish that the untrusted section has ended.
    sections = list(_SECTION.finditer(page_text[:start]))
    return any(section.group("untrusted") for section in sections)


def _quote_covers(page_text, quote, value, start, end):
    """The cited occurrence must contain the value inside the labeled block."""
    qp, vp = _phrase(quote), _phrase(value)
    if qp is None or vp is None or _INSTRUCTION.search(quote):
        return False
    for q in qp.finditer(page_text):
        for v in vp.finditer(page_text, max(q.start(), start), min(q.end(), end)):
            if start <= v.start() and v.end() <= end and not _blocked(page_text, start, end):
                return True
    return False


def _approval_blocks(page_text):
    for pattern in (_APPROVAL, _APPROVAL_STATUS):
        for match in pattern.finditer(page_text):
            tail = match.group("tail").strip(" :=-")
            if re.match(r"(?i)(?:no|false|not\b|pending|denied|rejected)\b", tail):
                continue
            if not _blocked(page_text, match.start(), match.end()):
                yield match


def _reason_blocks(page_text):
    status = list(_STATUS.finditer(page_text))
    for match in _REASON.finditer(page_text):
        # Generic Reason: belongs to OFAC only if a preceding explicit OFAC
        # status exists in this record. Other payment/return reasons do not.
        if not match.group("label").casefold().startswith("ofac"):
            preceding = [s for s in status if s.end() <= match.start() and match.start() - s.end() <= 1200]
            if not preceding or _normal(preceding[-1].group("value")) != "false positive":
                continue
            between = page_text[preceding[-1].end():match.start()]
            if re.search(r"(?i)\b(?:Return\s+(?:Details|Information|Reason)|Rejection\s+Details|Payment\s+(?:Details|Information)|Transaction\s+Details|Wire\s+Status\s*:?\s*(?:Returned|Rejected))\b", between):
                continue
        end = min(len(page_text), match.end() + 500)
        boundary = _FIELD_BOUNDARY.search(page_text, match.end(), end)
        if boundary:
            end = boundary.start()
        # Source reason text can wrap naturally; blank lines or another explicit
        # labeled record field terminate it. Never consume the rest of the page.
        rest = page_text[match.end():end]
        termination = re.search(r"\n\s*\n|\n\s*[A-Za-z][A-Za-z /_-]{1,45}:", rest)
        if termination:
            end = match.end() + termination.start()
        if page_text[match.end():end].strip() and not _blocked(page_text, match.start(), end):
            yield match.end(), end


def guard_fact(group, field, value, quote, page_text):
    """Return False for an incoming OFAC field lacking its own source context.

    This is an additional predicate after exact page/quote/value validation.
    It never interprets a customer memo as a policy or an authoritative result.
    """
    if group != "incoming" or field not in {"ofac_status", "ofac_approved", "ofac_reason"}:
        return True
    if not isinstance(page_text, str) or not isinstance(quote, str):
        return False
    # A forged memo can imitate a record heading. An explicit behavioral
    # instruction on the same source page therefore requires manual OFAC review;
    # a later 'General Information' line cannot restore trust in that page.
    if _INSTRUCTION.search(page_text):
        return False
    if field == "ofac_status":
        if not isinstance(value, str):
            return False
        wanted = re.sub(r"[^a-z]", "", value.casefold()).removeprefix("ofac")
        if wanted not in {"passed", "falsepositive"}:
            return False
        # Every actual labeled status on the page must agree. Ignoring Pending,
        # Failed or a correction after 'Passed' would cherry-pick a subquote.
        observed = []
        for label in _STATUS_LABEL.finditer(page_text):
            if _blocked(page_text, label.start(), label.end()):
                continue
            parsed = re.match(r"(?i)(?:OFAC\s+)?(False\s+Positive|Passed)\b(.*)$", label.group("tail"))
            if parsed is None:
                return False
            remainder = parsed.group(2).strip()
            if re.search(r"(?i)\bOFAC\s+(?:Status|Screening|Review|Result)\b", remainder):
                return False
            if remainder and not re.match(r"(?i)^(?:Reference\s+Number|Charge\s+Bearer|Wire\s+(?:Number|Status)|Effective\s+Date)\b", remainder):
                return False
            observed.append(re.sub(r"[^a-z]", "", parsed.group(1).casefold()))
        if not observed or any(status != wanted for status in observed):
            return False
        return any(re.sub(r"[^a-z]", "", match.group("value").casefold()) == wanted and
                   _quote_covers(page_text, quote, match.group("value"), match.start("value"), match.end("value"))
                   for match in _STATUS.finditer(page_text))
    if field == "ofac_approved":
        if value is not True:
            return False
        for label in _APPROVAL_ANY.finditer(page_text):
            if _blocked(page_text, label.start(), label.end()):
                continue
            if re.search(r"(?i)\b(?:denied|rejected|revoked|withdrawn|pending|not\s+approved)\b", label.group(0)):
                return False
            if _APPROVAL.match(label.group(0)) is None and _APPROVAL_STATUS.match(label.group(0)) is None:
                return False
        return any(_quote_covers(page_text, quote, "Approved", match.start(), match.end()) for match in _approval_blocks(page_text))
    if not isinstance(value, str) or not value.strip() or _INSTRUCTION.search(value):
        return False
    return any(_quote_covers(page_text, quote, value, start, end) for start, end in _reason_blocks(page_text))


def _record_value(record, key):
    item = record.get(key) if isinstance(record, dict) else None
    return item.get("value") if isinstance(item, dict) else None


def _ref_pattern(reference):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(reference) + r"(?![A-Za-z0-9])", re.I)


def _principal_candidate(text, ref):
    return bool(ref.search(text) and re.search(r"\b(?:principal|deposit|credit)\b|\bincoming\s+wire\b", text, re.I)
                and not re.search(r"\bfee\b|\b(?:reversal|withdrawal|debit)\b", text, re.I))


def _source_principals(page, reference):
    """Count physical table rows, or explicit native-text principal entries."""
    ref, observed = _ref_pattern(reference), page.get("statement_rows")
    if isinstance(observed, list) and observed:
        rows, seen_coordinates = [], set()
        for index, row in enumerate(observed):
            if not isinstance(row, dict):
                continue
            text = row.get("row_text", row.get("text", ""))
            if not _principal_candidate(text, ref):
                continue
            coordinates = row.get("bbox_pixels")
            coordinate_key = tuple(coordinates) if isinstance(coordinates, list) else None
            if coordinate_key is not None and coordinate_key in seen_coordinates:
                continue
            if coordinate_key is not None:
                seen_coordinates.add(coordinate_key)
            rows.append(index)
        # Body OCR plus appended targeted rows represent the same physical rows;
        # never sum their textual repetitions as distinct transactions.
        return len(rows)
    text = page.get("text", "")
    if not isinstance(text, str):
        return 0
    return sum(_principal_candidate(line, ref) for line in text.splitlines())


def source_diagnostics(sample, validated_facts):
    """Attribute source-visible omitted/duplicate principal entries to statement.

    Multiple explicit principal occurrences for the same reference must not be
    collapsed to a unique match by the model. This is a conservative ambiguity
    gate; it does not invent omitted transactions or decide their amounts.
    """
    references = {str(_record_value(record, "wire_number")) for record in validated_facts.get("incoming", [])
                  if _record_value(record, "wire_number") is not None}
    diagnostics = []
    for reference in references:
        positions, seen_pages = [], set()
        for document in sample.get("documents", []):
            if document.get("role") != "statement":
                continue
            for page in document.get("pages", []):
                identity = (document.get("relative_path"), page.get("page"))
                if identity in seen_pages:
                    continue
                seen_pages.add(identity)
                count = _source_principals(page, reference)
                if count:
                    positions.append((identity, count))
        source_count = sum(count for _, count in positions)
        extracted_count = sum(_record_value(record, "kind") == "principal" and
                              str(_record_value(record, "wire_number")) == reference
                              for record in validated_facts.get("statements", []))
        if source_count < 2 or extracted_count >= source_count:
            continue
        for (source_file, page), _ in positions:
            diagnostics.append({"scope": "page", "role": "statement", "source_file": source_file, "page": page,
                                "code": "SOURCE_PRINCIPAL_ENTRIES_COLLAPSED", "record_index": None, "field": None,
                                "message": "Source contains multiple principal transaction entries for the incoming reference; the extraction did not retain every occurrence. Uniqueness requires manual review."})
    return diagnostics


_PRINCIPAL_START = re.compile(r"(?im)^[ \t]*(?:Principal\s+wire\s+reference|Incoming\s+wire\s+(?:reference|number)|Wire\s+(?:reference|number))[ \t]*[:=][ \t]*(?P<value>[A-Za-z0-9./_-]{3,80})[ \t]*$")
_FEE_START = re.compile(r"(?im)^[ \t]*(?:Wire\s+)?Fee\s+Amount[ \t]*[:=][^\r\n]*")


def _line_item(block, labels, source_file, page, converter=lambda value: value):
    candidates = []
    for label in labels:
        pattern = re.compile(r"(?im)^[ \t]*" + label + r"[ \t]*[:=][ \t]*(?P<value>[^\r\n]+)")
        for match in pattern.finditer(block):
            raw = match.group("value").strip()
            try:
                value = converter(raw)
            except (ValueError, InvalidOperation):
                continue
            if value is not None:
                candidates.append({"value": value, "source_file": source_file, "page": page, "quote": match.group(0).strip()})
    if not candidates or len({_normal(item["value"]) for item in candidates}) != 1:
        return None
    return candidates[0]


def _decimal_text(text):
    if not re.fullmatch(r"\(?[+-]?\$?\d[\d,]*\.\d{2}\)?", text):
        raise ValueError("Not explicit money")
    value = Decimal(text.replace(",", "").replace("$", "").strip("()"))
    if text.startswith("("):
        value = -abs(value)
    return str(value) if value.is_finite() else None


def _date_text(text):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError("Not a complete explicit date")


def _masked_suffix(text):
    match = re.fullmatch(r"(?:\*+|[Xx]+)[ -]*(\d{4})", text)
    return match[1] if match else None


def _statement_block(block, source_file, page, kind):
    """Parse only explicit labeled records; no amount/account inference."""
    prefix = r"(?:Wire\s+deposit|Principal(?:\s+wire)?)" if kind == "principal" else r"(?:Wire\s+)?Fee"
    record = {
        "amount": _line_item(block, [prefix + r"\s+Amount"], source_file, page, _decimal_text),
        "date": _line_item(block, [prefix + r"\s+(?:Posted\s+)?Date"], source_file, page, _date_text),
        "account_suffix": _line_item(block, [r"(?:Statement\s+)?Account(?:\s+Number)?"], source_file, page, _masked_suffix),
        "description": _line_item(block, [r"(?:Transaction\s+)?Description"], source_file, page),
        "direction": _line_item(block, [r"(?:Fee\s+)?Transaction\s+Direction"], source_file, page, lambda value: value.casefold() if value.casefold() in {"credit", "debit"} else None),
    }
    if any(item is None for item in record.values()):
        return None
    description = record["description"]
    if kind == "principal" and (not re.search(r"(?i)\bwire\b|\bdeposit\b|\bcredit\b", description["value"]) or re.search(r"(?i)\bfee\b", description["value"])):
        return None
    if kind == "fee" and not re.search(r"(?i)\bfee\b", description["value"]):
        return None
    record["kind"] = {**description, "value": kind}
    if kind == "principal":
        record["wire_number"] = _line_item(block, [r"Principal\s+wire\s+reference", r"Incoming\s+wire\s+(?:reference|number)", r"Wire\s+(?:reference|number)"], source_file, page)
        record["account_name"] = _line_item(block, [r"(?:Statement\s+)?Account\s+Name"], source_file, page)
        if record["wire_number"] is None or record["account_name"] is None:
            return None
    else:
        reference = _line_item(block, [r"(?:Wire\s+)?Fee\s+(?:Reference|Number)"], source_file, page)
        if reference is not None:
            record["wire_number"] = reference
    return record


def recover_statement_records(sample, validated_facts):
    """Return (statement records, source audits) for complete labeled page sets.

    This recognizes a generic explicit field layout, not a dataset/sample name.
    Native usable pages only; every required principal/fee field must be present
    and unambiguous. Physical repeated blocks are retained as separate records.
    Callers MUST run each returned field through their normal quote/value/role
    validator. No reconstructed source record is automatically a passing check.
    """
    recovered, audits, rejected_pages = {}, [], set()
    for document in sample.get("documents", []):
        if document.get("role") != "statement" or document.get("diagnostics"):
            continue
        source_file = document.get("relative_path")
        for page in document.get("pages", []):
            text = page.get("text", "")
            if (page.get("quality") != "usable" or page.get("needs_review") or page.get("diagnostics")
                    or page.get("method") not in {"native", "synthetic_text"} or not isinstance(text, str) or _INSTRUCTION.search(text)):
                continue
            principal_starts = list(_PRINCIPAL_START.finditer(text))
            if not principal_starts:
                continue
            records, positions, valid = [], [], True
            for index, start in enumerate(principal_starts):
                end = principal_starts[index + 1].start() if index + 1 < len(principal_starts) else len(text)
                fee_starts = list(_FEE_START.finditer(text, start.end(), end))
                principal_end = fee_starts[0].start() if fee_starts else end
                principal = _statement_block(text[start.start():principal_end], source_file, page["page"], "principal")
                if principal is None:
                    valid = False; break
                records.append(principal); positions.append((start.start(), principal_end))
                for fee_index, fee_start in enumerate(fee_starts):
                    fee_end = fee_starts[fee_index + 1].start() if fee_index + 1 < len(fee_starts) else end
                    charge = _statement_block(text[fee_start.start():fee_end], source_file, page["page"], "fee")
                    if charge is None:
                        valid = False; break
                    records.append(charge); positions.append((fee_start.start(), fee_end))
                if not valid:
                    break
            if not valid or not records:
                continue
            key = (source_file, page["page"])
            if key in rejected_pages:
                continue
            if key in recovered:  # Duplicated page identities are not safe recovery.
                recovered.pop(key, None)
                rejected_pages.add(key)
                continue
            recovered[key] = records
            for index, (record, span) in enumerate(zip(records, positions)):
                audits.append({"kind": "SOURCE_LABELED_STATEMENT_RECORD_RECONSTRUCTED", "group": "statements",
                               "record_index_source": index, "source_file": source_file, "page": page["page"],
                               "source_span_start": span[0], "source_span_end": span[1], "fields": list(record),
                               "reason": "All required fields were explicitly labeled on one usable native source page; repeated physical blocks were preserved independently. Normal source validation is still required."})
    original = validated_facts.get("statements", [])
    if not recovered or not isinstance(original, list):
        return original, []
    audits = [entry for entry in audits if (entry["source_file"], entry["page"]) in recovered]
    results, emitted = [], set()
    for record in original:
        keys = {(item.get("source_file"), item.get("page")) for item in record.values() if isinstance(item, dict)} if isinstance(record, dict) else set()
        if len(keys) == 1 and next(iter(keys)) in recovered:
            key = next(iter(keys))
            if key not in emitted:
                results.extend(recovered[key]); emitted.add(key)
        else:
            results.append(record)
    for key, records in recovered.items():
        if key not in emitted:
            results.extend(records)
    return results, audits
