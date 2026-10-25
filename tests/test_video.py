"""Synthetic media only; conversion contracts do not require FFmpeg on CI hosts."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from app.media import object_name
from app.server import make_server
from app.store import Store
from app.video import VideoPreviews, playback_metadata, video_codecs


def box(kind, payload): return struct.pack('>I4s', len(payload)+8,kind)+payload


def movie(codec=b'hvc1'):
    metadata=box(b'stsd',b'\0\0\0\0\0\0\0\1'+box(codec,b'\0'*78))
    for kind in (b'stbl',b'minf',b'mdia',b'trak',b'moov'): metadata=box(kind,metadata)
    return box(b'ftyp',b'isom\0\0\0\0isomiso2')+box(b'mdat',b'synthetic video data')+metadata


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.original=movie()
        self.row=self.store.upload_media(io.BytesIO(self.original),len(self.original),'合成视频.mp4')
        self.path=self.store.media_file(self.row['id'])[1]
        self.previews=VideoPreviews(self.store.directory)

    def tearDown(self): self.tmp.cleanup()

    def test_bounded_metadata_detection_of_hevc_without_reading_payload(self):
        info=self.path.stat()
        self.assertEqual(video_codecs(str(self.path),info.st_size,info.st_mtime_ns),('hvc1',))
        with patch('app.video.helper',return_value='/synthetic/ffmpeg'):
            metadata=playback_metadata(self.row,self.path,self.store.directory)
        self.assertTrue(metadata['requires_preview'])
        self.assertTrue(metadata['playback_url'].endswith('?preview=1'))
        self.assertTrue(metadata['poster_url'].endswith('?poster=1'))
        self.path.write_bytes(b'\x00\x00\x00\x01moov'+struct.pack('>Q',2**62))
        info=self.path.stat()
        self.assertEqual(video_codecs(str(self.path),info.st_size,info.st_mtime_ns),())

    def test_supported_h264_does_not_need_conversion(self):
        self.path.write_bytes(movie(b'avc1'))
        with patch('app.video.helper') as helper:
            self.assertEqual(playback_metadata(self.row,self.path,self.store.directory),{})
        helper.assert_not_called()

    def test_missing_helper_is_explicit_and_missing_original_does_not_hide_text(self):
        with patch('app.video.helper',return_value=None):
            self.assertIn('playback_warning',playback_metadata(self.row,self.path,self.store.directory))
            with self.assertRaisesRegex(ValueError,'组件'): self.previews.ensure(self.row,self.path)
        self.path.unlink()
        self.assertEqual(playback_metadata(self.row,self.path,self.store.directory),{})

    def test_conversion_cache_and_original_download_remain_independent(self):
        def convert(command,**options):
            self.assertIs(options['stdin'],subprocess.DEVNULL)
            Path(command[-1]).write_bytes(movie(b'avc1') if command[-1].endswith('.mp4') else b'\xff\xd8synthetic poster\xff\xd9')
        with patch('app.video.helper',return_value='/synthetic/ffmpeg'),patch('app.video.subprocess.run',side_effect=convert) as run:
            first=self.previews.ensure(self.row,self.path)
            self.assertEqual(self.previews.ensure(self.row,self.path),first)
        self.assertEqual(run.call_count,2)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),self.row['sha256'])
        self.assertNotEqual(first[0].read_bytes(),self.original)
        self.assertIn('libx264',run.call_args_list[0].args[0])
        self.assertIn('yuv420p',run.call_args_list[0].args[0])

    def test_conversion_failure_is_not_published_and_does_not_destroy_original(self):
        with patch('app.video.helper',return_value='/synthetic/ffmpeg'),patch('app.video.subprocess.run',side_effect=subprocess.CalledProcessError(1,['synthetic'])):
            with self.assertRaisesRegex(ValueError,'原件已保留'): self.previews.ensure(self.row,self.path)
        self.assertEqual(list((self.store.directory/'media-previews/v1').iterdir()),[])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_removing_last_original_discards_derived_private_copies(self):
        folder=self.store.directory/'media-previews/v1'; folder.mkdir(parents=True)
        for suffix in ('.jpg','.mp4'): (folder/(self.row['sha256']+suffix)).write_bytes(b'synthetic cache')
        self.store.discard_media(self.row['id'])
        self.assertEqual(list(folder.iterdir()),[])

    def test_cached_preview_has_range_head_authentication_and_original_download(self):
        server=make_server(self.tmp.name); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urllib.request.urlopen(base+'/api/bootstrap') as response: cookie=response.headers['Set-Cookie'].split(';')[0]
            folder=self.store.directory/'media-previews/v1'; folder.mkdir(parents=True)
            compatible=movie(b'avc1')
            (folder/(self.row['sha256']+'.mp4')).write_bytes(compatible)
            (folder/(self.row['sha256']+'.jpg')).write_bytes(b'\xff\xd8synthetic poster\xff\xd9')
            path=base+'/api/media/'+self.row['id']
            with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(path+'?preview=1')
            self.assertEqual(caught.exception.code,403)
            request=urllib.request.Request(path+'?preview=1',headers={'Cookie':cookie,'Range':'bytes=4-11'})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status,206); self.assertEqual(response.read(),compatible[4:12])
            request=urllib.request.Request(path+'?poster=1',headers={'Cookie':cookie},method='HEAD')
            with urllib.request.urlopen(request) as response: self.assertEqual(response.headers['Content-Type'],'image/jpeg')
            with urllib.request.urlopen(urllib.request.Request(path+'?download=1',headers={'Cookie':cookie})) as response:
                self.assertEqual(response.read(),self.original)
        finally: server.shutdown(); server.server_close()


if __name__=='__main__': unittest.main()
