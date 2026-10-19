"""Synthetic accounting checks. No real provider, credentials or diary data."""
import csv
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

from app.ai import AIError, ModelClient
from app.server import make_server
from app.store import Store
from app.usage import UsageLedger, call_scope, estimate, provider_key, rate, usage_counts


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(self.temp.name)
        self.settings = {"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "synthetic",
                         "timezone": "Asia/Shanghai", "backup_keep": 3}
        self.save_price("0.8", "2.7")

    def tearDown(self):
        self.temp.cleanup()

    def save_price(self, input_rate, output_rate, cached_rate=None):
        self.ledger.save_price(self.settings, {"model": self.settings["ai_model"], "provider": provider_key(self.settings),
            "input_rate": input_rate, "output_rate": output_rate, "cached_rate": cached_rate})

    def record(self, usage=None):
        identifier = self.ledger.start(self.settings)
        self.ledger.finish(identifier, usage or {}, "synthetic", "success", 15)
        return identifier

    def rows(self):
        return self.ledger.state(self.settings, "all")

    def test_cached_and_reasoning_tokens_are_subsets_not_extra_tokens(self):
        raw = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
               "prompt_tokens_details": {"cached_tokens": 200}, "completion_tokens_details": {"reasoning_tokens": 80}}
        counts = usage_counts(raw)
        self.assertEqual(counts["total_tokens"], 1100)
        self.assertEqual(counts["reasoning_tokens"], 80)
        self.assertEqual(estimate(counts, {"input_rate": "0.8", "output_rate": "2.7", "cached_rate": "0.08"}), ("0.00092600", "cached_rate"))
        self.assertEqual(estimate(counts, {"input_rate": "0.8", "output_rate": "2.7"}), ("0.00107000", "standard_rates"))

    def test_missing_invalid_and_zero_usage_are_distinct(self):
        for raw in ({}, None, {"prompt_tokens": True, "completion_tokens": -1}, {"prompt_tokens": "10"}):
            with self.subTest(raw=raw):
                counts = usage_counts(raw)
                self.assertIsNone(counts["input_tokens"])
                self.assertIsNone(estimate(counts, {"input_rate": "0", "output_rate": "0"})[0])
        zero = usage_counts({"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(estimate(zero, {"input_rate": "0.8", "output_rate": "2.7"})[0], "0.00000000")

    def test_totals_can_be_derived_but_conflicts_are_flagged(self):
        counts = usage_counts({"prompt_tokens": 20, "completion_tokens": 5})
        self.assertEqual((counts["total_tokens"], counts["total_source"]), (25, "sum"))
        bad = usage_counts({"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 99,
                            "prompt_tokens_details": {"cached_tokens": 21}})
        self.assertIsNone(bad["total_tokens"])
        self.assertIsNone(bad["cached_tokens"])
        self.assertIn("不一致", bad["usage_note"])

    def test_price_input_validation(self):
        for value in (True, None, "NaN", "Infinity", "-1", "1.000000001", "1000001", "not a rate"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rate(value)
        self.assertEqual(rate("0"), "0")
        self.assertIsNone(rate("", True))

    def test_prices_are_bound_to_provider_and_model(self):
        for changes in ({"ai_base_url": "http://127.0.0.1:2/v1"}, {"ai_model": "other"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.ledger.price({**self.settings, **changes}))
        with self.assertRaises(ValueError):
            self.ledger.save_price(self.settings, {"model": "other", "provider": provider_key(self.settings), "input_rate": 0, "output_rate": 0})
        verified = {**self.settings, "ai_model": "qwen3.8-flash", "ai_base_url": "https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}
        self.assertEqual(self.ledger.price(verified)["output_rate"], "2.7")
        self.assertIsNone(self.ledger.price({**verified, "ai_base_url": "https://example.com/v1"}))

    def test_price_snapshot_does_not_change_mid_request_or_reprice_history(self):
        identifier = self.ledger.start(self.settings)
        self.save_price("9", "9")
        self.ledger.finish(identifier, {"prompt_tokens": 1000, "completion_tokens": 100}, "actual-alias", "success", 20)
        first = self.rows()["records"][0]
        self.assertEqual(first["input_rate"], "0.8")
        self.assertEqual(first["estimated_cost"], "0.00107000")
        self.assertEqual(first["returned_model"], "actual-alias")
        self.assertEqual(self.rows()["price"]["input_rate"], "9")

    def test_unknown_cost_not_counted_as_zero_and_interrupted_request_persists(self):
        self.record({"prompt_tokens": 1000, "completion_tokens": 100})
        self.ledger.start(self.settings)
        restarted = UsageLedger(self.temp.name)
        state = restarted.state(self.settings, "all")
        self.assertEqual(state["summary"]["requests"], 2)
        self.assertEqual(state["summary"]["unknown_cost"], 1)
        self.assertEqual(state["summary"]["known_cost"], "0.00107000")
        self.assertTrue(any(r["status"] == "interrupted" and r["estimated_cost"] is None for r in state["records"]))

    def test_month_filter_uses_local_timezone_and_paginates(self):
        identifiers = [self.record({"prompt_tokens": 1, "completion_tokens": 1}) for _ in range(2)]
        with self.ledger.connection() as db:
            db.execute("UPDATE calls SET started_at='2026-10-31T15:59:59.000+00:00' WHERE id=?", (identifiers[0],))
            db.execute("UPDATE calls SET started_at='2026-10-31T16:00:00.000+00:00' WHERE id=?", (identifiers[1],))
        self.assertEqual(self.ledger.state(self.settings, "2026-10")["summary"]["requests"], 1)
        self.assertEqual(self.ledger.state(self.settings, "2026-11")["summary"]["requests"], 1)
        self.assertEqual(len(self.ledger.state(self.settings, "all", 1, 1)["records"]), 1)
        for month in ("2026-13", "2026-1", "../../data"):
            with self.assertRaises(ValueError):
                self.ledger.state(self.settings, month)

    def test_diary_restore_does_not_rewind_usage_and_snapshots_are_complete(self):
        store = Store(self.temp.name)
        old_diary = store.export()
        self.record({"prompt_tokens": 1000, "completion_tokens": 100})
        store.import_data(old_diary, replace=True)
        self.assertEqual(self.rows()["summary"]["requests"], 1)
        self.ledger.backup(self.settings)
        snapshot = next((Path(self.temp.name)/"backups").glob("usage-*.sqlite3"))
        with contextlib.closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM calls").fetchone()[0], 1)
        self.assertNotIn(snapshot.name, {r["name"] for r in store.backups()})

    def test_csv_is_safe_for_spreadsheets_and_has_no_private_payload(self):
        self.settings["ai_model"] = "=dangerous()"
        self.record({"prompt_tokens": 1, "completion_tokens": 1})
        rows = list(csv.reader(io.StringIO(self.ledger.export_csv("all", "Asia/Shanghai").decode("utf-8-sig"))))
        self.assertEqual(rows[1][rows[0].index("model")], "'=dangerous()")
        self.assertNotIn("body", rows[0])
        self.assertNotIn("api_key", rows[0])

    def transport(self, envelope):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_invalid_json_response_still_records_reported_usage_without_text(self):
        opener = self.transport({"choices": [{"message": {"content": "private synthetic response: invalid JSON"}}],
                                 "usage": {"prompt_tokens": 10, "completion_tokens": 20}, "model": "synthetic"})
        client = ModelClient(type("Keys", (), {"get": lambda _: "synthetic-secret"})(), self.ledger)
        with patch("urllib.request.build_opener", return_value=opener), call_scope("entry", "synthetic-entry", "synthetic-job", 2):
            with self.assertRaises(AIError):
                client.call(self.settings, "private synthetic prompt", {"body": "private synthetic diary"})
        row = self.rows()["records"][0]
        self.assertEqual((row["kind"], row["attempt"], row["job_id"]), ("entry", 2, "synthetic-job"))
        self.assertEqual((row["total_tokens"], row["status"]), (30, "error"))
        self.assertIsNotNone(row["estimated_cost"])
        self.assertNotIn("private synthetic", json.dumps(row))
        self.assertNotIn("synthetic-secret", json.dumps(row))

    def test_transport_errors_and_retries_are_separate_requests(self):
        client = ModelClient(type("Keys", (), {"get": lambda _: ""})(), self.ledger)
        opener = self.transport({"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        with patch("urllib.request.build_opener", return_value=opener):
            opener.open.side_effect = TimeoutError()
            with call_scope("day", "2026-10-20", "job", 1), self.assertRaises(AIError):
                client.call(self.settings, "system", {})
            opener.open.side_effect = None
            with call_scope("day", "2026-10-20", "job", 2):
                client.call(self.settings, "system", {})
        rows = self.rows()["records"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["attempt"] for r in rows}, {1, 2})
        failed = next(r for r in rows if r["status"] == "error")
        self.assertIsNone(failed["estimated_cost"])
        self.assertEqual(failed["error_code"], "network_or_timeout")

    def test_ledger_failure_after_response_does_not_repeat_or_reject_paid_call(self):
        opener = self.transport({"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        client = ModelClient(type("Keys", (), {"get": lambda _: ""})(), self.ledger)
        with patch("urllib.request.build_opener", return_value=opener), patch.object(self.ledger, "finish", side_effect=OSError("synthetic disk full")):
            self.assertEqual(client.call(self.settings, "system", {}), {"ok": True})
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(self.ledger.warning)

    def test_ledger_failure_before_request_prevents_untracked_model_call(self):
        opener = MagicMock()
        client = ModelClient(type("Keys", (), {"get": lambda _: ""})(), self.ledger)
        with patch("urllib.request.build_opener", return_value=opener), patch.object(self.ledger, "start", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(AIError):
                client.call(self.settings, "system", {})
        opener.open.assert_not_called()


class UsageHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.load(urllib.request.urlopen(self.base + "/api/bootstrap"))["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, data=None, token=True):
        headers = {"X-Notes-Token": self.token} if token else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        return urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers,
            data=json.dumps(data).encode() if data is not None else None))

    def test_usage_read_export_and_write_require_token(self):
        for path, data in (("/api/usage", None), ("/api/usage/export", None), ("/api/usage/price", {})):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as exc:
                self.request(path, data, token=False)
            self.assertEqual(exc.exception.code, 403)
        self.assertEqual(json.load(self.request("/api/usage"))["summary"]["requests"], 0)
        self.assertIn("text/csv", self.request("/api/usage/export").headers["Content-Type"])

    def test_connection_test_is_logged_without_diary_text(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            self.request("/api/ai/test", {"ai_mode": "local", "ai_model": "synthetic", "ai_base_url": "http://127.0.0.1:1/v1"})
        rows = json.load(self.request("/api/usage"))["records"]
        self.assertEqual((rows[0]["kind"], rows[0]["total_tokens"]), ("test", 8))
        self.assertNotIn("messages", rows[0])

    def test_price_api_accepts_zero_and_rejects_stale_model_configuration(self):
        self.request("/api/settings", {"ai_mode": "off", "ai_model": "synthetic", "ai_base_url": "http://127.0.0.1:1/v1"})
        state = json.load(self.request("/api/usage"))
        payload = {"model": state["model"], "provider": state["provider"], "input_rate": "0", "output_rate": "0", "cached_rate": ""}
        self.request("/api/usage/price", payload)
        self.assertEqual(json.load(self.request("/api/usage"))["price"]["input_rate"], "0")
        self.request("/api/settings", {"ai_model": "different"})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/usage/price", payload)
        self.assertEqual(exc.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
