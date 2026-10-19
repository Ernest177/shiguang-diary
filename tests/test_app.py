import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.ai import AIError, KeyStore, ModelClient, Worker, validate_config, validate_entry
from app.server import make_server
from app.store import Store, uid


class FakeModel:
    def __init__(self, result=None, callback=None):
        self.result, self.callback = result, callback

    def call(self, settings, prompt, content):
        if self.callback:
            self.callback()
        if 'entry' in content:
            e = content['entry']
            return copy.deepcopy(self.result or {'schema_version':1,'summary':'记录了一个计划','insights':[],
                'actions':[{'title':'联系客户','kind':'task','quote':e['body'],'due_text':None,'due_date':None,'existing_action_id':None}], 'completions':[]})
        e = content['records'][0]
        return {'schema_version':1,'overview':'记录了今天的计划','sections':[{'category':'actions','items':[{'text':'计划联系客户','sources':[{'entry_id':e['id'],'quote':e['body']}]}]}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(self.temporary.name)
        # These legacy contracts exercise the model pipeline; adaptive routing has its own suite.
        self.store.save_settings({'ai_mode':'local','ai_base_url':'http://127.0.0.1:11434/v1','ai_model':'test','organization_mode':'always'})

    def tearDown(self):
        self.temporary.cleanup()

    def entry(self, body='明天联系客户', **extra):
        return self.store.save_entry({'body':body,'journal_day':'2026-10-19',**extra})

    def analyze(self, entry=None, model=None):
        entry = entry or self.entry()
        Worker(self.store, model or FakeModel()).step()
        return entry

    def test_durable_multiple_entries_and_idempotency(self):
        identifier=uid()
        first=self.entry(id=identifier)
        second=self.entry('今天看了一本书')
        self.assertEqual(self.entry(id=identifier)['id'],first['id'])
        reopened=Store(self.temporary.name)
        self.assertEqual(len(reopened.entries('2026-10-19')),2)
        self.assertNotEqual(first['id'],second['id'])

    def test_revisions_and_optimistic_conflict(self):
        e=self.entry()
        updated=self.store.save_entry({'body':'后天联系客户','journal_day':'2026-10-18','revision':1}, e['id'])
        self.assertEqual(updated['created_at'],e['created_at'])
        self.assertEqual(updated['revision'],2)
        with self.assertRaises(ValueError):
            self.store.save_entry({'body':'冲突内容','revision':1}, e['id'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM entry_versions').fetchone()[0],2)

    def test_chinese_search(self):
        self.entry('今天尝试整理摄影资料')
        self.assertEqual(len(self.store.entries(query='摄影资料')),1)
        self.assertEqual(len(self.store.entries(query='不存在')),0)

    def test_real_worker_lifecycle_and_duplicate_analysis(self):
        e=self.analyze()
        worker=Worker(self.store,FakeModel())
        worker.step()
        self.assertIsNotNone(self.store.summary('2026-10-19')['data'])
        self.store.retry(entry_id=e['id'])
        worker.step()
        self.assertEqual(len(self.store.actions()),1)
        self.assertEqual(self.store.entries('2026-10-19')[0]['analysis']['summary'],'记录了一个计划')

    def test_user_cancellation_and_edit_survive_reanalysis(self):
        e=self.analyze()
        a=self.store.actions()[0]
        self.store.update_action(a['id'],{'status':'cancelled','title':'我自己的任务标题'})
        self.store.retry(entry_id=e['id'])
        Worker(self.store,FakeModel()).step()
        result=self.store.actions()[0]
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['title'],'我自己的任务标题')

    def test_undo_action(self):
        self.analyze()
        a=self.store.actions()[0]
        change=self.store.update_action(a['id'],{'status':'done'})
        self.store.undo_action(change)
        self.assertEqual(self.store.actions()[0]['status'],'open')

    def test_stale_ai_result_is_not_applied(self):
        e=self.entry()
        def during_request():
            self.store.save_entry({'body':'新的记录','journal_day':'2026-10-19','revision':1},e['id'])
        Worker(self.store,FakeModel(callback=during_request)).step()
        current=self.store.entries('2026-10-19')[0]
        self.assertEqual(current['revision'],2)
        self.assertIsNone(current['analysis'])
        self.assertEqual(len(self.store.actions()),0)

    def test_disabled_ai_content_is_not_sent_to_day_context(self):
        self.entry('非常私人的内容',ai_enabled=False)
        context,_=self.store.day_context('2026-10-19')
        self.assertEqual(context['records'],[])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='entry'").fetchone()[0],0)

    def test_delete_clears_derivatives_keeps_manual_tasks(self):
        e=self.analyze()
        self.store.update_action(self.store.actions()[0]['id'],{'status':'done'})
        self.store.delete_entry(e['id'])
        Worker(self.store,FakeModel()).step()
        self.assertEqual(self.store.entries('2026-10-19'),[])
        self.assertEqual(self.store.actions()[0]['sources'],[])
        self.assertIsNone(self.store.summary('2026-10-19')['data'])

    def test_no_ai_keeps_data_and_waiting_jobs(self):
        self.store.save_settings({'ai_mode':'off'})
        self.entry()
        Worker(self.store,FakeModel()).step()
        self.assertEqual(len(self.store.entries('2026-10-19')),1)
        self.assertEqual(self.store.entries('2026-10-19')[0]['job_status'],'waiting')

    def test_backup_integrity_and_restore(self):
        self.entry()
        name=self.store.backup(force=True)
        backup=sqlite3.connect(Path(self.temporary.name)/'backups'/name)
        self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        backup.close()
        self.entry('之后新增的记录')
        self.store.restore_backup(name)
        self.assertEqual(len(self.store.entries('2026-10-19')),1)

    def test_export_restore_roundtrip_and_duplicate_merge(self):
        self.analyze()
        self.store.update_action(self.store.actions()[0]['id'],{'status':'done'})
        data=self.store.export()
        with tempfile.TemporaryDirectory() as directory:
            target=Store(directory)
            target.import_data(data)
            target.import_data(data)
            self.assertEqual(len(target.entries('2026-10-19')),1)
            self.assertEqual(target.actions()[0]['status'],'done')
            self.assertEqual(target.actions()[0]['sources'][0]['quote'],'明天联系客户')
            self.assertNotIn('api_key',json.dumps(data))

    def test_merge_maps_same_action_fingerprint_from_different_ids(self):
        self.analyze()
        data=self.store.export()
        imported=copy.deepcopy(data)
        old_entry=imported['tables']['entries'][0]['id']
        new_entry=uid()
        old_action=imported['tables']['actions'][0]['id']
        new_action=uid()
        for table,rows in imported['tables'].items():
            for row in rows:
                if table=='entries': row['id']=new_entry
                if table=='actions': row['id']=new_action
                if row.get('entry_id')==old_entry: row['entry_id']=new_entry
                if row.get('action_id')==old_action: row['action_id']=new_action
        self.store.import_data(imported)
        self.assertEqual(len(self.store.actions()),1)
        self.assertEqual(len(self.store.actions()[0]['sources']),2)

    def test_corrupt_import_does_not_touch_existing_data(self):
        self.entry()
        data=self.store.export()
        data['tables']['entry_versions'][0]['entry_id']='missing'
        with self.assertRaises(ValueError):
            self.store.import_data(data,replace=True)
        self.assertEqual(len(self.store.entries('2026-10-19')),1)

    def test_corrupt_analysis_import_rejected_before_writing(self):
        self.analyze()
        data=self.store.export()
        data['tables']['analyses'][0]['data']='{"summary":"broken"}'
        with self.assertRaises(ValueError):self.store.import_data(data)
        self.assertEqual(len(self.store.entries('2026-10-19')),1)

    def test_delete_immediately_removes_daily_summary_even_with_ai_off(self):
        e=self.analyze()
        Worker(self.store,FakeModel()).step()
        self.assertIsNotNone(self.store.summary('2026-10-19')['data'])
        self.store.save_settings({'ai_mode':'off'})
        self.store.delete_entry(e['id'])
        self.assertIsNone(self.store.summary('2026-10-19')['data'])

    def test_user_deleted_rejection_does_not_reappear_after_edit(self):
        e=self.analyze()
        a=self.store.actions()[0]
        self.store.update_action(a['id'],{'status':'dismissed'})
        self.store.save_entry({'body':'明天联系客户，再确认资料','journal_day':'2026-10-19','revision':1},e['id'])
        Worker(self.store,FakeModel()).step()
        self.assertEqual(len(self.store.actions()),1)
        self.assertEqual(self.store.actions()[0]['status'],'dismissed')


class ValidationTests(unittest.TestCase):
    def result(self, quote, kind='task'):
        return {'schema_version':1,'summary':'摘要','insights':[],'actions':[{'title':'联系客户','kind':kind,'quote':quote,'due_text':None,'due_date':None,'existing_action_id':None}],'completions':[]}

    def test_task_vs_opportunity_and_negation(self):
        for sentence,expected in [('明天联系客户','task'),('也许值得联系客户','opportunity'),('今天已联系客户',None),('不准备联系客户',None)]:
            result=validate_entry(self.result(sentence),{'body':sentence},[])
            self.assertEqual(result['actions'][0]['kind'] if result['actions'] else None,expected)

    def test_invented_evidence_is_rejected(self):
        with self.assertRaises(AIError):validate_entry(self.result('从未写过的内容'),{'body':'原始文字'},[])

    def test_cloud_requires_consent_and_no_local_mode_remote_fallback(self):
        for settings in [dict(ai_mode='cloud',ai_base_url='https://example.com/v1',ai_model='a',cloud_consent=False),dict(ai_mode='local',ai_base_url='https://example.com/v1',ai_model='a')]:
            with self.assertRaises(ValueError):validate_config(settings)

    def test_key_store_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            keys=KeyStore(Path(directory));keys.set('test-secret-123');self.assertEqual(keys.get(),'test-secret-123')
            if os.name=='nt':self.assertNotIn(b'test-secret-123',keys.path.read_bytes())
            keys.set('');self.assertEqual(keys.get(),'')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.server=make_server(self.directory.name)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.token=json.load(urllib.request.urlopen(self.url+'/api/bootstrap'))['token']

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.directory.cleanup()

    def request(self,path,body=None,headers=None):
        headers={'X-Notes-Token':self.token,**(headers or {})}
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(self.url+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        return urllib.request.urlopen(req)

    def test_write_requires_token_and_trusted_origin(self):
        for headers in [{'X-Notes-Token':''},{'Origin':'https://evil.example'},{'Host':'evil.example'}]:
            with self.assertRaises(urllib.error.HTTPError) as context:self.request('/api/entries/save',{'body':'test'},headers)
            self.assertEqual(context.exception.code,403)

    def test_static_csp_and_api_roundtrip(self):
        response=self.request('/')
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        self.assertIn('拾光',response.read().decode())
        self.request('/api/entries/save',{'body':'中文持久化','journal_day':'2026-10-19'})
        result=json.load(self.request('/api/state?day=2026-10-19'))
        self.assertEqual(result['entries'][0]['body'],'中文持久化')

    def test_key_is_bound_to_provider_address(self):
        initial={'ai_mode':'local','ai_base_url':'http://127.0.0.1:11434/v1','ai_model':'test','api_key':'test-secret'}
        self.request('/api/settings',initial)
        self.assertTrue(self.server.application.keys.path.exists())
        self.request('/api/settings',{'ai_mode':'off'})
        self.assertEqual(self.server.application.keys.get(),'test-secret')
        self.request('/api/settings',{'ai_base_url':'http://127.0.0.1:11435/v1'})
        self.assertFalse(self.server.application.keys.path.exists())

    def test_invalid_secret_does_not_echo_secret_in_error(self):
        with self.assertRaises(urllib.error.HTTPError) as context:self.request('/api/settings',{'api_key':'private\r\nsecret'})
        self.assertNotIn('private',context.exception.read().decode())


if __name__=='__main__':
    unittest.main()
