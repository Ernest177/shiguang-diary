"""Adaptive presentation and request routing, with fictional content only."""
import copy
import tempfile
import unittest

from app.ai import Worker
from app.organization import brief_original, day_presentation, near_original, portrait_readiness, portrait_presentation, original_memory_preview
from app.server import Application
from app.store import Store
from tests.test_request_costs import CountingModel
from tests.test_growth import GrowthModel


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({"ai_mode": "local", "ai_base_url": "http://127.0.0.1:1/v1", "ai_model": "synthetic"})
        self.model = CountingModel()
        self.worker = Worker(self.store, self.model)

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, body="明天联系客户。", **extra):
        return self.store.save_entry({"body": body, "journal_day": "2026-10-20", **extra})

    def test_single_brief_entry_keeps_original_without_additional_day_call(self):
        entry = self.entry(); self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 1)
        summary = self.store.summary("2026-10-20")
        self.assertEqual(summary["model"], "本机原文")
        self.assertTrue(summary["data"]["presentation"]["local"])
        self.assertEqual(summary["data"]["presentation"]["records"][0]["body"], entry["body"])
        self.assertEqual(len(self.store.actions()), 1)
        self.store.validate_import(self.store.export())

    def test_growth_extraction_still_happens_but_premature_portrait_does_not(self):
        self.store.set_growth_policy({"growth_enabled": True})
        self.entry(); model = GrowthModel(); worker = Worker(self.store, model)
        worker.step(); worker.step()
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(self.store.memories()), 1)
        self.store.request_about("memory")
        self.assertFalse(worker.step())
        state = self.store.about_state("2026-10")
        self.assertFalse(state["organization"]["ready"])
        self.assertEqual(state["original_preview"][0]["label"], "原文")

    def test_existing_ai_review_is_represented_as_original_without_regeneration(self):
        self.store.save_settings({"organization_mode": "always"})
        entry = self.entry(); self.worker.step(); self.worker.step()
        before = len(self.model.calls)
        self.store.save_settings({"organization_mode": "auto"})
        data = self.store.summary("2026-10-20")["data"]
        self.assertEqual(data["presentation"]["records"][0]["body"], entry["body"])
        self.assertFalse(data["presentation"]["local"], "Past AI use must not be relabeled as free local generation")
        self.assertEqual(len(self.model.calls), before)

    def test_explicit_language_difficulty_uses_ai_without_a_manual_checkbox(self):
        self.entry("这段表达不清，帮我梳理这段想法：我想先学习还是先工作，有些矛盾。")
        self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 2)
        self.assertNotIn("presentation", self.store.summary("2026-10-20")["data"])

    def test_project_planning_is_not_misread_as_request_to_rewrite_diary(self):
        self.entry("计划花两天时间理清项目思路。")
        context, _ = self.store.day_context("2026-10-20")
        self.assertTrue(brief_original(context, self.store.settings()))
        context["records"][0]["body"] = "早点睡。整理桌面。散步。买书。喝水。做饭。听歌。浇花。休息。"
        self.assertTrue(brief_original(context, self.store.settings()), "Many short sentences alone do not imply complex expression")

    def test_multi_entry_and_long_or_many_paragraph_records_use_ai(self):
        self.entry(); self.entry("后天联系另一个客户。")
        self.worker.step(); self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 3)
        context, _ = self.store.day_context("2026-10-20")
        self.assertFalse(brief_original(context, self.store.settings()))
        context["records"] = [{"id": "fiction", "body": "较长的想法" * 150}]
        self.assertFalse(brief_original(context, self.store.settings()))
        context["records"][0]["body"] = "\n".join("片段" + str(i) for i in range(7))
        self.assertFalse(brief_original(context, self.store.settings()))

    def test_force_generation_bypasses_local_selection_and_remains_visible(self):
        self.entry(); self.worker.step(); self.worker.step()
        self.store.retry(day="2026-10-20", force=True); self.worker.step()
        self.assertEqual(len(self.model.calls), 2)
        self.assertTrue(self.store.summary("2026-10-20")["data"]["_organization"]["forced"])
        self.assertNotIn("presentation", self.store.summary("2026-10-20")["data"])

    def test_always_mode_preserves_opt_in_model_workflow(self):
        self.store.save_settings({"organization_mode": "always"})
        self.entry(); self.worker.step(); self.worker.step()
        self.assertEqual(len(self.model.calls), 2)

    def test_similarity_threshold_can_change_without_discarding_useful_compression(self):
        original = "今天晚上整理了读书笔记"
        self.assertTrue(near_original("今天晚上整理读书笔记", original, 90))
        self.assertFalse(near_original("今天晚上整理读书笔记", original, 99))
        self.assertFalse(near_original("整理读书笔记", original, 60), "A shorter useful summary should not become the longer original")

    def test_similarity_reuses_the_original_including_negation_and_never_alters_actions(self):
        body = "今天没有完成项目资料的整理工作"
        data = {"schema_version": 1, "overview": "回顾", "sections": [
            {"category": "insights", "items": [{"text": "今天完成项目资料的整理工作", "sources": [{"entry_id": "a", "quote": body}]}]},
            {"category": "actions", "items": [{"text": "按当前行动状态展示", "sources": [{"entry_id": "a", "quote": body}]}]}]}
        context = {"records": [{"id": "a", "body": body}, {"id": "b", "body": "另一条记录"}]}
        presented = day_presentation(data, context, self.store.settings())
        self.assertEqual(presented["sections"][0]["items"][0]["text"], body)
        self.assertEqual(presented["sections"][1], data["sections"][1])
        self.assertNotEqual(presented, data)
        self.assertEqual(data["sections"][0]["items"][0]["text"], "今天完成项目资料的整理工作")

    def test_threshold_changes_do_not_retry_failed_paid_jobs(self):
        self.entry()
        with self.store.connection() as db: db.execute("UPDATE jobs SET status='failed'")
        self.store.save_settings({"original_similarity": 95})
        self.assertFalse(self.worker.step())

    def test_privacy_opt_out_still_excludes_originals_from_recap_and_profile(self):
        self.entry(ai_enabled=False)
        self.worker.step()
        self.assertIsNone(self.store.summary("2026-10-20")["data"])
        self.assertEqual(self.store.about_state("2026-10")["original_preview"], [])
        self.assertEqual(self.model.calls, [])

    def memories(self):
        return [{"id": "m" + str(i), "detail": "原来的线索" + str(i), "status": "pending", "correction": "",
                 "sources": [{"record_id": "entry:r" + str(i), "quote": "原话片段" + str(i), "period": "2026-10-" + ("20" if i < 2 else "21"), "title": "合成日记"}]} for i in range(3)]

    def test_multiple_memories_from_one_source_or_one_period_do_not_make_a_portrait(self):
        memories = self.memories()
        self.assertTrue(portrait_readiness(memories)["ready"])
        for m in memories: m["sources"][0]["period"] = "2026-10-20"
        self.assertFalse(portrait_readiness(memories)["ready"])
        for m in memories: m["sources"][0]["record_id"] = "entry:one"
        self.assertEqual(portrait_readiness(memories)["records"], 1)

    def test_portrait_view_keeps_cross_record_interpretation_and_hides_rephrasing(self):
        memories = self.memories()
        data = {"schema_version": 1, "items": [
            {"text": "原来的线索0", "memory_ids": ["m0"], "category": "focus"},
            {"text": "把前后不同的尝试联系起来，可以进一步探索适合自己的表达方式。", "memory_ids": ["m0", "m2"], "category": "questions"}]}
        result = portrait_presentation(data, memories, self.store.settings())
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["memory_ids"], ["m0", "m2"])
        self.assertEqual(len(data["items"]), 2)

    def test_correction_takes_precedence_in_local_original_preview(self):
        self.store.set_growth_policy({"growth_enabled": True})
        self.entry(); Worker(self.store, GrowthModel()).step()
        memory = self.store.memories()[0]
        self.store.update_memory(memory["id"], {"status": "corrected", "correction": "这是我补充的实际情况", "updated_at": memory["updated_at"]})
        preview = self.store.about_state("2026-10")["original_preview"][0]
        self.assertEqual((preview["label"], preview["text"]), ("你的修正", "这是我补充的实际情况"))

    def test_repeated_originals_from_different_dates_remain_distinct_occurrences(self):
        memories = self.memories()
        memories[0]["sources"][0]["quote"] = "最近晚上总会担心第二天的工作。"
        memories[2]["sources"][0]["quote"] = "最近晚上总会担心第二天的工作。"
        # A single memory may cite the same concern on multiple dates.
        memories[0]["sources"].append(memories[2]["sources"][0])
        preview = original_memory_preview([memories[0]])
        self.assertEqual(len(preview), 2)
        self.assertEqual(preview[0]["text"], preview[1]["text"])
        self.assertNotEqual(preview[0]["source"]["record_id"], preview[1]["source"]["record_id"])
        self.assertNotEqual(preview[0]["source"]["period"], preview[1]["source"]["period"])

    def test_original_preview_collapses_only_identical_citations_from_the_same_record(self):
        memory = self.memories()[0]
        duplicate = copy.deepcopy(memory)
        similar = copy.deepcopy(memory)
        similar["sources"][0]["quote"] = "原话片段里还有另一个细节"
        preview = original_memory_preview([memory, duplicate, similar])
        self.assertEqual([p["text"] for p in preview], [memory["sources"][0]["quote"], similar["sources"][0]["quote"]])

    def test_portrait_is_not_hidden_for_matching_another_ai_generated_summary(self):
        memories = self.memories()
        generated = "这段时间你在尝试不同的表达方式，并逐渐发现提前准备能够减轻沟通时的压力。"
        memories[0]["detail"] = generated
        data = {"schema_version": 1, "items": [{"text": generated, "memory_ids": ["m0", "m2"], "category": "focus"}]}
        self.assertEqual(portrait_presentation(data, memories, self.store.settings()), data)

    def test_policy_input_is_validated_independently_of_cloud_permission(self):
        app = Application(self.temp.name)
        self.assertEqual(app.settings_input({})["organization_mode"], "auto")
        for values in ({"organization_mode": "unknown"}, {"original_similarity": 100}, {"original_similarity": True}, {"brief_entry_chars": 0}):
            with self.assertRaises(ValueError): app.settings_input(values)


if __name__ == "__main__":
    unittest.main()
