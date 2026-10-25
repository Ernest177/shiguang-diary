"""Acceptance: evidence, explicit hypotheses, user corrections, and retraction."""
import copy
import contextlib
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.ai import AIError, Worker
from app.about_ai import validate_memories, validate_portrait, validate_month
from app.server import make_server
from app.store import Store, SCHEMA


class PersonalModel:
    def __init__(self, callback=None, fail=False):
        self.callback, self.fail = callback, fail
        self.requests = []

    def call(self, settings, prompt, context):
        self.requests.append(copy.deepcopy(context))
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        if self.fail:
            raise AIError("合成接口失败")
        if "memories" in context:
            memory = context["memories"][0]
            return {"schema_version":1,"items":[{"text":memory["detail"],"category":"strengths","certainty":"observation","memory_ids":[memory["id"]]}]}
        if "month" in context:
            record = next(r for r in context["records"] if r["period"].startswith(context["month"]))
            return {"schema_version":1,"items":[{"text":"【合成替身】本月尝试表达想法","category":"events","comparison":False,"sources":[{"record_id":record["id"],"quote":record["body"]}]}]}
        record = context["records"][0]
        return {"schema_version":1,"memories":[{"title":"表达想法的一次尝试","detail":"【合成替身】主动表达过想法","category":"fact","horizon":"recent","period":record["period"] or "所述经历期间",
                 "sources":[{"record_id":record["id"],"quote":record["body"]}]}]}


class AboutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.save_settings({"ai_mode":"local","ai_base_url":"http://127.0.0.1:1/v1","ai_model":"synthetic","organization_mode":"always"})

    def tearDown(self):
        self.tmp.cleanup()

    def experience(self, **extra):
        return self.store.save_experience({"title":"第一次主动发言","period":"2026-10","chapter":"成长","body":"我在讨论会上主动表达了自己的想法。","ai_enabled":True,**extra})

    def generate(self, model=None):
        self.store.request_about("memory")
        worker = Worker(self.store, model or PersonalModel())
        self.assertTrue(worker.step())
        self.assertTrue(worker.step())
        return worker

    def test_history_defaults_private_and_diaries_need_separate_opt_in(self):
        self.experience(ai_enabled=False)
        entry=self.store.save_entry({"body":"只允许当日整理","journal_day":"2026-10-19"})
        self.assertEqual(self.store.memory_context()[0]["records"],[])
        self.store.set_memory_permission(entry["id"],True)
        self.assertEqual(len(self.store.memory_context()[0]["records"]),1)
        self.assertEqual(self.store.about_state("2026-10")["entry_source_count"],1)

    def test_experience_save_retry_is_idempotent(self):
        first=self.experience(client_id="same-draft-request")
        second=self.experience(client_id="same-draft-request")
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(len(self.store.about_state("2026-10")["experiences"]),1)

    def test_full_job_lifecycle_has_evidence_and_does_not_self_confirm(self):
        self.experience()
        self.generate()
        state=self.store.about_state("2026-10")
        self.assertEqual(len(state["memories"]),1)
        self.assertEqual(state["memories"][0]["status"],"pending")
        item=state["reports"]["portrait"]["data"]["items"][0]
        self.assertEqual(item["certainty"],"hypothesis")
        self.assertEqual(item["period"],"2026-10")
        self.assertEqual(state["reports"]["portrait"]["prompt_version"],"portrait.v3")

    def test_user_correction_survives_regeneration_and_reaches_portrait(self):
        self.experience()
        self.generate()
        memory=self.store.memories()[0]
        self.store.update_memory(memory["id"],{"status":"corrected","correction":"我只在准备充分时更愿意发言。","updated_at":memory["updated_at"]})
        self.assertNotIn("portrait",self.store.about_state("2026-10")["reports"])
        model=PersonalModel()
        self.generate(model)
        current=self.store.memories()[0]
        self.assertEqual(current["id"],memory["id"])
        self.assertEqual(current["correction"],"我只在准备充分时更愿意发言。")
        self.assertEqual(model.requests[-1]["memories"][0]["detail"],current["correction"])
        self.assertEqual(self.store.about_state("2026-10")["reports"]["portrait"]["data"]["items"][0]["text"],current["correction"])

    def test_stale_memory_edit_is_rejected(self):
        self.experience();self.generate()
        memory=self.store.memories()[0]
        self.store.update_memory(memory["id"],{"status":"confirmed","updated_at":memory["updated_at"]})
        with self.assertRaises(ValueError):
            self.store.update_memory(memory["id"],{"status":"corrected","correction":"旧窗口修改","updated_at":"old"})

    def test_forgetting_removes_text_and_blocks_same_evidence(self):
        self.experience();self.generate()
        memory=self.store.memories()[0]
        self.store.forget_memory(memory["id"])
        self.assertEqual(self.store.memories(),[])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM personal_reports").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM memories").fetchone()[0],0)
            self.assertNotIn(memory["detail"],db.execute("SELECT source_hashes FROM memory_suppressions").fetchone()[0])
        self.store.request_about("memory")
        Worker(self.store,PersonalModel()).step()
        self.assertEqual(self.store.memories(),[])

    def test_deleting_source_retracts_confirmed_memory_and_portrait(self):
        experience=self.experience();self.generate()
        memory=self.store.memories()[0]
        self.store.update_memory(memory["id"],{"status":"confirmed","updated_at":memory["updated_at"]})
        self.store.request_about("portrait");Worker(self.store,PersonalModel()).step()
        self.store.delete_experience(experience["id"])
        self.assertEqual(self.store.memories(),[])
        self.assertEqual(self.store.about_state("2026-10")["reports"],{})
        self.assertEqual(self.store.export()["tables"]["memories"],[])

    def test_source_revision_change_blocks_inflight_model_result(self):
        experience=self.experience()
        self.store.request_about("memory")
        def change():
            self.experience(id=experience["id"],revision=1,body="更新后的真实想法")
        Worker(self.store,PersonalModel(callback=change)).step()
        self.assertEqual(self.store.memories(),[])

    def test_opt_out_blocks_inflight_model_result(self):
        experience=self.experience()
        self.store.request_about("memory")
        Worker(self.store,PersonalModel(callback=lambda:self.experience(id=experience["id"],revision=1,ai_enabled=False))).step()
        self.assertEqual(self.store.memories(),[])
        self.assertEqual(self.store.memory_context()[0]["records"],[])

    def test_diary_opt_out_retracts_sources_without_removing_original(self):
        entry=self.store.save_entry({"body":"愿意长期保留的经历","journal_day":"2026-10-19"})
        self.store.set_memory_permission(entry["id"],True)
        with self.store.connection() as db: db.execute("DELETE FROM jobs")
        self.generate()
        self.store.set_memory_permission(entry["id"],False)
        self.assertEqual(self.store.memories(),[])
        self.assertEqual(self.store.entries("2026-10-19")[0]["body"],entry["body"])
        self.assertEqual(self.store.about_state("2026-10")["reports"],{})

    def test_fabricated_memory_quote_and_single_event_pattern_rejected(self):
        self.experience()
        context=self.store.memory_context()[0]
        result=PersonalModel().call({},"",context)
        result["memories"][0]["sources"][0]["quote"]="没有发生过的事"
        with self.assertRaises(AIError):validate_memories(result,context)
        result=PersonalModel().call({},"",context)
        result["memories"][0]["category"]="pattern"
        with self.assertRaises(AIError):validate_memories(result,context)

    def test_recent_state_must_keep_time_range(self):
        self.experience()
        context=self.store.memory_context()[0]
        result=PersonalModel().call({},"",context)
        result["memories"][0]["period"]=""
        with self.assertRaises(AIError):validate_memories(result,context)
        context["records"][0]["body"]="最近一周工作很忙。"
        result=PersonalModel().call({},"",context)
        result["memories"][0].update(horizon="long_term",period="")
        checked=validate_memories(result,context)["memories"][0]
        self.assertEqual(checked["horizon"],"recent")
        self.assertEqual(checked["period"],"2026-10")

    def test_portrait_cannot_cite_unknown_memory(self):
        with self.assertRaises(AIError):validate_portrait({"schema_version":1,"items":[{"text":"无依据的画像","category":"values","certainty":"hypothesis","memory_ids":["missing"]}]},{"memories":[]})

    def test_month_comparison_needs_both_months_and_excludes_other_months(self):
        self.experience(period="2026-10")
        self.experience(title="上个月",period="2026-09")
        self.experience(title="更早",period="2026-08")
        self.experience(title="日期模糊",period="大学期间")
        context,_=self.store.month_context("2026-10")
        self.assertEqual({r["period"] for r in context["records"]},{"2026-09","2026-10"})
        result=PersonalModel().call({},"",context)
        result["items"][0]["comparison"]=True
        with self.assertRaises(AIError):validate_month(result,context)
        previous=next(r for r in context["records"] if r["period"]=="2026-09")
        result["items"][0]["sources"].append({"record_id":previous["id"],"quote":previous["body"]})
        self.assertEqual(len(validate_month(result,context)["items"]),1)

    def test_month_job_and_source_deletion_retraction(self):
        experience=self.experience()
        self.store.request_about("month","2026-10")
        Worker(self.store,PersonalModel()).step()
        self.assertIn("month",self.store.about_state("2026-10")["reports"])
        self.store.delete_experience(experience["id"])
        self.assertNotIn("month",self.store.about_state("2026-10")["reports"])

    def test_personal_jobs_do_not_retry_paid_calls_automatically(self):
        self.experience();self.store.request_about("memory")
        model=PersonalModel(fail=True)
        worker=Worker(self.store,model)
        worker.step();worker.step()
        self.assertEqual(len(model.requests),1)
        self.assertEqual(self.store.about_state("2026-10")["jobs"][0]["status"],"failed")
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET status='running' WHERE kind='memory'")
        restarted=Store(self.tmp.name)
        self.assertEqual(restarted.about_state("2026-10")["jobs"][0]["status"],"failed")

    def test_v2_roundtrip_keeps_experiences_permissions_and_corrections(self):
        self.experience();self.generate()
        memory=self.store.memories()[0]
        self.store.update_memory(memory["id"],{"status":"corrected","correction":"只在准备充分时","updated_at":memory["updated_at"]})
        bundle=self.store.export()
        with tempfile.TemporaryDirectory() as folder:
            target=Store(folder);target.import_data(bundle,replace=True)
            self.assertEqual(target.memories()[0]["correction"],"只在准备充分时")
            target.import_data(bundle)
            self.assertEqual(len(target.memories()),1)
            self.assertEqual(len(target.about_state("2026-10")["experiences"]),1)

    def test_corrupt_memory_import_rejected_before_replacing_data(self):
        self.experience();self.generate()
        bundle=self.store.export()
        bundle["tables"]["memory_sources"][0]["quote"]="伪造依据"
        with self.assertRaises(ValueError):self.store.import_data(bundle,replace=True)
        self.assertEqual(len(self.store.memories()),1)

    def test_v1_migration_has_snapshot_and_can_restore_old_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"journal.sqlite3"
            legacy=sqlite3.connect(path)
            legacy.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS experiences")[0]+"PRAGMA user_version=1;")
            legacy.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)",('legacy','旧版本日记','2026-10-19','Asia/Shanghai','2026-10-19T08:00:00+00:00','2026-10-19T08:00:00+00:00',1,0))
            legacy.execute("INSERT INTO entry_versions VALUES(?,?,?,?,?,?,?)",('legacy',1,'旧版本日记','2026-10-19','Asia/Shanghai',0,'2026-10-19T08:00:00+00:00'))
            legacy.commit();legacy.close()
            target=Store(folder)
            snapshot=list((Path(folder)/'upgrade-backups').glob('*.sqlite3'))
            self.assertEqual(len(snapshot),1)
            with contextlib.closing(sqlite3.connect(snapshot[0])) as old:
                self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(old.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            Store(folder)
            self.assertEqual(len(list((Path(folder)/'upgrade-backups').glob('*.sqlite3'))),1)
            target.backup(force=True)
            old_backup=Path(folder)/'backups'/'shiguang-legacy.sqlite3'
            shutil.copyfile(snapshot[0],old_backup)
            target.restore_backup(old_backup.name)
            self.assertEqual(target.entries('2026-10-19')[0]['body'],'旧版本日记')
            bundle=target.export()
            self.assertEqual(bundle['schema_version'],5)
            legacy_bundle={**bundle,'schema_version':1,'tables':{k:v for k,v in bundle['tables'].items() if k not in ('experiences','memory_permissions','memories','memory_sources','memory_suppressions','personal_reports','memory_exclusions','growth_receipts','attachments','media_versions','entry_media')}}
            target.import_data(legacy_bundle,replace=True)
            self.assertEqual(target.entries('2026-10-19')[0]['body'],'旧版本日记')

    def test_http_about_endpoints_keep_local_request_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            server=make_server(folder)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                token=json.load(urllib.request.urlopen(base+'/api/bootstrap'))['token']
                payload=json.dumps({'title':'合成经历','body':'一次主动表达'}).encode()
                request=urllib.request.Request(base+'/api/experiences/save',data=payload,headers={'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(request)
                request.add_header('X-Notes-Token',token)
                with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['experience']['id'])
                request=urllib.request.Request(base+'/api/about?month=2026-10',headers={'X-Notes-Token':token})
                with urllib.request.urlopen(request) as response:
                    state=json.load(response)
                    self.assertEqual(state['source_count'],0)
                    self.assertEqual(len(state['experiences']),1)
            finally:
                server.shutdown();server.server_close()


if __name__=='__main__':
    unittest.main()
