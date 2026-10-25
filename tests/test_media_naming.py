"""Synthetic-only contracts for opt-in vision, real accounting, caching and name history."""
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from app.ai import AIError, ModelClient
from app.media_naming import MediaNamer, preview_image, uninformative_name
from app.server import make_server
from app.store import Store
from app.usage import UsageLedger

PNG = b'\x89PNG\r\n\x1a\nsynthetic private original; never a model input'


def preview(width=640, height=480, metadata=False):
    # Structural JPEG fixture for the size/metadata gate; not used for decoder tests.
    sof = b'\xff\xc0\x00\x11\x08' + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + b'\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01'
    extra = b'\xff\xe1\x00\x08Exif\x00\x00' if metadata else b''
    raw = b'\xff\xd8' + extra + sof + b'\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\x00\xff\xd9'
    return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({'ai_mode':'local', 'ai_base_url':'http://127.0.0.1:1/v1', 'ai_model':'synthetic-vision', 'image_naming':True})
        self.ledger = UsageLedger(self.temp.name)
        self.client = ModelClient(type('Keys', (), {'get':lambda _:''})(), self.ledger)
        self.namer = MediaNamer(self.store, self.client)
        self.photo = self.upload('128jfcfsad.png')
        self.payload = {'id':self.photo['id'], 'preview':preview()}

    def tearDown(self):
        for temporary, _ in getattr(self.store, 'media_imports', {}).values(): temporary.cleanup()
        self.temp.cleanup()

    def upload(self, name):
        return self.store.upload_media(io.BytesIO(PNG), len(PNG), name)

    def transport(self, answer=None):
        answer = {'name':'合成画面', 'confidence':.9} if answer is None else answer
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'model':'synthetic-vision',
            'choices':[{'message':{'content':json.dumps(answer, ensure_ascii=False)}}],
            'usage':{'prompt_tokens':230, 'completion_tokens':14, 'total_tokens':244}}).encode()
        opener = MagicMock(); opener.open.return_value = response
        return opener

    def test_local_name_filter_keeps_descriptive_names_without_spending(self):
        for name in ('128jfcfsad.jpg','IMG_20261024_153030.jpg','mmexport1792859293528.jpg','af713acebe771bc4843bb1e112aa4f11.png','12345678.png','粘贴的图片.png'):
            with self.subTest(name=name): self.assertTrue(uninformative_name(name))
        for name in ('室内合影_01.jpg','毕业_2020-07-01.png','summer2026.jpg','family_trip_2026.jpg','holiday2026.jpg'):
            with self.subTest(name=name): self.assertFalse(uninformative_name(name))
        photo = self.upload('毕业合影.png')
        with patch('urllib.request.build_opener') as transport:
            result = self.namer.suggest({'id':photo['id']})
        self.assertEqual(result['status'], 'kept'); transport.assert_not_called()

    def test_separate_opt_in_is_required_even_with_diary_cloud_consent(self):
        self.store.save_settings({'image_naming':False, 'cloud_consent':True})
        with patch('urllib.request.build_opener') as transport, self.assertRaisesRegex(ValueError, '开启'):
            self.namer.suggest(self.payload)
        transport.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(Store(folder).settings()['image_naming'])

    def test_small_preview_payload_and_actual_usage_are_recorded_without_private_context(self):
        opener = self.transport()
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.namer.suggest(self.payload)['label'], '合成画面')
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body['messages'][1]['content'][1], {'type':'image_url','image_url':{'url':preview()}})
        self.assertEqual(body['max_tokens'],128)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],25)
        self.assertNotIn(self.photo['sha256'], request.data.decode())
        self.assertNotIn(self.photo['name'], request.data.decode())
        self.assertNotIn(base64.b64encode(PNG).decode(), request.data.decode())
        rows = self.ledger.state(self.store.settings(), 'all')['records']
        self.assertEqual((rows[0]['kind'],rows[0]['total_tokens'],rows[0]['status']),('image_name',244,'success'))
        self.assertNotIn('合成画面',json.dumps(rows, ensure_ascii=False))

    def test_same_original_is_cached_across_upload_names_and_diary_dates(self):
        other = self.upload('IMG_12345.png')
        opener = self.transport()
        with patch('urllib.request.build_opener',return_value=opener):
            first = self.namer.suggest(self.payload)
            again = self.namer.suggest({'id':other['id']})  # Cache needs no new preview.
        self.assertFalse(first['cached']); self.assertTrue(again['cached'])
        self.assertEqual(opener.open.call_count,1)
        for day in ('2026-10-24','2026-10-25'):
            self.store.save_entry({'body':'', 'journal_day':day,'attachments':[{'id':other['id'],'position':0,'label':again['label']}]})
        self.assertEqual(self.store.entries('2026-10-24')[0]['attachments'][0]['display_name'],'合成画面_2026-10-24.png')

    def test_cache_is_scoped_to_model_and_endpoint(self):
        opener = self.transport()
        with patch('urllib.request.build_opener',return_value=opener):
            self.namer.suggest(self.payload)
            self.store.save_settings({'ai_model':'synthetic-vision-v2'})
            self.namer.suggest(self.payload)
            self.store.save_settings({'ai_base_url':'http://127.0.0.1:2/v1'})
            self.namer.suggest(self.payload)
        self.assertEqual(opener.open.call_count,3)

    def test_aliyun_vision_request_disables_thinking_and_preserves_price_tracking(self):
        self.store.save_settings({'ai_mode':'cloud','ai_base_url':'https://synthetic.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                                 'ai_model':'qwen3.8-flash','cloud_consent':True})
        opener=self.transport()
        with patch('urllib.request.build_opener',return_value=opener): self.namer.suggest(self.payload)
        self.assertFalse(json.loads(opener.open.call_args.args[0].data)['enable_thinking'])
        row=self.ledger.state(self.store.settings(),'all')['records'][0]
        self.assertIsNotNone(row['estimated_cost'])

    def test_low_confidence_is_cached_without_inventing_a_name(self):
        opener = self.transport({'name':'不可靠地点','confidence':.2})
        with patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(self.namer.suggest(self.payload)['status'],'uncertain')
            self.assertEqual(self.namer.suggest(self.payload)['label'],'')
        self.assertEqual(opener.open.call_count,1)

    def test_invalid_or_hallucinated_file_paths_are_not_saved(self):
        for answer in ({'name':'../../outside','confidence':.9},{'name':'画面','confidence':True},{'name':'画面','confidence':2}):
            with patch('urllib.request.build_opener',return_value=self.transport(answer)), self.assertRaises(AIError):
                self.namer.suggest(self.payload)
        with self.store.connection() as db: self.assertEqual(db.execute('SELECT count(*) FROM image_name_cache').fetchone()[0],0)

    def test_error_is_not_retried_and_original_can_still_be_saved(self):
        opener = self.transport(); opener.open.side_effect = TimeoutError()
        with patch('urllib.request.build_opener',return_value=opener), self.assertRaisesRegex(AIError,'附件已保留，请保存日记'):
            self.namer.suggest(self.payload)
        self.assertEqual(opener.open.call_count,1)
        entry = self.store.save_entry({'body':'','journal_day':'2026-10-25','attachments':[{'id':self.photo['id'],'position':0}]})
        self.assertEqual(entry['attachments'][0]['name'],self.photo['name'])
        self.assertEqual(self.store.media_file(self.photo['id'])[1].read_bytes(),PNG)
        self.assertEqual(self.ledger.state(self.store.settings(),'all')['records'][0]['status'],'error')

    def test_external_urls_oversized_images_and_metadata_never_reach_provider(self):
        for value in ('https://example.com/a.jpg', preview(769,480), preview(metadata=True), 'data:image/jpeg;base64,'+'A'*400000, 'data:image/jpeg;base64,not-base64'):
            with self.subTest(value=value[:40]), patch('urllib.request.build_opener') as transport, self.assertRaises(ValueError):
                self.namer.suggest({**self.payload,'preview':value})
            transport.assert_not_called()
        self.assertEqual(preview_image(preview()),preview())

    def test_manual_request_can_name_a_meaningful_file_without_renaming_original(self):
        photo = self.upload('临时名字.png')
        opener = self.transport()
        with patch('urllib.request.build_opener',return_value=opener):
            result = self.namer.suggest({'id':photo['id'],'preview':preview(),'manual':True})
        self.assertEqual(result['label'],'合成画面')
        self.assertEqual(self.store.media_file(photo['id'])[0]['name'],'临时名字.png')

    def test_concurrent_duplicate_requests_share_a_paid_result(self):
        opener = self.transport()
        with patch('urllib.request.build_opener',return_value=opener), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.namer.suggest,[self.payload,self.payload]))
        self.assertEqual(opener.open.call_count,1)
        self.assertEqual(sorted(r['cached'] for r in results),[False,True])

    def test_label_changes_have_media_history_without_reanalyzing_text(self):
        entry = self.store.save_entry({'body':'合成记录','journal_day':'2026-10-25','ai_enabled':False,
            'attachments':[{'id':self.photo['id'],'position':0,'label':'初次命名'}]})
        with self.store.connection() as db: before_jobs=[tuple(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]
        entry['attachments'][0]['label']='我的修正'
        changed=self.store.save_entry(entry,entry['id'])
        self.assertEqual(changed['revision'],entry['revision'])
        self.assertEqual(changed['media_revision'],2)
        self.assertEqual(self.store.media_download_name(self.photo,entry['id'],1),'初次命名_2026-10-25.png')
        self.assertEqual(self.store.media_download_name(self.photo,entry['id'],2),'我的修正_2026-10-25.png')
        with self.store.connection() as db: self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')],before_jobs)
        legacy={**changed,'attachments':[{'id':self.photo['id'],'position':0}]}
        self.assertEqual(self.store.save_entry(legacy,entry['id'])['attachments'][0]['label'],'我的修正')
        # Text-only API edits must retain the current name, including when no layout is supplied.
        changed.pop('attachments'); changed['body']='更新了合成记录'
        final=self.store.save_entry(changed,entry['id'])
        self.assertEqual(final['attachments'][0]['label'],'我的修正')

    def test_portable_and_sqlite_restore_keep_name_history(self):
        entry=self.store.save_entry({'body':'','journal_day':'2026-10-25',
            'attachments':[{'id':self.photo['id'],'position':0,'label':'合成画面'}]})
        self.store.backup(force=True)
        archive=self.store.portable_archive()
        try:
            with tempfile.TemporaryDirectory() as folder:
                other=Store(folder)
                ticket=other.preview_archive(archive)['ticket']
                other.restore_archive(ticket,replace=True)
                self.assertEqual(other.entries('2026-10-25')[0]['attachments'][0]['label'],'合成画面')
                self.assertEqual(other.media_download_name(other.entries('2026-10-25')[0]['attachments'][0],entry['id'],1),'合成画面_2026-10-25.png')
            self.store.restore_backup(self.store.backups()[0]['name'])
            self.assertEqual(self.store.entries('2026-10-25')[0]['attachments'][0]['label'],'合成画面')
        finally: archive.unlink(missing_ok=True)

    def test_v4_json_and_database_upgrade_add_empty_labels_without_changing_originals(self):
        self.store.save_entry({'body':'','journal_day':'2026-10-25','attachments':[{'id':self.photo['id'],'position':0}]})
        bundle=self.store.export(); bundle['schema_version']=4
        for row in bundle['tables']['entry_media']: del row['label']
        self.assertEqual(self.store.validate_import(bundle)['duplicates'],1)
        with self.store.connection() as db:
            db.execute('ALTER TABLE entry_media DROP COLUMN label'); db.execute('DROP TABLE image_name_cache'); db.execute('PRAGMA user_version=4')
        upgraded=Store(self.temp.name)
        self.assertEqual(upgraded.entries('2026-10-25')[0]['attachments'][0]['label'],'')
        self.assertEqual(upgraded.media_file(self.photo['id'])[1].read_bytes(),PNG)
        snapshots=list((Path(self.temp.name)/'upgrade-backups').glob('before-v5-*.sqlite3'))
        self.assertEqual(len(snapshots),1)
        with contextlib.closing(sqlite3.connect(snapshots[0])) as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)


class NamingHTTPTests(unittest.TestCase):
    def test_read_cookie_cannot_request_a_paid_name_and_default_is_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            server=make_server(folder); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            try:
                base=f'http://127.0.0.1:{server.server_port}'
                with urllib.request.urlopen(base+'/api/bootstrap') as response:
                    cookie=response.headers['Set-Cookie'].split(';')[0]; boot=json.load(response)
                photo=server.application.store.upload_media(io.BytesIO(PNG),len(PNG),'128jfcfsad.png')
                for headers,status in (({'Cookie':cookie},403),({'X-Notes-Token':boot['token']},400)):
                    request=urllib.request.Request(base+'/api/media/name',data=json.dumps({'id':photo['id'],'preview':preview()}).encode(),
                        headers={**headers,'Content-Type':'application/json'})
                    with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(request)
                    self.assertEqual(caught.exception.code,status)
                self.assertFalse(boot['settings']['image_naming'])
            finally: server.shutdown(); server.server_close()


if __name__ == '__main__': unittest.main()
