"""Offline mutation tests for evaluator integrity; no model/network credentials."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reusable_lab import evaluation as ev


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline, cls.report = ev.run_offline()
        cls.gold = ev._load(ev.FIXTURES / "synthetic_expected.json")

    def setUp(self):
        self.actual = copy.deepcopy(self.report)
        self.expected = copy.deepcopy(self.gold)

    def score(self):
        return ev.score_report(self.actual, self.expected)

    def codes(self, result):
        return {f["code"] for f in result["critical_failures"]}

    def set_control(self, index, control, status):
        result = self.actual["results"][index]
        symbol = {"SUPPORTED": "X", "NOT_APPLICABLE": "-", "OPEN": "OPEN"}[status]
        result["checks"][control]["status"] = status
        result["checks"][control]["symbol"] = symbol
        result["values"][control] = symbol

    def test_offline_contract_not_model_accuracy(self):
        result = self.baseline
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["samples"]["graded"], 23)
        self.assertEqual(result["check_accuracy"], 1)
        self.assertEqual(result["fields"]["exact_accuracy"], 1)
        self.assertGreater(result["fields"]["graded"], 700)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["model_accuracy_claim"], "NOT_MEASURED_MOCKED_EXTRACTION")
        self.assertFalse(result["human_validated_gold"])
        self.assertIn("NOT_ESTABLISHED", result["production_readiness"])

    def test_offline_never_invokes_extractor(self):
        with patch.object(ev, "extract_sample", side_effect=AssertionError("No model allowed")):
            self.assertEqual(ev.run_offline()[0]["status"], "PASS")

    def test_boundary_labels_frozen_independently(self):
        labels = {c["sample_id"]: c for c in self.expected["cases"]}
        self.assertEqual(labels["SYN-002"]["expected_checks"]["travel"], "NOT_APPLICABLE")
        self.assertEqual(labels["SYN-003"]["expected_checks"]["travel"], "SUPPORTED")
        self.assertEqual(labels["SYN-004"]["expected_checks"]["travel"], "OPEN")

    def test_false_supported_is_critical_and_denominator_explicit(self):
        self.set_control(8, "ofac", "SUPPORTED")  # False positive, approval absent.
        result = self.score()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("FALSE_SUPPORTED", self.codes(result))
        self.assertEqual(result["controls"]["ofac"]["false_supported"], 1)
        negatives = sum(c["expected_checks"]["ofac"] != "SUPPORTED" for c in self.expected["cases"])
        self.assertEqual(result["controls"]["ofac"]["false_supported_rate_among_expected_not_supported"], round(1 / negatives, 6))

    def test_false_not_applicable_cannot_hide_missing_travel(self):
        self.set_control(3, "travel", "NOT_APPLICABLE")
        self.assertIn("FALSE_NOT_APPLICABLE", self.codes(self.score()))

    def test_overconservative_open_is_error_but_not_false_support(self):
        self.set_control(0, "travel", "OPEN")
        result = self.score()
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn("FALSE_SUPPORTED", self.codes(result))
        self.assertLess(result["check_accuracy"], 1)

    def test_missing_evidence_field_is_detected(self):
        self.actual["extracted_facts"][0]["facts"]["incoming"][0]["fee"] = None
        result = self.score()
        self.assertLess(result["fields"]["exact_accuracy"], 1)
        self.assertEqual(result["fields"]["mismatches"][0]["field"], "incoming[0].fee")

    def test_money_decimal_equivalence_is_not_a_name_or_account_coercion(self):
        self.assertTrue(ev._equal("15.00", "15.0", "incoming[0].fee"))
        self.assertFalse(ev._equal("00001234", "1234", "incoming[0].beneficiary_account"))
        self.assertFalse(ev._equal(True, 1, "incoming[0].ofac_approved"))
        self.assertFalse(ev._equal("NaN", "NaN", "incoming[0].fee"))

    def test_fabricated_page_is_critical(self):
        self.actual["extracted_facts"][0]["facts"]["incoming"][0]["fee"]["page"] = 99
        result = self.score()
        self.assertIn("CITATION_AUDIT_FAILED", self.codes(result))

    def test_fabricated_verbatim_quote_is_critical(self):
        self.actual["extracted_facts"][0]["facts"]["incoming"][0]["ofac_status"]["quote"] = "The CEO approved everything"
        result = self.score()
        invalid = result["cases"][0]["citation_audit"]["invalid"]
        self.assertTrue(any(i["reason"] == "QUOTE_NOT_VERBATIM_ON_CITED_PHYSICAL_PAGE" for i in invalid))

    def test_wrong_document_role_is_critical(self):
        item = self.actual["extracted_facts"][0]["facts"]["incoming"][0]["fee"]
        item["source_file"] = "fictional/SYN-001/statement.pdf"
        self.assertIn("CITATION_AUDIT_FAILED", self.codes(self.score()))

    def test_stale_result_citation_is_critical(self):
        self.actual["results"][0]["evidence"][0]["quote"] = "Unsupported prior result"
        self.assertIn("CITATION_AUDIT_FAILED", self.codes(self.score()))

    def test_missing_control_references_cannot_pass(self):
        self.actual["results"][0]["checks"]["posting"]["evidence"] = []
        result = self.score()
        self.assertIn("posting", result["cases"][0]["citation_audit"]["supported_without_valid_citations"])

    def test_missing_source_pages_are_unverified_not_pass(self):
        self.actual["document_inventory"]["samples"] = []
        result = self.score()
        self.assertIn("SOURCE_PAGES_REQUIRED_FOR_ACCEPTED_CONTROLS", self.codes(result))

    def test_real_labels_without_independent_reviewer_rejected(self):
        self.expected["data_origin"] = "real"
        with self.assertRaisesRegex(ValueError, "human_validated"):
            self.score()

    def test_invalid_reviewer_date_is_not_validation(self):
        self.expected["data_origin"] = "real"
        self.expected["provenance"].update(human_validated=True, reviewer="Fictional Reviewer", reviewed_on="yesterday")
        with self.assertRaisesRegex(ValueError, "human_validated"):
            self.score()

    def attest_simulated_real_manifest(self):
        """Simulated attestation only inside this mutation test, never real gold."""
        self.actual["source_workbook_sha256"] = "a" * 64
        self.actual["rules_sha256"] = "b" * 64
        for sample in self.actual["document_inventory"]["samples"]:
            for doc in sample["documents"]:
                doc["sha256"] = "c" * 64
        self.expected["data_origin"] = "real"
        self.expected["provenance"].update(human_validated=True, reviewer="Fictional Test Reviewer", reviewed_on="2026-10-16")
        self.expected["source_identity"] = ev.source_identity(self.actual)

    def test_real_gold_requires_matching_all_source_hashes(self):
        self.attest_simulated_real_manifest()
        self.assertEqual(self.score()["status"], "PASS")
        self.actual["source_workbook_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "source_identity"):
            self.score()

    def test_real_gold_partial_or_malformed_document_hashes_rejected(self):
        self.attest_simulated_real_manifest()
        self.expected["source_identity"]["document_sha256"] = "not a mapping"
        with self.assertRaisesRegex(ValueError, "source_identity"):
            self.score()

    def test_unlabeled_real_report_has_no_accuracy_claim(self):
        result = ev.score_report(self.actual)
        self.assertEqual(result["status"], "NOT_GRADED_NO_EXPECTED_LABELS")
        self.assertIsNone(result["check_accuracy"])
        self.assertIsNone(result["fields"]["exact_accuracy"])
        self.assertEqual(result["model_accuracy_claim"], "NOT_MEASURED_NO_GOLD")
        self.assertIsNone(result["controls"]["ofac"]["supported_precision"])

    def test_blank_label_template_cannot_be_scored_and_does_not_copy_results(self):
        template = ev.label_template(self.actual)
        self.assertFalse(template["provenance"]["human_validated"])
        self.assertTrue(all(v is None for c in template["cases"] for v in c["expected_checks"].values()))
        self.assertTrue(all(not c["expected_fields"] for c in template["cases"]))
        with self.assertRaises(ValueError):
            ev.score_report(self.actual, template)

    def test_duplicate_sample_cannot_inflate_accuracy(self):
        self.actual["results"].append(copy.deepcopy(self.actual["results"][0]))
        self.assertIn("DUPLICATE_SAMPLE_RESULTS", self.codes(self.score()))

    def test_missing_case_cannot_shrink_denominator(self):
        self.actual["results"].pop()
        result = self.score()
        self.assertIn("MISSING_EXPECTED_SAMPLES", self.codes(result))
        self.assertLess(result["check_accuracy"], 1)

    def test_unexpected_case_is_explicitly_ungraded_critical(self):
        self.actual["results"].append({**copy.deepcopy(self.actual["results"][0]), "sample_id": "UNKNOWN"})
        self.assertIn("UNEXPECTED_UNGRADED_SAMPLES", self.codes(self.score()))

    def test_report_symbol_must_agree_with_status(self):
        self.actual["results"][0]["values"]["travel"] = "-"
        self.assertIn("CONTROL_SYMBOL_INCONSISTENT", self.codes(self.score()))

    def test_wrong_workbook_fee_value_detected_even_if_extracted_fee_correct(self):
        self.actual["results"][0]["values"]["fee"] = "0.00"
        result = self.score()
        self.assertEqual(result["fields"]["exact_accuracy"], 1)
        self.assertIn("WRONG_WORKBOOK_VALUE", self.codes(result))

    def test_record_order_is_aligned_by_identity_not_amount(self):
        facts = copy.deepcopy(self.actual["extracted_facts"][0]["facts"])
        label = self.expected["cases"][0]
        facts["statements"].reverse()
        aligned = ev._aligned_field_paths(label, facts)
        for field, expected in label["expected_fields"].items():
            self.assertTrue(ev._equal(expected, ev.field_at(facts, aligned[field]), field), field)
        facts["statements"][1]["amount"]["value"] = "999.00"
        aligned = ev._aligned_field_paths(label, facts)
        self.assertFalse(ev._equal(label["expected_fields"]["statements[0].amount"], ev.field_at(facts, aligned["statements[0].amount"]), "amount"))

    def test_extra_extracted_record_is_not_silently_ungraded(self):
        self.actual["extracted_facts"][0]["facts"]["statements"].append(copy.deepcopy(self.actual["extracted_facts"][0]["facts"]["statements"][0]))
        self.assertIn("EXTRACTION_RECORD_COUNT_MISMATCH", self.codes(self.score()))

    def test_empty_expected_manifest_rejected(self):
        self.expected["cases"] = []
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.score()

    def test_dataset_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            path.write_text((ev.FIXTURES / "synthetic_cases.json").read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                ev.run_offline(dataset_path=path)

    def test_held_out_and_development_splits_are_distinct(self):
        held, _ = ev.run_offline(split="held_out")
        dev, _ = ev.run_offline(split="development")
        self.assertEqual(held["samples"]["graded"], 8)
        self.assertEqual(dev["samples"]["graded"], 15)
        self.assertFalse({c["sample_id"] for c in held["cases"]} & {c["sample_id"] for c in dev["cases"]})

    def test_optional_model_api_requires_injected_model(self):
        with self.assertRaisesRegex(ValueError, "explicitly supplied"):
            ev.evaluate_model(None, model_name="test", cache_dir="unused")

    def test_optional_model_api_preserves_live_and_cached_distinction(self):
        for calls, mode in ((1, "LIVE_MODEL_TEXT_EXTRACTION"), (0, "CACHED_MODEL_TEXT_EXTRACTION")):
            def fake(context, sample, *args):
                source = next(c for c in ev._load(ev.FIXTURES / "synthetic_cases.json")["cases"] if c["sample_id"] == context["sample_id"])
                return ev.validate_facts(source["mock_raw"], sample), {"model_calls": calls, "cache_hit": not calls}
            with patch.object(ev, "extract_sample", side_effect=fake):
                result, report = ev.evaluate_model(object(), model_name="fake-test-only", cache_dir="unused")
            self.assertEqual(result["mode"], mode)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["model_calls"], calls * 8)

    def test_model_failure_stops_dispatch_and_saves_only_exception_type(self):
        with patch.object(ev, "extract_sample", side_effect=RuntimeError("SECRET-NEVER-PRINT")) as extractor:
            result, report = ev.evaluate_model(object(), model_name="fake-test-only", cache_dir="unused")
        self.assertEqual(extractor.call_count, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["mode"], "LIVE_MODEL_TEXT_EXTRACTION")
        self.assertIsNone(result["model_calls"])
        self.assertFalse(result["model_call_count_complete"])
        self.assertNotIn("SECRET-NEVER-PRINT", json.dumps((result, report)))
        self.assertEqual(sum(m.get("not_run_after_model_error", False) for m in report["extraction"]), 7)

    def make_fake_model_cache(self, folder):
        cases, _, _ = ev._synthetic_inputs(split="held_out")
        paths = []
        for case in cases:
            module = ev.extractor_module
            payload = module.extraction_payload(case["context"], case["sample"])
            signature = hashlib.sha256((module.VERSION + module.SYSTEM + json.dumps(module.SCHEMA, sort_keys=True) + "fake-test-only" + payload).encode()).hexdigest()
            path = Path(folder) / (signature + ".json")
            path.write_text(json.dumps({"signature": signature, "version": module.VERSION, "raw": case["mock_raw"]}), encoding="utf-8")
            paths.append(path)
        return paths

    def test_cache_replay_is_read_only_and_cannot_call_model_or_repairs(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = self.make_fake_model_cache(folder)
            before = {p.name: ev._sha(p) for p in paths}
            with patch.object(ev, "extract_sample", side_effect=AssertionError("No extraction or repair allowed")):
                result, report = ev.replay_model_cache(folder, model_name="fake-test-only")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["mode"], "CACHED_MODEL_REGRESSION_AFTER_OBSERVED_FAILURES")
            self.assertEqual(result["model_calls"], 0)
            self.assertTrue(result["cache_unchanged"])
            self.assertFalse(result["cache_write_performed"])
            self.assertTrue(all(not m["repair_attempted"] for m in report["extraction"]))
            self.assertEqual(before, {p.name: ev._sha(p) for p in paths})
            self.assertIn("regression after observed failures", " ".join(result["limitations"]))

    def test_missing_or_mismatched_cache_fails_instead_of_model_fallback(self):
        for mode in ("missing", "wrong_signature"):
            with tempfile.TemporaryDirectory() as folder:
                paths = self.make_fake_model_cache(folder)
                if mode == "missing":
                    paths[0].unlink()
                else:
                    data = json.loads(paths[0].read_text(encoding="utf-8")); data["signature"] = "WRONG"
                    paths[0].write_text(json.dumps(data), encoding="utf-8")
                before = {p.name: ev._sha(p) for p in Path(folder).glob("*.json")}
                with patch.object(ev, "extract_sample", side_effect=AssertionError("No fallback")):
                    with self.assertRaises(ValueError):
                        ev.replay_model_cache(folder, model_name="fake-test-only")
                self.assertEqual(before, {p.name: ev._sha(p) for p in Path(folder).glob("*.json")})

    def test_cli_writes_readable_result(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            output = Path(folder) / "result.json"
            self.assertEqual(ev.main(["--offline", "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "PASS")

    def test_cli_cannot_overwrite_input_report(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()):
            path = Path(folder) / "report.json"
            original = json.dumps(self.actual)
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(SystemExit):
                ev.main(["--report", str(path), "--output", str(path)])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_incomplete_run_cannot_be_passing_evaluation(self):
        self.actual["ok"] = False
        result = self.score()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("RUN_NOT_COMPLETE_OR_SOURCES_CHANGED", self.codes(result))


if __name__ == "__main__":
    unittest.main()
