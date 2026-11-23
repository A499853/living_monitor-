"""Offline checks for default playback, saved preferences and custom recording volumes."""
from __future__ import annotations
import functools
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'app'))
from livewatch.media_options import select_stream, recording_directory
from livewatch.maintenance import Maintenance
from livewatch.playback import Playback
from livewatch.recorder import Recorder
from livewatch.storage import Store


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass


class MediaPreferences(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='livewatch-media-')
        self.root = Path(self.temp.name) / 'app'
        self.store = Store(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def variants(self):
        return [dict(quality=q, protocol=p, url=f'http://127.0.0.1/{q}.{p}')
                for q in ('origin', 'ultra', 'high', 'standard') for p in ('flv', 'hls')]

    def test_available_rungs_protocol_and_sparse_fallback(self):
        self.assertEqual(select_stream(self.variants(), 'below_origin_1', 'flv')['url'], 'http://127.0.0.1/ultra.flv')
        self.assertEqual(select_stream(self.variants(), 'below_origin_2', 'hls')['url'], 'http://127.0.0.1/high.hls')
        self.assertEqual(select_stream(self.variants(), 'blue', 'flv')['quality'], 'ultra')
        only = [dict(quality='origin', protocol='hls', url='http://127.0.0.1/one')]
        self.assertEqual(select_stream(only, 'below_origin_2', 'flv'), only[0])
        with self.assertRaises(ValueError): select_stream(only, 'high', 'flv', strict=True)

    def test_playback_applies_defaults_but_keeps_explicit_selection(self):
        player = Playback(self.root)
        self.addCleanup(player.close)
        settings = dict(self.store.get_settings(), ffmpeg_path=str(PROJECT/'tools'/'ffmpeg.exe'))
        info = player.create({'id':'test'}, {'stream_variants':self.variants()}, settings)
        self.assertEqual(player.sessions[info['id']]['url'], 'http://127.0.0.1/high.hls')
        info = player.create({'id':'test'}, {'stream_variants':self.variants()}, settings, 'origin', 'flv')
        self.assertEqual(player.sessions[info['id']]['url'], 'http://127.0.0.1/origin.flv')

    def test_settings_and_room_directory_survive_reopen(self):
        output = str(Path(self.temp.name)/'global recordings')
        self.store.update_settings(dict(recording_directory=output, copy_source_quality='high', playback_quality='standard', monitor_douyin_requests_per_second=1.5))
        room = self.store.add_room(dict(platform='wechat', room_input='test', record_enabled=True, record_quality='high', record_directory='custom'))
        self.assertEqual(recording_directory(self.root,self.store.get_settings(),room), self.root/'custom')
        with_store = Store(self.root)
        try:
            self.assertEqual(with_store.get_settings()['recording_directory'], output)
            self.assertEqual(with_store.get_room(room['id'])['record_directory'], 'custom')
            self.assertTrue(with_store.get_room(room['id'])['record_enabled'])
        finally: with_store.close()
        for value in ('../outside', 'C:relative', 'bad\nname', 'file:stream', 'C:\\output\\bad:stream', 'C:\\NUL', 'C:\\space.\\video', '\\\\.\\C:\\video'):
            with self.assertRaises(ValueError, msg=value): self.store.update_settings({'recording_directory':value})
        for value in (True, 0, 21, float('nan')):
            with self.assertRaises(ValueError): self.store.update_settings({'monitor_douyin_requests_per_second':value})

    def test_destination_change_keeps_only_next_owned_segment(self):
        destination=Path(self.temp.name)/'previous target'
        destination.mkdir()
        maintenance=Maintenance(self.store)
        self.store.update_settings({'recording_directory':str(destination)})
        prior=destination/'test_000000.ts'
        prior.write_bytes(b'old segment')
        maintenance.register_recording(prior,process_id=os.getpid())
        maintenance.finish_recording(prior)
        self.store.update_settings({'recording_directory':''})
        following=destination/'test_000001.ts'
        maintenance.register_recording(following,process_id=os.getpid(),previous_segment=prior)
        self.assertTrue(self.store.db.execute('SELECT 1 FROM owned_files WHERE path=?',(str(following),)).fetchone())
        with self.assertRaises(ValueError):
            maintenance.register_recording(destination/'unrelated.ts',process_id=os.getpid(),previous_segment=prior)
        with self.assertRaises(ValueError):
            maintenance.register_recording(destination/'test_000002.ts',process_id=os.getpid()+100,previous_segment=following)

    def test_configured_timeout_survives_reopen_without_four_second_clamp(self):
        for timeout in (20,60):
            self.store.update_settings({'status_request_timeout':timeout,'request_timeout':timeout})
            reopened=Store(self.root)
            try:
                self.assertEqual(reopened.get_settings()['status_request_timeout'],timeout)
                self.assertEqual(reopened.get_settings()['request_timeout'],timeout)
            finally: reopened.close()
        for invalid in (1,61,True,float('nan')):
            with self.assertRaises(ValueError): self.store.update_settings({'status_request_timeout':invalid})

    def test_custom_output_real_recording_and_conservative_retention(self):
        external = Path(self.temp.name)/'other volume 中文'
        external.mkdir()
        untouched = external/'user-video.ts'
        untouched.write_bytes(b'user file must survive')
        binary = PROJECT/'tools'/'ffmpeg.exe'
        fixture = self.root/'fixture.ts'
        subprocess.run([str(binary),'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=128x72:rate=10','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-threads','1','-f','mpegts',str(fixture)], check=True,timeout=15,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        http = ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(self.root)))
        thread = threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        settings = self.store.update_settings(dict(ffmpeg_path=str(binary),recording_directory=str(external),min_free_disk_gib=0))
        room = self.store.add_room(dict(platform='wechat',room_input='fixture',record_enabled=True))
        maintenance = Maintenance(self.store)
        recorder = Recorder(self.root,maintenance)
        self.addCleanup(recorder.close)
        recorder.start(room,f'http://127.0.0.1:{http.server_port}/fixture.ts',{},settings)
        recorder.jobs[room['id']]['process'].wait(timeout=15)
        result = recorder.stop(room['id'])
        output = Path(result['path'])
        self.assertTrue(output.is_relative_to(external))
        self.assertTrue(result['has_media'])
        self.assertTrue(output.exists())
        self.assertEqual(maintenance.disk_status(settings,path=output)['directory'], str(output.parent))
        self.assertTrue(self.store.db.execute('SELECT 1 FROM owned_files WHERE path=?',(str(output),)).fetchone())
        # Changing the default must still allow a finished, registered output
        # to be cleaned without claiming adjacent files in that directory.
        self.store.update_settings(dict(recording_directory='',recording_retention_days=1))
        cleanup = maintenance.cleanup(now=time.time()+2*86400)
        self.assertGreaterEqual(cleanup['recordings_deleted'],1)
        self.assertFalse(output.exists())
        self.assertEqual(untouched.read_bytes(),b'user file must survive')


if __name__ == '__main__': unittest.main(verbosity=2)
