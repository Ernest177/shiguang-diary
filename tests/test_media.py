"""Synthetic originals only: durable mixed media, privacy, range playback and portable restore."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from unittest.mock import patch

from app.ai import Worker
from app.media import MEDIA_TABLES, object_name
from app.server import make_server
from app.store import Store, SCHEMA

PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic image original'
VIDEO = b'\x00\x00\x00\x18ftypisom' + bytes(range(256)) * 16
DAY = '2026-10-25'


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        for temporary, _ in getattr(self.store, 'media_imports', {}).values(): temporary.cleanup()
        self.tmp.cleanup()

    def upload(self, data=PNG, name='合成照片.png'):
        return self.store.upload_media(io.BytesIO(data), len(data), name)

    def entry(self, body='出发🌿\n回家后继续记录。', **extra):
        return self.store.save_entry({'body':body, 'journal_day':DAY, **extra})

    def update(self, entry, attachments, body=None):
        return self.store.save_entry({**entry, 'body':entry['body'] if body is None else body,
            'attachments':attachments}, entry['id'])

    def test_content_addressed_dedup_originals_and_unicode_layout(self):
        photo, again = self.upload(), self.upload(name='另一个名字.png')
        self.assertNotEqual(photo['id'], again['id'])
        self.assertEqual(photo['sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(len(list(self.store.media_root().rglob('*.png'))), 1)
        entry = self.entry(attachments=[{'id':photo['id'], 'position':3}, {'id':again['id'], 'position':3}])
        self.assertEqual([a['position'] for a in self.store.entries(DAY)[0]['attachments']], [3,3])
        self.assertEqual(entry['media_revision'], 1)
        self.assertEqual(self.store.media_file(photo['id'])[1].read_bytes(), PNG)
        self.assertEqual(self.store.validate_import(self.store.export())['attachments'], 2)

    def test_sniff_limits_truncation_and_no_partial_metadata(self):
        for data in (b'<svg onload="alert(1)"></svg>', b'<html>hello</html>', b'not an image'):
            with self.assertRaises(ValueError): self.upload(data, 'fake.jpg')
        with self.assertRaises(ValueError): self.store.upload_media(io.BytesIO(PNG), len(PNG)+10, 'partial.png')
        with self.assertRaises(ValueError): self.store.upload_media(io.BytesIO(), 513*1024*1024, 'huge.mp4')
        with patch('app.media.MAX_IMAGE', 8):
            with self.assertRaisesRegex(ValueError, '图片最大'): self.upload()
        self.assertEqual(list((Path(self.tmp.name)/'media-incoming').glob('*.part')), [])
        with self.store.connection() as db: self.assertEqual(db.execute('SELECT count(*) FROM attachments').fetchone()[0], 0)

    def test_discard_draft_releases_only_unshared_originals(self):
        first, second = self.upload(), self.upload()
        path = self.store.media_file(first['id'])[1]
        self.store.discard_media(first['id'])
        self.assertTrue(path.exists())
        self.store.discard_media(second['id'])
        self.assertFalse(path.exists())

    def test_media_only_edits_preserve_analysis_jobs_receipts_and_text_revision(self):
        entry = self.entry()
        with self.store.connection() as db:
            before_jobs = [tuple(r) for r in db.execute('SELECT * FROM jobs')]
            before_versions = [tuple(r) for r in db.execute('SELECT * FROM entry_versions')]
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?)', (entry['id'], 1, '{}', 'synthetic', entry['updated_at']))
        photo = self.upload()
        edited = self.update(entry, [{'id':photo['id'], 'position':3}])
        self.assertEqual(edited['revision'], entry['revision'])
        self.assertEqual(edited['updated_at'], entry['updated_at'])
        with self.store.connection() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM jobs')], before_jobs)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM entry_versions')], before_versions)
            self.assertEqual(db.execute('SELECT data FROM analyses').fetchone()[0], '{}')
        with self.assertRaisesRegex(ValueError, '附件已在其他窗口'):
            self.update(entry, [])
        removed = self.update(edited, [])
        self.assertEqual(removed['media_revision'], 2)
        self.assertEqual(removed['revision'], 1)
        self.assertEqual(len(self.store.media_history(entry['id'])[1]['attachments']), 1)
        self.assertTrue(self.store.media_file(photo['id'])[1].exists(), 'history retains its original')

    def test_media_is_absent_from_model_context_and_inflight_edit_keeps_paid_result(self):
        photo = self.upload(name='secret-photo-name.png')
        entry = self.entry(body='今天的感受', attachments=[{'id':photo['id'], 'position':0}])
        self.store.save_settings({'ai_mode':'local', 'ai_base_url':'http://127.0.0.1:11434/v1', 'ai_model':'test'})
        calls, store = [], self.store
        class Model:
            def call(self, settings, prompt, content):
                wire = json.dumps(content, ensure_ascii=False)
                self_test.assertNotIn(photo['sha256'], wire)
                self_test.assertNotIn(photo['name'], wire)
                self_test.assertNotIn('attachments', wire)
                calls.append(wire)
                if 'entry' in content:
                    store.save_entry({**entry, 'attachments':[], 'media_revision':1}, entry['id'])
                    return {'schema_version':1,'summary':'今天的感受','insights':[], 'actions':[], 'completions':[]}
                raise AssertionError('a short original review must not add a model request')
        self_test = self
        worker = Worker(store, Model())
        for _ in range(6): worker.step()
        current = store.entries(DAY)[0]
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(current['analysis'])
        self.assertEqual(current['attachments'], [])
        self.assertEqual(current['revision'], 1)

    def test_attachment_only_entry_does_not_call_ai_and_invalid_save_is_atomic(self):
        photo = self.upload()
        entry = self.entry(body='', attachments=[{'id':photo['id'], 'position':0}])
        self.assertFalse(entry['ai_enabled'])
        class ForbiddenModel:
            def call(self, *_): raise AssertionError('media must not invoke a model')
        self.store.save_settings({'ai_mode':'local', 'ai_base_url':'http://127.0.0.1:11434/v1', 'ai_model':'test'})
        worker = Worker(self.store, ForbiddenModel())
        for _ in range(3): worker.step()
        before = self.store.export()['tables']
        with self.assertRaises(ValueError): self.update(entry, [])
        with self.assertRaises(ValueError): self.update(entry, [{'id':'unknown', 'position':0}], body='changed')
        self.assertEqual(self.store.export()['tables'], before)
        self.assertEqual(self.store.validate_import(self.store.export())['entries'], 1)

    def test_text_update_clamps_legacy_layout_and_retains_media_history(self):
        photo = self.upload()
        first = self.entry(attachments=[{'id':photo['id'], 'position':10}])
        edited = self.store.save_entry({'body':'短句', 'journal_day':DAY, 'revision':1}, first['id'])
        self.assertEqual(edited['attachments'][0]['position'], 2)
        self.assertEqual(edited['revision'], 2)
        self.assertEqual(edited['media_revision'], 2)
        history = self.store.media_history(first['id'])
        self.assertEqual(history[1]['body'], first['body'])
        self.assertEqual(history[1]['attachments'][0]['position'], 10)
        self.store.delete_entry(first['id'])
        with self.assertRaises(FileNotFoundError): self.store.media_file(photo['id'])

    def test_zip_roundtrip_on_new_computer_includes_originals_and_history(self):
        photo, video = self.upload(), self.upload(VIDEO, '合成视频.mp4')
        first = self.entry(attachments=[{'id':photo['id'], 'position':3}, {'id':video['id'], 'position':len('出发🌿\n回家后继续记录。')}])
        self.update(first, [{'id':video['id'], 'position':0}])
        archive = self.store.portable_archive()
        with tempfile.TemporaryDirectory() as folder:
            target = Store(folder)
            preview = target.preview_archive(archive)
            self.assertEqual(preview['preview']['attachments'], 2)
            self.assertEqual(target.entries(DAY), [], 'preview does not replace records')
            target.restore_archive(preview['ticket'])
            entry = target.entries(DAY)[0]
            self.assertEqual(entry['attachments'][0]['mime'], 'video/mp4')
            self.assertEqual(target.media_file(photo['id'])[1].read_bytes(), PNG)
            self.assertEqual(target.media_file(video['id'])[1].read_bytes(), VIDEO)
            self.assertEqual(len(target.media_history(entry['id'])), 2)
            self.assertFalse(target.settings()['cloud_consent'])
        archive.unlink()

    def test_zip_traversal_hash_missing_original_and_uncommitted_drafts_are_rejected(self):
        photo = self.upload()
        self.entry(attachments=[{'id':photo['id'], 'position':0}])
        draft = self.upload(VIDEO)
        bundle = self.store.export()
        self.assertNotIn(draft['id'], {a['id'] for a in bundle['tables']['attachments']})
        member = 'media/' + object_name(photo)
        for extras in ({member: b'changed'}, {'../escape.png':PNG}, {}):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as z:
                z.writestr('diary.json', json.dumps(bundle))
                for name, content in extras.items(): z.writestr(name, content)
            buffer.seek(0)
            with self.assertRaises(ValueError): self.store.preview_archive(buffer)
        self.assertEqual(len(self.store.entries(DAY)), 1)

    def test_sqlite_backup_mirrors_once_and_restores_missing_original(self):
        photo = self.upload()
        self.entry(attachments=[{'id':photo['id'], 'position':0}])
        name = self.store.backup(force=True)
        mirror = Path(self.tmp.name)/'backups'/'media'/object_name(photo)
        stamp = mirror.stat().st_mtime_ns
        self.store.backup(force=True)
        self.assertEqual(mirror.stat().st_mtime_ns, stamp)
        self.store.media_file(photo['id'])[1].unlink()
        self.store.restore_backup(name)
        self.assertEqual(self.store.media_file(photo['id'])[1].read_bytes(), PNG)

    def test_merge_keeps_existing_text_layout_and_remaps_conflicting_attachment_ids(self):
        photo = self.upload()
        entry = self.entry(attachments=[{'id':photo['id'], 'position':0}])
        archive = self.store.portable_archive()
        self.update(entry, [])  # Text revision is unchanged; media changes still count as an import conflict.
        preview = self.store.preview_archive(archive)
        self.assertEqual(preview['preview']['conflicts'], 1)
        self.store.restore_archive(preview['ticket'])
        self.assertEqual(self.store.entries(DAY)[0]['body'], entry['body'])
        self.assertEqual(self.store.entries(DAY)[0]['attachments'], [])
        archive.unlink()

    def test_attachment_id_collision_cannot_replace_a_different_original(self):
        photo = self.upload()
        local = self.entry(attachments=[{'id':photo['id'], 'position':0}])
        video = self.upload(VIDEO, 'video.mp4')
        imported = self.entry(body='导入来源', attachments=[{'id':video['id'], 'position':0}])
        bundle = self.store.export()
        self.store.delete_entry(imported['id'])
        # Preserve the valid second original externally as an import would do.
        path = self.store.media_root()/object_name(video); path.parent.mkdir(exist_ok=True); path.write_bytes(VIDEO)
        for a in bundle['tables']['attachments']:
            if a['id'] == video['id']: a['id'] = photo['id']
        bundle['tables']['entries'] = [e for e in bundle['tables']['entries'] if e['id'] == imported['id']]
        for table in ('entry_versions','media_versions','entry_media'):
            bundle['tables'][table] = [v for v in bundle['tables'][table] if v['entry_id'] == imported['id']]
        bundle['tables']['attachments'] = [a for a in bundle['tables']['attachments'] if a['sha256'] == video['sha256']]
        for ref in bundle['tables']['entry_media']: ref['attachment_id'] = photo['id']
        self.store.import_data(bundle)
        restored = next(e for e in self.store.entries(DAY) if e['id'] == imported['id'])
        self.assertNotEqual(restored['attachments'][0]['id'], photo['id'])
        self.assertEqual(self.store.media_file(photo['id'])[1].read_bytes(), PNG)
        self.assertEqual(self.store.media_file(restored['attachments'][0]['id'])[1].read_bytes(), VIDEO)

    def test_v3_upgrade_snapshot_and_import_remain_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'journal.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA.split('CREATE TABLE IF NOT EXISTS attachments')[0] + 'PRAGMA user_version=3;')
            db.close()
            target = Store(folder)
            snapshots = list((Path(folder)/'upgrade-backups').glob('before-v4-*.sqlite3'))
            self.assertEqual(len(snapshots), 1)
            with contextlib.closing(sqlite3.connect(snapshots[0])) as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            bundle = target.export(); bundle['schema_version'] = 3
            for table in MEDIA_TABLES: del bundle['tables'][table]
            target.import_data(bundle, replace=True)
            with target.connection() as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)


class MediaHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(self.tmp.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        boot = urllib.request.urlopen(self.base + '/api/bootstrap')
        self.token = json.load(boot)['token']; self.cookie = boot.headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly; SameSite=Strict', boot.headers['Set-Cookie'])
        self.assertIn('Path=/api/media/', boot.headers['Set-Cookie'])

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        for temporary, _ in getattr(self.server.application.store, 'media_imports', {}).values(): temporary.cleanup()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None, method=None):
        return urllib.request.urlopen(urllib.request.Request(self.base+path, data=data, headers=headers or {}, method=method))

    def test_cookie_read_range_head_suffix_and_strict_mutation_guard(self):
        self.assertTrue(self.cookie.startswith('ShiguangMedia'+str(self.server.server_port)+'='), 'independent instances must not replace each other\'s media cookie')
        headers = {'X-Notes-Token':self.token, 'Content-Type':'application/octet-stream'}
        uploaded = json.load(self.request('/api/media/upload?name='+urllib.parse.quote('合成视频.mp4'), VIDEO, headers))['attachment']
        path = '/api/media/' + uploaded['id']
        with self.assertRaises(urllib.error.HTTPError) as error: self.request(path)
        self.assertEqual(error.exception.code, 403)
        for value, expected in [('bytes=4-11', VIDEO[4:12]), ('bytes=-10', VIDEO[-10:]), ('bytes=15-', VIDEO[15:])]:
            with self.request(path, headers={'Cookie':self.cookie, 'Range':value}) as response:
                self.assertEqual(response.status, 206); self.assertEqual(response.read(), expected)
                self.assertIn('Content-Range', response.headers)
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        with self.request(path, headers={'Cookie':self.cookie}, method='HEAD') as response:
            self.assertEqual(int(response.headers['Content-Length']), len(VIDEO)); self.assertEqual(response.read(), b'')
        for value in ('bytes=99999-', 'bytes=4-2', 'bytes=-0', 'bytes=0-1,3-4'):
            with self.assertRaises(urllib.error.HTTPError) as error: self.request(path, headers={'Cookie':self.cookie, 'Range':value})
            self.assertEqual(error.exception.code, 416)
        for path2, body in (('/api/media/upload', VIDEO), ('/api/media/discard', b'{}')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path2, body, {'Cookie':self.cookie, 'Content-Type':'application/json'})
            self.assertEqual(error.exception.code, 403, 'read cookie cannot mutate')
        with self.assertRaises(urllib.error.HTTPError) as error: self.request(path, headers={'Cookie':self.cookie, 'Origin':'https://other.example'})
        self.assertEqual(error.exception.code, 403)

    def test_http_portable_export_preview_restore_and_json_guard(self):
        store = self.server.application.store
        attachment = store.upload_media(io.BytesIO(PNG), len(PNG), '照片.png')
        store.save_entry({'body':'合成记录', 'journal_day':DAY, 'attachments':[{'id':attachment['id'], 'position':0}]})
        with self.request('/api/media/archive', headers={'Cookie':self.cookie}) as response:
            self.assertEqual(response.headers['Content-Type'], 'application/zip'); archive = response.read()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(archive)))
        result = json.load(self.request('/api/media/import', archive, {'X-Notes-Token':self.token, 'Content-Type':'application/octet-stream'}))
        restored = json.load(self.request('/api/media/restore', json.dumps({'ticket':result['ticket'], 'mode':'replace'}).encode(), {'X-Notes-Token':self.token, 'Content-Type':'application/json'}))
        self.assertEqual(restored['attachments'], 1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/export?format=json', headers={'X-Notes-Token':self.token})
        self.assertEqual(error.exception.code, 400)
        with self.request('/api/media/'+attachment['id']+'?download=1', headers={'Cookie':self.cookie}) as response:
            self.assertIn("filename*=UTF-8''", response.headers['Content-Disposition'])


if __name__ == '__main__': unittest.main()
