"""Synthetic coverage and cache boundaries for bounded personal portraits."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from app.about import encode
from app.memory_selection import period_key, select_memories
from app.store import Store


def memory(identifier, period="2026-10-01", **changes):
    value = {"id": identifier, "title": "一次尝试", "detail": "【合成】在不同时间主动表达自己的想法。",
             "category": "fact", "horizon": "recent", "period": period, "status": "pending", "correction": "", "user_modified": 0,
             "sources": [{"record_id": "entry:" + identifier, "quote": "主动表达", "period": period}]}
    return {**value, **changes}


class MemorySelectionTests(unittest.TestCase):
    def test_history_decisions_and_categories_survive_a_burst_of_recent_notes(self):
        values = [memory(f"new-{i:02d}", f"2026-10-{i + 1:02d}") for i in range(28)]
        values += [memory("old", "2018-06"), memory("corrected", "2020", status="corrected", correction="我只在充分准备时主动表达。"),
                   memory("value", "2021", category="value", horizon="long_term"),
                   memory("preference", "2022", category="preference"), memory("goal", "2023", category="goal")]
        original = copy.deepcopy(values)
        selected, scope = select_memories(values)
        ids = {m["id"] for m in selected}
        self.assertTrue({"old", "new-27", "corrected", "value", "preference", "goal"} <= ids)
        self.assertEqual(len(selected), 24)
        corrected = next(m for m in selected if m["id"] == "corrected")
        self.assertEqual(corrected["detail"], "我只在充分准备时主动表达。")
        self.assertNotIn("一次尝试", encode(corrected))
        self.assertNotIn('"sources"', encode(selected))
        self.assertEqual(scope["period_start"], "2018-06")
        self.assertEqual(values, original, "selection must never modify or merge stored memories")

    def test_stable_order_independent_of_ingestion_and_no_similarity_merging(self):
        values = [memory("a", "2026-09-01"), memory("b", "2026-10-01")]
        self.assertEqual(select_memories(values), select_memories(list(reversed(values))))
        selected, scope = select_memories(values)
        self.assertEqual(len(selected), 2)
        self.assertEqual(scope["source_count"], 2)

    def test_budget_skips_full_items_without_chopping_a_correction(self):
        values = [memory("large", status="corrected", correction="很长的修正" * 1000), memory("small")]
        selected, scope = select_memories(values, max_items=3, max_characters=400)
        self.assertEqual([m["id"] for m in selected], ["small"])
        self.assertLessEqual(scope["selected_characters"], 400)

    def test_ambiguous_dates_do_not_become_recent_due_to_ingestion(self):
        for value in ("大学期间", "最近", "2026-02-30", "2026-13", "0", None):
            self.assertEqual(period_key(value), "")
        self.assertEqual(period_key("2020"), "2020")
        self.assertEqual(period_key("2024-02-29"), "2024-02-29")
        _, scope = select_memories([memory("unknown", "大学期间")])
        self.assertEqual(scope["period_start"], "")

    def test_unselected_memory_only_updates_coverage_without_invalidating_cached_portrait(self):
        values = [memory(f"day-{i:02d}", f"2026-10-{i + 1:02d}") for i in range(28)]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.save_settings({"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "synthetic"})
            store.set_growth_policy({"growth_enabled": True})
            with patch.object(store, "memories", return_value=values):
                context, before = store.portrait_context()
            extra = memory("unselected", "2026-10-02")
            # Give this occurrence a source already covered by a higher priority item.
            extra["sources"][0]["record_id"] = "entry:day-01"
            with patch.object(store, "memories", return_value=values + [extra]):
                updated, after = store.portrait_context()
                self.assertEqual(context["memories"], updated["memories"])
                self.assertEqual(before, after)
                with store.connection() as db:
                    db.execute("INSERT INTO personal_reports VALUES(?,?,?,?,?,?,?,?)", (
                        "portrait", "current", before, json.dumps({"schema_version": 1, "items": [], "scope": context["scope"]}),
                        "synthetic", "portrait.v2", "synthetic", "2026-10-20T00:00:00+00:00"))
                state = store.about_state("2026-10")
                self.assertEqual(state["reports"]["portrait"]["data"]["scope"]["available"], 29)
                self.assertEqual(len(values), 28)


if __name__ == "__main__":
    unittest.main()
