"""Synthetic lifecycle, cost guards and cache checks; no private data or model calls."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import threading
import urllib.request
import urllib.error

from app.ai import Worker, AIError
from app.about import encode
from app.growth import chunks, validate_growth, record_hash
from app.store import Store, SCHEMA
from app.server import make_server
from app.usage import UsageLedger, call_scope


def growth(record, empty=False):
    quote = record["body"][:80]
    return {"schema_version": 1, "memories": [] if empty else [{"title": "一次新的尝试", "detail": quote,
        "category": "fact", "horizon": "recent", "period": record["period"] or "经历所述期间",
        "sources": [{"record_id": record["id"], "quote": quote}]}],
        "events": [] if empty else [{"title": "留下生活片段", "detail": quote, "kind": "moment", "quote": quote}]}


class GrowthModel:
    def __init__(self, callback=None, empty=False, fail=False, ledger=None):
        self.requests, self.callback = [], callback
        self.empty, self.fail, self.ledger = empty, fail, ledger

    def call(self, settings, prompt, context):
        self.requests.append(copy.deepcopy(context))
        if self.callback:
            fn, self.callback = self.callback, None
            fn()
        if self.fail:
            raise AIError("合成故障")
        if self.ledger:
            identifier = self.ledger.start(settings)
            self.ledger.finish(identifier, {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}, "synthetic", "success", 2)
        if "entry" in context:
            entry = context["entry"]
            result = {"schema_version": 1, "summary": "合成日记摘要", "insights": [], "actions": [], "completions": []}
            if context.get("growth_enabled"):
                result["growth"] = growth({"id": context["growth_record_id"], "body": entry["body"], "period": entry["journal_day"]}, self.empty)
            return result
        if "memories" in context:
            return {"schema_version": 1, "items": [{"text": context["memories"][0]["detail"], "category": "focus", "certainty": "hypothesis", "memory_ids": [context["memories"][0]["id"]]}]}
        if "month" in context:
            record = next(r for r in context["records"] if r["period"].startswith(context["month"]))
            return {"schema_version": 1, "items": [{"text": "片段回顾", "category": "events", "comparison": False, "sources": [{"record_id": record["id"], "quote": record["body"][:20]}]}]}
        if "part" in context:
            return growth(context["records"][0], self.empty)
        return {"schema_version": 1, "overview": "合成回顾", "sections": []}


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.save_settings({"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "synthetic", "organization_mode": "always"})
        self.store.set_growth_policy({"growth_enabled": True})

    def tearDown(self):
        self.tmp.cleanup()

    def diary(self, body="【合成】今天主动分享了自己的想法。", **extra):
        return self.store.save_entry({"body": body, "journal_day": "2026-10-20", **extra})

    def clear_jobs(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM jobs")

    def backfill(self, record_id, model=None):
        with self.store.connection() as db:
            record = self.store.growth_record(record_id, db)
            self.store.enqueue(db, "growth", record_id, record["revision"])
        model = model or GrowthModel()
        Worker(self.store, model).step()
        return model

    def test_global_selection_needs_no_individual_permissions_and_respects_private(self):
        self.diary(); self.diary(ai_enabled=False, body="【合成】私密日记")
        self.store.save_experience({"title": "私密经历", "body": "不参与AI"})
        self.assertEqual(len(self.store.memory_context()[0]["records"]), 1)
        self.assertEqual(self.store.entries("2026-10-20")[0]["memory_enabled"], 0)

    def test_new_diary_piggybacks_one_request_and_repeat_update_is_free(self):
        entry = self.diary()
        model = GrowthModel(); Worker(self.store, model).step()
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(self.store.memories()), 1)
        self.assertEqual(self.store.growth_status()["complete"], 1)
        self.clear_jobs()
        self.store.request_about("memory")
        Worker(self.store, model).step()
        count = len(model.requests)
        self.store.request_about("memory")
        self.assertFalse(Worker(self.store, model).step())
        self.assertEqual(len(model.requests), count)

    def test_empty_result_is_cached_and_not_queued_again(self):
        self.diary(); model = GrowthModel(empty=True)
        Worker(self.store, model).step(); self.clear_jobs()
        self.store.request_about("memory")
        self.assertFalse(Worker(self.store, model).step())
        self.assertEqual(self.store.growth_status()["complete"], 1)

    def test_repeated_update_preserves_queued_and_inflight_portrait_ticket(self):
        self.diary(); Worker(self.store, GrowthModel()).step(); self.clear_jobs()
        self.store.request_about("memory")
        with self.store.connection() as db: before = db.execute("SELECT id FROM jobs WHERE kind='portrait'").fetchone()[0]
        self.store.request_about("memory")
        with self.store.connection() as db: self.assertEqual(db.execute("SELECT id FROM jobs WHERE kind='portrait'").fetchone()[0], before)
        model = GrowthModel(callback=lambda: self.store.request_about("memory"))
        Worker(self.store, model).step()
        self.assertIn("portrait", self.store.about_state("2026-10")["reports"])
        self.assertEqual(len(model.requests), 1)

    def test_empty_new_entry_preserves_unchanged_portrait_without_paid_update(self):
        self.diary(); model = GrowthModel(); Worker(self.store, model).step(); self.clear_jobs()
        self.store.request_about("portrait"); Worker(self.store, model).step()
        self.diary(body="【合成】只是随手一记")
        Worker(self.store, GrowthModel(empty=True)).step()
        self.assertIn("portrait", self.store.about_state("2026-10")["reports"])
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM jobs WHERE kind='portrait' AND status IN ('queued','running')").fetchone())

    def test_same_title_from_different_sources_keeps_independent_evidence(self):
        first = self.diary(); Worker(self.store, GrowthModel()).step()
        second = self.diary(); Worker(self.store, GrowthModel()).step()
        self.assertEqual(len(self.store.memories()), 2)
        self.assertTrue(all(len(m["sources"]) == 1 for m in self.store.memories()))
        self.store.delete_entry(first["id"])
        self.assertEqual(len(self.store.memories()), 1)
        self.assertEqual(self.store.memories()[0]["sources"][0]["record_id"], "entry:" + second["id"])
        self.store.validate_import(self.store.export())

    def test_historical_long_entry_processes_bounded_chunks_and_resumes(self):
        entry = self.diary(body="【合成】" + "学习表达与回看生活。" * 1300)
        self.clear_jobs()
        model = self.backfill("entry:" + entry["id"])
        self.assertLessEqual(len(model.requests[0]["records"][0]["body"]), 4000)
        self.store.set_growth_policy({"growth_paused": True})
        self.assertFalse(Worker(self.store, model).step())
        self.store.set_growth_policy({"growth_paused": False})
        while self.store.growth_status()["pending"]:
            self.assertTrue(Worker(self.store, model).step())
        sent = len(model.requests)
        self.assertEqual(sent, len(chunks({"body": entry["body"]})))
        self.store.request_about("memory")
        self.assertEqual(self.store.growth_status()["complete"], 1)

    def test_inflight_source_edit_retracts_cache_and_old_result(self):
        entry = self.diary(); self.clear_jobs()
        model = GrowthModel(callback=lambda: self.store.save_entry({**entry, "body": "【合成】修改后的原文"}, entry["id"]))
        self.backfill("entry:" + entry["id"], model)
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.growth_status()["complete"], 0)

    def test_inflight_exclusion_and_pause_block_results(self):
        for action in (lambda e: self.store.set_memory_permission(e["id"], False),
                       lambda e: self.store.set_growth_policy({"growth_paused": True})):
            self.store.set_growth_policy({"growth_paused": False})
            entry = self.diary(); self.clear_jobs()
            self.backfill("entry:" + entry["id"], GrowthModel(callback=lambda: action(entry)))
            self.assertEqual(self.store.memories(), [])

    def test_exclusion_survives_reenable_and_original_remains_in_local_search(self):
        entry = self.diary(); Worker(self.store, GrowthModel()).step()
        self.store.set_memory_permission(entry["id"], False)
        self.store.set_growth_policy({"growth_enabled": False})
        self.store.set_growth_policy({"growth_enabled": True})
        self.assertEqual(self.store.memories(), [])
        self.assertEqual(self.store.growth_status()["total"], 0)
        self.assertEqual(self.store.timeline("主动")["total"], 1)

    def test_fabricated_quotes_and_single_record_patterns_rejected(self):
        record = {"id": "entry:synthetic", "body": "原文", "period": "2026-10-20"}
        result = growth(record); result["events"][0]["quote"] = "伪造"
        with self.assertRaises(AIError): validate_growth(result, record)
        result = growth(record); result["memories"][0]["category"] = "pattern"
        with self.assertRaises(AIError): validate_growth(result, record)

    def test_corrections_and_suppressions_survive_incremental_processing(self):
        entry = self.diary(); Worker(self.store, GrowthModel()).step()
        memory = self.store.memories()[0]
        self.store.update_memory(memory["id"], {"status": "corrected", "correction": "只在准备充分时表达", "updated_at": memory["updated_at"]})
        self.clear_jobs(); self.diary(body="【合成】另一天的表达"); Worker(self.store, GrowthModel()).step()
        self.assertEqual(next(m for m in self.store.memories() if m["id"] == memory["id"])["correction"], "只在准备充分时表达")
        self.store.forget_memory(memory["id"])
        self.clear_jobs()
        with self.store.connection() as db:
            db.execute("DELETE FROM growth_receipts WHERE record_id=?", ("entry:" + entry["id"],))
        self.backfill("entry:" + entry["id"])
        self.assertFalse(any(m["detail"] == entry["body"] for m in self.store.memories()))

    def test_portrait_bound_and_originals_not_transmitted(self):
        for i in range(35):
            entry = self.diary(body=f"【合成片段{i}】一次经历"); self.clear_jobs()
            self.backfill("entry:" + entry["id"])
        context, _ = self.store.portrait_context()
        self.assertLessEqual(len(context["memories"]), 24)
        self.assertLessEqual(len(encode(context["memories"])), 24000)
        self.assertNotIn('"sources"', encode(context))
        self.assertEqual(context["scope"]["available"], 35)

    def test_month_uses_bounded_cached_excerpts_and_records_scope(self):
        for i in range(12):
            entry = self.diary(body=f"【合成{i}】有价值的片段。" + "原文其他细节" * 1000)
            Worker(self.store, GrowthModel()).step(); self.clear_jobs()
        context, _ = self.store.month_context("2026-10")
        self.assertLessEqual(len(context["records"]), 10)
        self.assertTrue(all(len(r["body"]) <= 1600 for r in context["records"]))
        self.store.request_about("month", "2026-10")
        Worker(self.store, GrowthModel()).step()
        report = self.store.about_state("2026-10")["reports"]["month"]
        self.assertEqual(report["data"]["scope"]["available"], 12)

    def test_failure_never_automatically_retries_paid_growth_request(self):
        entry = self.diary(); self.clear_jobs()
        model = self.backfill("entry:" + entry["id"], GrowthModel(fail=True))
        with self.store.connection() as db: self.store.schedule_growth(db)
        self.assertFalse(Worker(self.store, model).step())
        self.assertEqual(len(model.requests), 1)
        self.store.request_about("memory")
        self.assertTrue(Worker(self.store, GrowthModel()).step())

    def test_daily_limit_and_month_budget_defer_without_network(self):
        ledger = UsageLedger(self.tmp.name)
        settings = self.store.settings()
        ledger.save_price(settings, {"provider": __import__('app.usage', fromlist=['provider_key']).provider_key(settings), "model": "synthetic", "input_rate": "1", "output_rate": "2"})
        self.store.set_growth_policy({"growth_daily_calls": 1, "growth_monthly_budget": "0.01"})
        with call_scope("growth", "synthetic"):
            identifier = ledger.start(settings)
            ledger.finish(identifier, {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}, "synthetic", "success", 1)
        entry = self.diary(); self.clear_jobs()
        model = self.backfill("entry:" + entry["id"], GrowthModel(ledger=ledger))
        self.assertEqual(model.requests, [])
        self.assertEqual(self.store.growth_status()["deferred"], 1)
        self.store.set_growth_policy({"growth_daily_calls": 10})
        with call_scope("portrait", "synthetic"):
            identifier = ledger.start(settings)
            ledger.finish(identifier, {"prompt_tokens": 20000, "completion_tokens": 100, "total_tokens": 20100}, "synthetic", "success", 1)
        self.assertTrue(Worker(self.store, model).step())
        self.assertEqual(model.requests, [])
        self.assertIn("预算", self.store.growth_status()["notice"])

    def test_v3_roundtrip_preserves_receipts_but_does_not_enable_cloud(self):
        self.diary(); Worker(self.store, GrowthModel()).step()
        bundle = self.store.export()
        with tempfile.TemporaryDirectory() as folder:
            target = Store(folder); target.import_data(bundle, replace=True)
            self.assertFalse(target.settings()["growth_enabled"])
            self.assertEqual(target.growth_status()["complete"], 1)
            self.assertEqual(len(target.memories()), 1)
            damaged = copy.deepcopy(bundle)
            value = json.loads(damaged["tables"]["growth_receipts"][0]["data"])
            value["chunks"][0]["events"][0]["quote"] = "不存在的原文"
            damaged["tables"]["growth_receipts"][0]["data"] = encode(value)
            with self.assertRaises(ValueError): target.import_data(damaged, replace=True)
            self.assertEqual(len(target.memories()), 1)

    def test_history_backlog_reserves_a_call_for_interim_portrait(self):
        self.diary(); Worker(self.store, GrowthModel()).step(); self.clear_jobs()
        ledger = UsageLedger(self.tmp.name)
        settings = self.store.settings()
        ledger.save_price(settings, {"provider": __import__('app.usage', fromlist=['provider_key']).provider_key(settings), "model": "synthetic", "input_rate": "1", "output_rate": "2"})
        self.store.set_growth_policy({"growth_daily_calls": 2})
        with call_scope("growth", "synthetic"):
            identifier = ledger.start(settings)
            ledger.finish(identifier, {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}, "synthetic", "success", 1)
        self.store.save_experience({"title": "【合成】旧经历", "body": "更早的一次尝试", "ai_enabled": True})
        self.store.save_experience({"title": "【合成】另一经历", "body": "另一种尝试", "ai_enabled": True})
        model = GrowthModel(ledger=ledger)
        self.assertTrue(Worker(self.store, model).step())
        self.assertEqual(model.requests, [])
        self.assertEqual(self.store.growth_status()["deferred"], 2)
        self.store.request_about("portrait")  # Make the coalesced portrait due now.
        with self.store.connection() as db: db.execute("UPDATE jobs SET retry_at=NULL WHERE kind='portrait'")
        self.assertTrue(Worker(self.store, model).step())
        self.assertEqual(len(model.requests), 1)
        self.assertIn("memories", model.requests[0])

    def test_disabling_automation_keeps_archive_and_export_restorable(self):
        self.diary(); Worker(self.store, GrowthModel()).step()
        self.store.set_growth_policy({"growth_enabled": False})
        self.assertEqual(len(self.store.memories()), 1)
        self.store.validate_import(self.store.export())

    def test_v3_sqlite_snapshot_restores_effective_automatic_sources_and_progress(self):
        entry = self.diary(); Worker(self.store, GrowthModel()).step()
        name = self.store.backup(force=True, label="growth-test")
        self.store.delete_entry(entry["id"])
        self.store.restore_backup(name)
        self.assertEqual(len(self.store.memories()), 1)
        self.assertEqual(self.store.growth_status()["complete"], 1)

    def test_local_timeline_paginates_all_originals_and_retracts_deleted_events(self):
        for i in range(34): self.diary(body=f"【合成{i}】一起散步")
        self.assertEqual(len(self.store.timeline()["items"]), 30)
        self.assertEqual(len(self.store.timeline(offset=30)["items"]), 4)
        self.assertEqual(self.store.timeline("散步")["total"], 34)
        self.assertEqual(self.store.timeline(kind="moment")["total"], 0)
        self.clear_jobs()
        entry = self.diary(body="【合成】路上听到鸟鸣")
        Worker(self.store, GrowthModel()).step()
        self.assertEqual(self.store.timeline(kind="moment")["total"], 1)
        self.store.delete_entry(entry["id"])
        self.assertEqual(self.store.timeline(kind="moment")["total"], 0)

    def test_http_policy_timeline_validation_and_token_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(folder)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base + "/api/bootstrap") as response: token = json.load(response)["token"]
                payload = json.dumps({"growth_enabled": True}).encode()
                request = urllib.request.Request(base + "/api/growth/policy", data=payload, headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(request)
                request.add_header("X-Notes-Token", token)
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 400)  # AI is still disabled.
                server.application.store.save_entry({"body": "【合成】仅本机保存", "journal_day": "2026-10-20", "ai_enabled": False})
                request = urllib.request.Request(base + "/api/growth/timeline?q=" + urllib.parse.quote("仅本机"), headers={"X-Notes-Token": token})
                with urllib.request.urlopen(request) as response: self.assertEqual(json.load(response)["total"], 1)
                with self.assertRaises(ValueError): self.store.set_growth_policy({"growth_daily_calls": 0})
                with self.assertRaises(ValueError): self.store.set_growth_policy({"growth_monthly_budget": "NaN"})
            finally:
                server.shutdown(); server.server_close()

    def test_v2_upgrade_snapshot_and_legacy_json_import(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS memory_exclusions")[0] + "PRAGMA user_version=2;")
            db.close()
            target = Store(folder)
            self.assertEqual(len(list((Path(folder) / "upgrade-backups").glob("before-v4-*.sqlite3"))), 1)
            bundle = target.export(); bundle["schema_version"] = 2
            del bundle["tables"]["memory_exclusions"]; del bundle["tables"]["growth_receipts"]
            for table in ("attachments", "media_versions", "entry_media"): del bundle["tables"][table]
            target.import_data(bundle, replace=True)
            with target.connection() as db: self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
