"""Synthetic cost regressions. Count actual client calls; never claim character counts are tokens."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.ai import AIError, Worker, validate_day, validate_entry
from app.request_context import prepare_request, receipt_key, cached_result, with_receipt
from app.store import Store, compact
from tests.test_app import FakeModel
from tests.test_growth import GrowthModel


class CountingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.calls = []

    def call(self, settings, prompt, content):
        self.calls.append(copy.deepcopy(content))
        return super().call(settings, prompt, content)


class RequestCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "synthetic", "organization_mode": "always"})
        self.model = CountingModel()
        self.worker = Worker(self.store, self.model)

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, **extra):
        return self.store.save_entry({"body": "明天联系客户。", "journal_day": "2026-10-20", **extra})

    def test_unchanged_save_keeps_revision_history_and_all_caches(self):
        entry = self.entry()
        self.worker.step(); self.worker.step()
        before = self.store.export()
        saved = self.store.save_entry({**entry, "body": "  " + entry["body"] + "  "}, entry["id"])
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(before["tables"], self.store.export()["tables"])
        self.assertFalse(self.worker.step())

    def test_entry_receipt_survives_restart_and_does_not_replay_side_effects(self):
        entry = self.entry(); self.worker.step()
        actions = self.store.actions()
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET status='running' WHERE kind='entry'")
        reopened = Store(self.temp.name)
        self.assertTrue(Worker(reopened, self.model).step())
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(reopened.actions(), actions)

    def test_nonforced_check_hits_cache_but_explicit_regeneration_calls_model(self):
        entry = self.entry(); self.worker.step()
        self.store.retry(entry_id=entry["id"], force=False); self.worker.step()
        self.assertEqual(len(self.model.calls), 1)
        self.store.retry(entry_id=entry["id"], force=True); self.worker.step()
        self.assertEqual(len(self.model.calls), 2)

    def test_service_model_and_prompt_changes_do_not_share_receipts(self):
        self.entry(); self.worker.step()
        for values in ({"ai_model": "different"}, {"ai_base_url": "http://127.0.0.1:2/v1"}):
            self.store.save_settings(values)
            entry = self.store.entries("2026-10-20")[0]
            self.store.retry(entry_id=entry["id"], force=False); self.worker.step()
        self.assertEqual(len(self.model.calls), 3)
        with patch("app.request_context.prompt_metadata", return_value={"entry": {"version": "synthetic.changed", "sha256": "different"}}):
            self.store.retry(entry_id=entry["id"], force=False); self.worker.step()
        self.assertEqual(len(self.model.calls), 4)

    def test_new_ambient_action_invalidates_entry_cache(self):
        first = self.entry(); self.worker.step()
        second = self.entry(body="后天联系另一个客户。")
        self.worker.step()
        self.store.update_action(self.store.actions()[0]["id"], {"title": "用户的修正"})
        self.store.retry(entry_id=first["id"], force=False); self.worker.step()
        self.assertEqual(len(self.model.calls), 3)

    def test_day_cache_ignores_redundant_entry_analysis_refresh(self):
        entry = self.entry(); self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 2)
        self.store.retry(entry_id=entry["id"])
        self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 3)
        self.assertFalse(self.store.summary("2026-10-20")["stale"])
        self.assertNotIn("analysis", self.model.calls[1]["records"][0])

    def test_day_source_change_and_force_generation_cannot_hit_old_cache(self):
        entry = self.entry(); self.worker.step(); self.worker.step()
        self.store.retry(day="2026-10-20", force=False); self.worker.step()
        self.assertEqual(len(self.model.calls), 2)
        self.store.retry(day="2026-10-20", force=True); self.worker.step()
        self.assertEqual(len(self.model.calls), 3)
        self.store.save_entry({**entry, "body": "后天联系客户。"}, entry["id"])
        self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 5)

    def test_six_quick_entries_share_one_delayed_day_request(self):
        worker = Worker(self.store, self.model, recap_delay=30)
        for i in range(6):
            self.entry(body=f"明天联系客户，准备第{i}份资料。")
            self.assertTrue(worker.step())
            self.assertTrue(worker.step())  # Defer the automatic recap; no client call.
        self.assertEqual(len(self.model.calls), 6)
        future = (datetime.now(timezone.utc) + timedelta(seconds=35)).isoformat(timespec="milliseconds")
        with patch("app.ai.now", return_value=future):
            self.assertTrue(worker.step())
        self.assertEqual(len(self.model.calls), 7)
        self.assertEqual(sum("day" in c for c in self.model.calls), 1)
        self.assertEqual(len(self.model.calls[-1]["records"]), 6)

    def test_explicit_day_request_bypasses_delay(self):
        self.entry()
        worker = Worker(self.store, self.model, recap_delay=30)
        worker.step(); worker.step()
        self.assertEqual(len(self.model.calls), 1)
        self.store.retry(day="2026-10-20")
        worker.step()
        self.assertEqual(len(self.model.calls), 2)

    def test_alias_roundtrip_preserves_exact_text_and_original_sources(self):
        entry = self.entry(body="明天联系客户。原文中的 r1、a1、m1 都不能改写。")
        context = {"entry": entry, "creation_local_date": "2026-10-20", "existing_actions": [],
                   "growth_enabled": True, "growth_record_id": "entry:" + entry["id"]}
        payload, decode = prepare_request("entry", context)
        self.assertEqual(payload["entry"]["body"], entry["body"])
        self.assertEqual(payload["growth_record_id"], "r1")
        self.assertNotIn("revision", payload["entry"])
        result = decode(GrowthModel().call(self.store.settings(), "", payload))
        self.assertEqual(result["growth"]["memories"][0]["sources"][0]["record_id"], "entry:" + entry["id"])
        self.assertEqual(result["growth"]["events"][0]["quote"], entry["body"])

    def test_alias_actions_and_completions_expand_only_structured_fields(self):
        entry = self.entry()
        context = {"entry": entry, "existing_actions": [{"id": "original-action", "title": "联系客户", "kind": "task", "status": "open"}]}
        payload, decode = prepare_request("entry", context)
        self.assertEqual(payload["existing_actions"][0]["id"], "a1")
        decoded = decode({"actions": [{"existing_action_id": "a1", "quote": "a1 是正文"}], "completions": [{"action_id": "a1"}]})
        self.assertEqual(decoded["actions"][0]["existing_action_id"], "original-action")
        self.assertEqual(decoded["actions"][0]["quote"], "a1 是正文")
        self.assertEqual(decoded["completions"][0]["action_id"], "original-action")

    def test_unknown_alias_does_not_get_accepted_as_a_source(self):
        entry = self.entry()
        context = {"day": "2026-10-20", "records": [entry], "actions": []}
        payload, decode = prepare_request("day", context)
        raw = {"schema_version": 1, "overview": "合成回顾", "sections": [{"category": "events", "items": [{"text": "事件", "sources": [{"entry_id": "r999", "quote": entry["body"]}]}]}]}
        with self.assertRaises(AIError): validate_day(decode(raw), context)

    def test_daily_wire_keeps_all_originals_but_drops_duplicated_analysis_and_quotes(self):
        entry = self.entry(); self.worker.step()
        context, _ = self.store.day_context("2026-10-20")
        payload, decode = prepare_request("day", context)
        self.assertEqual(payload["records"][0]["body"], entry["body"])
        self.assertEqual(payload["records"][0]["id"], "r1")
        self.assertNotIn("analysis", payload["records"][0])
        self.assertNotIn("quote", payload["actions"][0]["sources"][0])
        self.assertEqual(payload["actions"][0]["sources"][0]["entry_id"], "r1")
        self.assertLess(len(compact(payload)), len(compact(context)))

    def test_portrait_wire_omits_raw_quotes_and_restores_memory_ids(self):
        context = {"memories": [{"id": "original-memory", "detail": "保留修正与时间范围", "sources": [{"quote": "不用重复发的原文"}]}]}
        payload, decode = prepare_request("portrait", context)
        self.assertNotIn("sources", payload["memories"][0])
        self.assertEqual(decode({"items": [{"memory_ids": ["m1"]}]})["items"][0]["memory_ids"], ["original-memory"])
        self.assertIn("sources", context["memories"][0])  # Caller state was not mutated.

    def test_receipt_binds_result_and_source_identity_without_storing_service_or_key(self):
        entry = self.entry(); settings = self.store.settings()
        context = {"entry": entry, "existing_actions": []}
        key = receipt_key("entry", context, settings)
        cached = with_receipt({"schema_version": 1, "summary": "合成"}, key)
        self.assertTrue(cached_result(cached, key))
        altered = copy.deepcopy(cached); altered["summary"] = "被改写"
        self.assertFalse(cached_result(altered, key))
        other = {**context, "entry": {**entry, "id": "another-original"}}
        self.assertNotEqual(receipt_key("entry", other, settings), key)
        self.assertNotIn(settings["ai_base_url"], json.dumps(cached))

    def test_source_deletion_removes_derived_receipts_from_export(self):
        entry = self.entry(); self.worker.step(); self.worker.step()
        self.store.delete_entry(entry["id"])
        data = self.store.export()["tables"]
        self.assertEqual(data["analyses"], [])
        self.assertEqual(data["daily_summaries"], [])

    def test_cache_receipts_survive_backup_restore_and_old_format_validators(self):
        self.entry(); self.worker.step(); self.worker.step()
        bundle = self.store.export(); self.store.validate_import(bundle)
        self.store.import_data(bundle, replace=True)
        entry = self.store.entries("2026-10-20")[0]
        self.store.retry(entry_id=entry["id"], force=False)
        before = len(self.model.calls)
        self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), before)


if __name__ == "__main__":
    unittest.main()
