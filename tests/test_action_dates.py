"""Synthetic regressions for duration deadlines and deliberate manual choices."""
import copy
import tempfile
import unittest

from app.ai import Worker, validate_entry
from app.store import Store


def response(quote):
    return {"schema_version": 1, "summary": "合成计划", "insights": [], "completions": [],
            "actions": [{"title": "整理项目思路", "kind": "task", "quote": quote,
                         "due_text": None, "due_date": None, "existing_action_id": None}]}


class DurationTests(unittest.TestCase):
    def resolve(self, body, created="2026-10-19T16:30:00+00:00", zone="Asia/Shanghai", **changes):
        raw = response(body)
        raw["actions"][0].update(changes)
        entry = {"body": body, "created_at": created, "timezone": zone, "journal_day": "2026-10-01"}
        return validate_entry(raw, entry, [])["actions"][0]

    def test_missing_model_date_is_recovered_from_explicit_duration(self):
        for phrase in ("花两天时间整理项目思路", "我打算用2天整理项目思路", "给自己两天时间整理项目思路", "两天内整理项目思路", "2天后整理项目思路", "花两天时间整理 Atlas-BI 项目"):
            with self.subTest(phrase=phrase):
                action = self.resolve(phrase)
                self.assertEqual(action["due_date"], "2026-10-22")
                self.assertIn(action["due_text"], phrase)

    def test_start_date_can_shift_the_duration(self):
        action = self.resolve("从明天开始，花两天时间整理项目思路")
        self.assertEqual(action["due_date"], "2026-10-23")
        self.assertEqual(action["due_text"], "从明天开始，花两天时间")

    def test_local_creation_date_not_journal_day_or_processing_day(self):
        self.assertEqual(self.resolve("花两天整理资料")["due_date"], "2026-10-22")
        self.assertEqual(self.resolve("花两天整理资料", zone="UTC")["due_date"], "2026-10-21")

    def test_calendar_boundaries_and_chinese_numbers(self):
        cases = [("2026-12-31T12:00:00+00:00", "用两天整理资料", "2027-01-02"),
                 ("2028-02-28T12:00:00+00:00", "两天内整理资料", "2028-03-01"),
                 ("2026-10-20T00:00:00+00:00", "用十一天整理资料", "2026-10-31"),
                 ("2026-10-20T00:00:00+00:00", "用一周整理资料", "2026-10-27")]
        for created, phrase, expected in cases:
            with self.subTest(phrase=phrase, created=created):
                self.assertEqual(self.resolve(phrase, created=created)["due_date"], expected)

    def test_estimates_history_ranges_and_workdays_do_not_gain_dates(self):
        for phrase in ("大概花两天整理资料", "花两三天整理资料", "花两天左右整理资料", "用两个工作日整理资料",
                       "用2到3天整理资料", "花两天半整理资料", "已经花两天整理资料，接下来联系客户",
                       "明天联系客户，再花两天整理资料", "下周开始用两天整理资料", "用两天整理资料，再用三天复盘",
                       "用两天整理资料，再休息三天", "花两天到三天整理资料", "花两天多整理资料"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.resolve(phrase)["due_date"])

    def test_only_the_action_quote_supplies_a_duration(self):
        entry = {"body": "花两天整理资料。联系客户。", "created_at": "2026-10-20T00:00:00+00:00", "timezone": "Asia/Shanghai"}
        action = validate_entry(response("联系客户"), entry, [])["actions"][0]
        self.assertIsNone(action["due_date"])

    def test_non_commitments_and_missing_anchor_do_not_gain_dates(self):
        self.assertIsNone(self.resolve("也许值得花两天整理资料", kind="opportunity")["due_date"])
        self.assertIsNone(validate_entry(response("花两天整理资料"), {"body": "花两天整理资料"}, [])["actions"][0]["due_date"])

    def test_explicit_duration_corrects_wrong_model_arithmetic(self):
        action = self.resolve("花两天整理资料", due_text="花两天", due_date="2026-10-21")
        self.assertEqual(action["due_date"], "2026-10-22")
        explicit = self.resolve("原计划花两天整理资料，但改成今天完成", due_text="今天完成", due_date="2026-10-20")
        self.assertEqual(explicit["due_date"], "2026-10-20")


class ActionDateLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.store.save_settings({"ai_mode": "local", "ai_base_url": "http://127.0.0.1:11434/v1", "ai_model": "synthetic", "organization_mode": "always"})
        self.entry = self.store.save_entry({"body": "花两天时间整理项目思路", "journal_day": "2026-10-20"})
        with self.store.connection() as db:
            db.execute("UPDATE entries SET created_at=? WHERE id=?", ("2026-10-19T16:30:00+00:00", self.entry["id"]))

        class SyntheticModel:
            def call(model, settings, prompt, context):
                return copy.deepcopy(response(context["entry"]["body"]))
        self.worker = Worker(self.store, SyntheticModel())
        self.worker.step()

    def tearDown(self):
        self.directory.cleanup()

    def test_reanalysis_repairs_an_older_unmodified_action(self):
        identifier = self.store.actions()[0]["id"]
        with self.store.connection() as db:
            db.execute("UPDATE actions SET due_text=NULL,due_date=NULL WHERE id=?", (identifier,))
        self.store.retry(entry_id=self.entry["id"])
        self.worker.step()
        actions = self.store.actions()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["due_date"], "2026-10-22")

    def test_manual_date_survives_reanalysis_and_supports_undo(self):
        original = self.store.actions()[0]
        history = self.store.update_action(original["id"], {"due_date": "2026-10-25"})
        self.store.retry(entry_id=self.entry["id"])
        self.worker.step()
        edited = self.store.actions()[0]
        self.assertEqual(edited["due_date"], "2026-10-25")
        self.assertIsNone(edited["due_text"])
        self.store.undo_action(history)
        self.assertEqual(self.store.actions()[0]["due_date"], original["due_date"])
        self.assertEqual(self.store.actions()[0]["due_text"], original["due_text"])

    def test_title_only_edit_preserves_time_quote_and_date(self):
        original = self.store.actions()[0]
        self.store.update_action(original["id"], {"title": "我的标题", "due_date": original["due_date"]})
        self.assertEqual(self.store.actions()[0]["due_text"], "花两天时间")

    def test_clearing_date_does_not_leave_a_stale_time_quote(self):
        self.store.update_action(self.store.actions()[0]["id"], {"due_date": None})
        self.assertIsNone(self.store.actions()[0]["due_date"])
        self.assertIsNone(self.store.actions()[0]["due_text"])


if __name__ == "__main__":
    unittest.main()
