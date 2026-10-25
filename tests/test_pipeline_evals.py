"""Pipeline comparisons must measure calls and preserve unknown billing."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.ai import AIError
from evals.pipeline import ContractClient, compare_reports, evaluate, metrics, summary


class PipelineEvaluationTests(unittest.TestCase):
    def test_automatic_mode_saves_requests_but_does_not_invent_token_savings(self):
        report = evaluate(case_ids=["brief", "expression", "cached", "growth"])
        self.assertTrue(report["passed"])
        self.assertLess(report["totals"]["auto"]["calls"], report["totals"]["always"]["calls"])
        self.assertIsNone(report["cost_reduction_percent"])
        for policy in report["totals"].values():
            self.assertIsNone(policy["input_tokens"])
            self.assertIsNone(policy["estimated_cost_cny"])
        expression = [s["metrics"]["calls"] for s in report["samples"] if s["id"] == "expression"]
        self.assertEqual(expression, [2, 2], "explicit expression difficulty must still invoke a recap")
        self.assertTrue(all(s["checks"]["cache_rechecks_without_calls"] for s in report["samples"]))
        self.assertTrue(all(s["checks"]["separate_growth_occurrences"] for s in report["samples"] if s["id"] == "growth"))
        self.assertIn("不以字符数", summary(report))

    def test_cap_covers_both_policies_and_incomplete_pairs_are_not_comparable(self):
        report = evaluate(case_ids=["brief"], max_calls=1)
        self.assertFalse(report["comparable"])
        self.assertFalse(report["passed"])
        self.assertEqual(sum(s["metrics"]["calls"] for s in report["samples"]), 1)
        self.assertIsNone(report["cost_reduction_percent"])

    def test_retries_are_counted_and_an_empty_usage_is_not_zero(self):
        class OnceFailing(ContractClient):
            def __init__(self, ledger):
                super().__init__(ledger)
                self.first = True

            def call(self, settings, prompt, content):
                if self.first:
                    self.first = False
                    identifier = self.ledger.start(settings)
                    self.ledger.finish(identifier, {}, "synthetic", "error", 0)
                    raise AIError("合成可恢复故障")
                return super().call(settings, prompt, content)
        report = evaluate(case_ids=["brief"], backend_factory=OnceFailing)
        self.assertTrue(report["passed"])
        self.assertEqual(report["totals"]["always"]["calls"], 3)
        self.assertEqual(report["totals"]["always"]["retry_calls"], 1)
        self.assertEqual(report["totals"]["auto"]["calls"], 2)
        self.assertIsNone(report["totals"]["always"]["estimated_cost_cny"])

    def test_initial_backfill_is_measured_separately_and_warm_checks_reuse_it(self):
        report = evaluate(case_ids=["history"])
        self.assertTrue(report["passed"])
        for policy in report["totals"].values():
            self.assertEqual(policy["by_kind"]["growth"], 3)
            self.assertGreaterEqual(policy["by_phase"]["backfill"], 3)
            self.assertEqual(policy["by_phase"].get("recheck", 0), 0)
        self.assertTrue(all(s["checks"]["separate_growth_occurrences"] for s in report["samples"]))

    def test_partial_accounting_cannot_be_presented_as_complete_cost(self):
        calls = [{"kind": "entry", "attempt": 1, "request_characters": 100, "latency_ms": 1}] * 2
        rows = [{"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "estimated_cost": "0.01"},
                {"input_tokens": None, "output_tokens": None, "total_tokens": None, "estimated_cost": None}]
        result = metrics(calls, rows, "live")
        self.assertIsNone(result["input_tokens"])
        self.assertEqual(result["known_cost_cny"], "0.01")
        self.assertIsNone(result["estimated_cost_cny"])
        self.assertEqual(result["unknown_cost_calls"], 1)

    def test_comparison_rejects_unmatched_inputs_and_unknown_cost_has_no_delta(self):
        report = evaluate(case_ids=["brief"])
        result = compare_reports(report, copy.deepcopy(report))
        self.assertTrue(result["passed"])
        self.assertEqual(result["deltas"]["auto"]["calls"], 0)
        self.assertIsNone(result["deltas"]["auto"]["estimated_cost_cny"])
        changed = copy.deepcopy(report)
        changed["corpus"]["sha256"] = "different"
        self.assertFalse(compare_reports(report, changed)["comparable"])
        changed = copy.deepcopy(report)
        changed["mode"] = "live"
        self.assertFalse(compare_reports(report, changed)["comparable"])
        changed = copy.deepcopy(report)
        changed["samples"][0]["checks"]["originals_preserved"] = False
        self.assertFalse(compare_reports(report, changed)["passed"])

    def test_live_adapter_totals_include_per_call_cache_and_price_without_network(self):
        def response(request, timeout):
            content = json.loads(json.loads(request.data)["messages"][-1]["content"])
            self.assertEqual(content['source_reference_format'],'source-refs.v1')
            if "entry" in content:
                body=''.join(s['text'] for s in content['entry']['segments'])
                output = {"schema_version": 1, "summary": body, "insights": [], "actions": [], "completions": []}
            else:
                body=''.join(s['text'] for s in content['records'][0]['segments'])
                output = {"schema_version": 1, "overview": body, "sections": []}
            return io.BytesIO(json.dumps({"model": "synthetic", "usage": {
                "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 40}, "completion_tokens_details": {"reasoning_tokens": 4}},
                "choices": [{"message": {"content": json.dumps(output)}}]}).encode())
        opener = MagicMock()
        opener.open.side_effect = response
        with patch("app.ai.urllib.request.build_opener", return_value=opener):
            report = evaluate(mode="live", settings={"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "synthetic"},
                              prices={"input_rate": "1", "output_rate": "2", "cached_rate": "0.5"}, case_ids=["brief"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["totals"]["always"]["input_tokens"], 200)
        self.assertEqual(report["totals"]["always"]["total_tokens"], 240)
        self.assertEqual(report["totals"]["always"]["estimated_cost_cny"], "0.00024000")
        self.assertEqual(report["cost_reduction_percent"], 50.0)
        self.assertEqual(report["samples"][0]["calls"][0]["accounting"]["cached_tokens"], 40)
        self.assertEqual(opener.open.call_count, 3)

    def test_cli_redirected_cp1252_outputs_chinese_utf8_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "evals.pipeline", "--case", "brief", "--max-calls", "6",
                                     "--output-dir", directory], cwd=Path(__file__).resolve().parents[1],
                                    env={**os.environ, "PYTHONIOENCODING": "cp1252"}, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertIn("拾光完整流程对比", result.stdout.decode("utf-8"))
            report = json.loads(next(Path(directory).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(report["mode"], "contract")
            self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
