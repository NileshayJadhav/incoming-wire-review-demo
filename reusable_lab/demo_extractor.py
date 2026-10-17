"""Fictional-layout parser for the explicit offline demo; no model calls."""
import re
from .fact_extractor import validate_facts

def fixture_extractor(context, sample):
    """Extract current fixture text and pass every quote through the real validator."""
    raw = {"incoming": [], "statements": [], "returns": [], "issues": []}
    for document in sample["documents"]:
        lines = [(line.strip(), page["page"]) for page in document["pages"]
                 for line in page["text"].splitlines() if line.strip()]

        def item(value, quote, page):
            return {"value": value, "source_file": document["relative_path"], "page": page, "quote": quote}

        def field(label):
            pattern = re.compile(r"^" + re.escape(label) + r"\s*:\s*(.*)$", re.I)
            for line, page in lines:
                match = pattern.match(line)
                if match:
                    return match.group(1), line, page
            return None

        def assign(record, target, label, convert=lambda value: value):
            source = field(label)
            if source:
                value, quote, page = source
                converted = convert(value)
                record[target] = item(converted, quote, page) if converted is not None else None

        if document["role"] == "incoming":
            record = {}
            for target, label in (("wire_number", "Wire Number"), ("effective_date", "Effective Date"),
                                  ("originator_name", "Originator Name"), ("originator_account", "Originator Account"),
                                  ("originator_address", "Originator Address"), ("sending_fi", "Sending FI"),
                                  ("receiving_fi", "Receiving FI"), ("beneficiary_name", "Beneficiary Name"),
                                  ("beneficiary_account", "Beneficiary Account"), ("beneficiary_address", "Beneficiary Address")):
                assign(record, target, label)
            assign(record, "posted_date", "Posted", lambda value: value[:10])
            assign(record, "principal_amount", "Wire Amount / Currency", lambda value: value.split()[0])
            assign(record, "currency", "Wire Amount / Currency", lambda value: value.split()[-1])
            assign(record, "fee", "Wire Fee", lambda value: value if re.fullmatch(r"\d+(?:\.\d+)?", value) else None)
            assign(record, "classification", "Type of Wire", lambda value: "Domestic" if value == "Domestic Incoming Wire" else None)
            assign(record, "ofac_status", "OFAC Status", lambda value: "Passed" if value == "OFAC Passed" else None)
            assign(record, "myadvantage", "Fee Description", lambda value: True if "MyAdvantage" in value else None)
            raw["incoming"].append(record)
        elif document["role"] == "statement":
            name, account = field("Member Name"), field("Account")
            for line, page in lines:
                match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) Wire Deposit (Incoming Wire|Fee) (\d+) ([+-]\d+\.\d+) USD", line)
                if not match:
                    continue
                day, kind, reference, amount = match.groups()
                record = {key: item(value, line, page) for key, value in {
                    "date": day, "kind": "principal" if kind == "Incoming Wire" else "fee",
                    "wire_number": reference, "amount": amount, "description": line,
                    "direction": "credit" if kind == "Incoming Wire" else "debit"}.items()}
                if name:
                    record["account_name"] = item(name[0], name[1], name[2])
                if account:
                    record["account_suffix"] = item(account[0][-4:], account[1], account[2])
                raw["statements"].append(record)
    return validate_facts(raw, sample), {"extractor": "fictional_fixture_parser", "model_calls": 0}
