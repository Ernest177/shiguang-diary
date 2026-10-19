"""Prove the evaluator can fail, not just that a perfect fixture can pass."""
import copy
import json
import re
import unittest
from unittest.mock import patch, MagicMock

from app.ai import ModelClient, AIError
from app.prompts import prompt_metadata
from evals.compare import compare
from evals.run import DATA, check_prompts, evaluate, read_json
from evals.scoring import aggregate, gate, score_case


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {c["id"]: c for c in read_json(DATA / "cases.json")["cases"]}
        cls.fixtures = read_json(DATA / "contract-responses.json")

    def sample(self, identifier, output):
        case = self.cases[identifier]
        return {"id": identifier, "kind": case["kind"], "error": None, "score": score_case(case, output)}

    def test_corpus_labels_have_real_anchors_and_unique_ids(self):
        original = read_json(DATA / "cases.json")["cases"]
        self.assertEqual(len(original), len(self.cases))
        self.assertEqual(set(self.cases), set(self.fixtures))
        for case in original:
            if case["kind"] == "entry":
                existing = {a["id"] for a in case["input"]["existing_actions"]}
                for label in case["expected"]["actions"]:
                    self.assertIn(label["anchor"], case["input"]["entry"]["body"])
                    re.compile(label["title_pattern"])
                    if label["existing_action_id"]:
                        self.assertIn(label["existing_action_id"], existing)
                self.assertTrue(set(case["expected"]["completions"]) <= existing)
            else:
                self.assertTrue(set(case["expected"]["source_ids"]) <= {r["id"] for r in case["input"]["records"]})

    def test_offline_contract_has_no_network_and_matches_baseline(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network allowed")):
            report = evaluate()
        self.assertEqual(report["metrics"]["cases"], 24)
        self.assertTrue(report["gate"]["passed"])
        self.assertIsNone(report["performance"]["usage"])
        baseline = read_json(DATA.parent / "baselines" / "contract-v1.json")
        self.assertTrue(compare(baseline, report)["passed"])

    def test_false_task_is_visible_even_if_application_filters_it(self):
        output = copy.deepcopy(self.fixtures["e03"])
        output["actions"] = [{"kind": "task", "title": "联系客户", "quote": "不打算联系客户",
                              "due_text": None, "due_date": None, "existing_action_id": None}]
        sample = self.sample("e03", output)
        self.assertEqual(sample["score"]["raw"]["critical_false_tasks"], 1)
        self.assertEqual(sample["score"]["final"]["critical_false_tasks"], 0)
        self.assertFalse(gate(aggregate([sample]))["passed"])

    def test_duplicate_action_is_a_false_positive(self):
        output = copy.deepcopy(self.fixtures["e01"])
        output["actions"].append(copy.deepcopy(output["actions"][0]))
        score = self.sample("e01", output)["score"]["final"]
        self.assertEqual((score["tp"], score["fp"], score["fn"]), (1, 1, 0))

    def test_fabricated_quote_rejects_result_and_keeps_missed_action(self):
        output = copy.deepcopy(self.fixtures["e01"])
        output["actions"][0]["quote"] = "下周投资一家公司"
        sample = self.sample("e01", output)
        self.assertEqual(sample["score"]["schema_valid"], 0)
        self.assertEqual(sample["score"]["final"]["fn"], 1)
        self.assertEqual(sample["score"]["raw_quotes"]["valid_quotes"], 0)

    def test_wrong_known_task_completion_is_critical(self):
        output = copy.deepcopy(self.fixtures["e15"])
        output["completions"][0]["action_id"] = "report"
        score = self.sample("e15", output)["score"]
        self.assertEqual(score["raw"]["unexpected_completions"], 1)
        self.assertEqual(score["final"]["unexpected_completions"], 1)
        self.assertEqual(score["final"]["correct_completions"], 0)

    def test_bad_date_and_missing_existing_link_are_measured(self):
        output = copy.deepcopy(self.fixtures["e18"])
        output["actions"][0].update(due_date="2026-10-21", existing_action_id=None)
        score = self.sample("e18", output)["score"]["final"]
        self.assertEqual(score["tp"], 1)
        self.assertEqual(score["correct_dates"], 0)
        self.assertEqual(score["correct_links"], 0)

    def test_daily_missing_source_lowers_coverage(self):
        output = copy.deepcopy(self.fixtures["d01"])
        output["sections"][0]["items"][0]["sources"].pop()
        score = self.sample("d01", output)["score"]
        self.assertEqual(score["schema_valid"], 1)
        self.assertEqual(score["day"]["covered_sources"], 1)
        self.assertEqual(score["day"]["required_sources"], 2)

    def test_request_failure_is_not_dropped_from_denominators(self):
        settings = {"ai_mode":"local", "ai_base_url":"http://127.0.0.1:1/v1", "ai_model":"synthetic-test"}
        with patch.object(ModelClient, "call_with_metadata", side_effect=AIError("unavailable")):
            report = evaluate("live", settings, case_ids=["e01"])
        self.assertEqual(report["metrics"]["cases"], 1)
        self.assertEqual(report["metrics"]["request_errors"], 1)
        self.assertEqual(report["metrics"]["final"]["fn"], 1)
        self.assertFalse(report["gate"]["passed"])
        self.assertEqual(report["samples"][0]["error"], "AIError")

    def test_incompatible_reports_are_refused(self):
        baseline = read_json(DATA.parent / "baselines" / "contract-v1.json")
        for field, value in (("mode", "live"), ("scorer_version", "other"), ("corpus", {}),
                             ("selected_ids", ["e01"]), ("fixture_sha256", "changed")):
            candidate = copy.deepcopy(baseline)
            candidate[field] = value
            self.assertFalse(compare(baseline, candidate)["comparable"])
        candidate = copy.deepcopy(baseline)
        candidate["prompts"]["entry"]["sha256"] = "changed-under-old-version"
        self.assertFalse(compare(baseline, candidate)["comparable"])
        candidate = copy.deepcopy(baseline)
        candidate["samples"][0]["score"]["final"]["correct_dates"] = 0
        self.assertFalse(compare(baseline, candidate)["passed"])

    def test_prompt_text_cannot_change_under_same_version(self):
        registry = {p["version"]: p["sha256"] for p in prompt_metadata().values()}
        registry[prompt_metadata()["entry"]["version"]] = "wrong"
        with patch("evals.run.read_json", return_value=registry):
            with self.assertRaises(ValueError):
                check_prompts()

    def test_provider_metadata_preserves_worker_call_contract(self):
        settings = {"ai_mode":"local", "ai_base_url":"http://127.0.0.1:1/v1", "ai_model":"requested"}
        client = ModelClient(type("EmptyKey", (), {"get": lambda self: ""})())
        raw = {"model": "returned", "choices": [{"message": {"content": '{"ok":true}'}}],
               "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            output, meta = client.call_with_metadata(settings, "system", {"synthetic": True})
            self.assertEqual(client.call(settings, "system", {}), {"ok":True})
            raw["usage"] = "malformed optional metadata"
            response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
            self.assertEqual(client.call(settings, "system", {}), {"ok":True})
        self.assertEqual(output, {"ok":True})
        self.assertEqual(meta["returned_model"], "returned")
        self.assertEqual(meta["usage"]["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
