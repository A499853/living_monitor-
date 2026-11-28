"""Offline checks for recording identity, crash checkpoints and same-file recovery."""
from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'app'))
from livewatch.engine import Engine
from livewatch.maintenance import Maintenance
from livewatch.recorder import Recorder
from livewatch.storage import Store

FFMPEG = PROJECT / 'tools' / 'ffmpeg.exe'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def command(arguments):
    return subprocess.run([str(FFMPEG), '-hide_banner', '-loglevel', 'error', *map(str, arguments)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=25, creationflags=FLAGS, check=True)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass


class ResumeChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix='record-resume-media-')
        cls.fixture_root = Path(cls.fixture_temp.name)
        cls.fixture = cls.fixture_root / 'fixture.ts'
        command(['-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=10',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                 '-t', '3', '-c:v', 'libx264', '-bf', '3', '-g', '10',
                 '-x264-params', 'b-adapt=0', '-pix_fmt', 'yuv420p', '-threads', '1',
                 '-c:a', 'aac', '-f', 'mpegts', cls.fixture])
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(cls.fixture_root)))
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/fixture.ts'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(3)
        cls.fixture_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recording-resume-')
        self.root = Path(self.temp.name) / '续录 % 数据'
        self.store = Store(self.root)
        self.room = self.store.add_room(dict(platform='wechat', room_input='local-fixture', nickname='续录测试',
                                             monitor_enabled=True, record_enabled=True, stream_url=self.url))
        self.store.update_settings(dict(ffmpeg_path=str(FFMPEG), min_free_disk_gib=0))
        self.recorder = Recorder(self.root, Maintenance(self.store))

    def tearDown(self):
        self.recorder.close()
        self.store.close()
        self.temp.cleanup()

    def identity_fixture(self, closed=False):
        path = self.root / 'video' / 'identity.ts'
        path.write_bytes(self.fixture.read_bytes())
        job = dict(pattern='', path=str(path), task_id='identity-task', duration_seconds=3,
                   last_media_write_at='2026-11-27T00:00:00+08:00', timestamp_reference=360000,
                   output_closed=closed)
        return path, self.recorder._resume_info(job)

    def await_media_exit(self, recorder=None):
        recorder = recorder or self.recorder
        job = recorder.jobs[self.room['id']]
        job['process'].wait(timeout=15)
        for reader in job['threads']:
            reader.join(3)
            self.assertFalse(reader.is_alive())
        self.assertFalse(job.get('write_error'), job.get('error'))
        return recorder.status(self.room['id'])

    def assert_complete_six_seconds(self, path):
        output = command(['-nostats', '-progress', 'pipe:1', '-i', path, '-f', 'null', '-'])
        self.assertEqual(output.stderr, b'')
        duration = int(re.findall(rb'out_time_us=(\d+)', output.stdout)[-1]) / 1e6
        self.assertGreater(duration, 5.9)
        self.assertLess(duration, 6.7)

    def test_checkpoint_allows_same_file_growth_with_unchanged_hash_window(self):
        path, checkpoint = self.identity_fixture()
        self.assertTrue(checkpoint['checkpoint'])
        prefix = path.read_bytes()
        suffix = prefix[-188 * 8:]
        with path.open('ab') as handle:
            handle.write(suffix)
        with self.recorder._open_output(path, checkpoint) as handle:
            self.assertEqual(handle.tell(), len(prefix) + len(suffix))
        self.assertEqual(path.read_bytes(), prefix + suffix)
        old_window = path.read_bytes()[max(0, checkpoint['size'] - 8192):checkpoint['size']]
        self.assertEqual(hashlib.sha256(old_window).hexdigest(), checkpoint['checkpoint_hash'])
        with path.open('r+b') as handle:
            handle.seek(checkpoint['size'] - 1)
            handle.write(bytes([prefix[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, '检查点内容已更改'):
            self.recorder._open_output(path, checkpoint)

    def test_closed_resume_requires_exact_file_and_rejects_replace_or_edit(self):
        path, resume = self.identity_fixture(closed=True)
        self.assertFalse(resume['checkpoint'])
        with self.recorder._open_output(path, resume):
            pass
        original = path.read_bytes()
        with path.open('ab') as handle:
            handle.write(original[-188:])
        with self.assertRaisesRegex(ValueError, '替换或修改'):
            self.recorder._open_output(path, resume)
        path.write_bytes(original)
        resume = self.recorder._resume_info(dict(pattern='', path=str(path), task_id='identity-task',
                                                 duration_seconds=3, last_media_write_at='', output_closed=True))
        with path.open('r+b') as handle:
            handle.seek(len(original) - 1)
            handle.write(bytes([original[-1] ^ 1]))
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, int(resume['modified_ns']) + 1000000000))
        with self.assertRaisesRegex(ValueError, '替换或修改'):
            self.recorder._open_output(path, resume)
        path.write_bytes(original)
        resume = self.recorder._resume_info(dict(pattern='', path=str(path), task_id='identity-task',
                                                 duration_seconds=3, last_media_write_at='', output_closed=True))
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(original)
        os.utime(replacement, ns=(path.stat().st_atime_ns, int(resume['modified_ns'])))
        os.replace(replacement, path)
        with self.assertRaisesRegex(ValueError, '替换或修改'):
            self.recorder._open_output(path, resume)
        self.assertEqual(path.read_bytes(), original)

    def test_first_media_checkpoint_is_durable_before_five_seconds(self):
        started = time.monotonic()
        self.recorder.start(self.room, self.url, {}, self.store.get_settings())
        current = self.await_media_exit()
        self.assertLess(time.monotonic() - started, 5)
        durable = self.store.get_record_task(self.room['id'])
        self.assertIsNotNone(durable['resume'])
        self.assertTrue(durable['resume']['checkpoint'])
        self.assertEqual(durable['resume']['task_id'], current['task_id'])
        self.assertGreaterEqual(durable['resume']['size'], 188)
        self.assertLessEqual(durable['resume']['size'], current['resume']['size'])
        # This is the state available after an abrupt backend exit, before the
        # normal stop worker updates size/mtime to their final values.
        with self.recorder._open_output(Path(current['path']), durable['resume']):
            pass

    def test_real_recovery_appends_same_task_and_preserves_active_ownership(self):
        self.recorder.start(self.room, self.url, {}, self.store.get_settings())
        self.await_media_exit()
        first = self.recorder.request_recovery(self.room['id']).result(timeout=10)
        self.assertTrue(first['resume_pending'])
        self.assertFalse(first['resume']['checkpoint'])
        original = Path(first['path']).read_bytes()
        active = self.store.db.execute("SELECT active FROM owned_files WHERE kind='recording'").fetchone()[0]
        self.assertEqual(active, 1)
        settings = dict(self.store.get_settings(), _record_resume=first['resume'])
        self.recorder.start(self.room, self.url, {}, settings)
        self.await_media_exit()
        second = self.recorder.request_recovery(self.room['id']).result(timeout=10)
        self.assertEqual(first['path'], second['path'])
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(second['bytes_written'], Path(second['path']).stat().st_size)
        self.assertGreater(second['bytes_written'], first['bytes_written'])
        self.assertEqual(Path(second['path']).read_bytes()[:len(original)], original)
        self.assertGreater(second['duration_seconds'], first['duration_seconds'] + 2.9)
        self.assertEqual(len(list((self.root / 'video').rglob('*.ts'))), 1)
        self.assert_complete_six_seconds(second['path'])
        self.recorder.finish_output(self.room['id'], second['resume'])
        active = self.store.db.execute("SELECT active FROM owned_files WHERE kind='recording'").fetchone()[0]
        self.assertEqual(active, 0)

    def test_engine_close_and_rebuild_resume_original_file(self):
        engine = Engine(self.store, providers=object(), recorder=self.recorder)
        def start_ready(current):
            state = current._state(self.room)
            state.update(confirmed_live=True, live_status='live', resolved_stream_url=self.url)
            current._start_record(self.room, state)
            if current.ffmpeg_prepare_thread:
                current.ffmpeg_prepare_thread.join(5)
                self.assertFalse(current.ffmpeg_prepare_thread.is_alive())
            if self.room['id'] not in current.recorder.jobs:
                current._start_record(self.room, state)
            self.assertIn(self.room['id'], current.recorder.jobs)
            return state
        try:
            state = start_ready(engine)
            first = self.await_media_exit(engine.recorder)
            engine.close()
            self.assertTrue(self.store.get_runtime(self.room['id'])['record_options'])
            self.assertEqual(self.store.get_record_task(self.room['id'])['resume']['path'], first['path'])
            self.recorder = Recorder(self.root, Maintenance(self.store))
            rebuilt = Engine(self.store, providers=object(), recorder=self.recorder)
            try:
                self.assertEqual(rebuilt.states[self.room['id']]['record_resume']['path'], first['path'])
                start_ready(rebuilt)
                second = self.await_media_exit(rebuilt.recorder)
                self.assertEqual(second['path'], first['path'])
                self.assertEqual(second['task_id'], first['task_id'])
                rebuilt.close()
            finally:
                rebuilt.close()
            self.assert_complete_six_seconds(first['path'])
            self.assertEqual(len(list((self.root / 'video').rglob('*.ts'))), 1)
        finally:
            engine.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
