"""Offline real HEVC/AAC decoding through WeChat FLV recording and playback."""
from __future__ import annotations

import functools
import io
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'app'))
from livewatch.recorder import Recorder
from livewatch.playback import Playback
from livewatch.wechat_media import WechatFlvRelay, normalized_flv

FFMPEG = PROJECT / 'tools/ffmpeg.exe'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def command(arguments):
    result = subprocess.run([str(FFMPEG), '-hide_banner', '-loglevel', 'error', *map(str, arguments)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, creationflags=FLAGS)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    return result


def legacy_flv(data):
    """Create the legacy codec-12 wire format from FFmpeg's HEVC fixture."""
    offset = int.from_bytes(data[5:9], 'big') + 4
    output = bytearray(data[:offset])
    while offset < len(data):
        tag = data[offset:offset + 11]
        size = int.from_bytes(tag[1:4], 'big')
        payload = data[offset + 11:offset + 11 + size]
        if tag[0] == 9 and payload[0] & 0x80 and payload[1:5] == b'hvc1':
            kind = payload[0] & 15
            if kind in (0, 2):
                payload = bytes([(payload[0] & 0x70) | 12, kind]) + b'\0\0\0' + payload[5:]
            elif kind == 1:
                payload = bytes([(payload[0] & 0x70) | 12, 1]) + payload[5:]
            elif kind == 3:
                payload = bytes([(payload[0] & 0x70) | 12, 1]) + b'\0\0\0' + payload[5:]
            elif kind == 4:
                pass  # Enhanced color metadata has no legacy video equivalent.
            else:
                raise AssertionError('unexpected HEVC packet type: '+str(kind)+' '+payload[:32].hex())
            tag = tag[:1] + len(payload).to_bytes(3, 'big') + tag[4:]
        output.extend(tag + payload + (11 + len(payload)).to_bytes(4, 'big'))
        offset += 11 + size + 4
    return bytes(output)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class MediaChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix='wechat-hevc-')
        cls.fixture_root = Path(cls.fixture_temp.name)
        cls.modern = cls.fixture_root / 'modern.flv'
        command(['-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=10', '-f', 'lavfi', '-i',
                 'sine=frequency=440:sample_rate=48000', '-t', '4', '-c:v', 'libx265', '-pix_fmt', 'yuv420p',
                 '-x265-params', 'pools=1:frame-threads=1:log-level=error:keyint=10',
                 '-threads', '1', '-c:a', 'aac', '-f', 'flv', cls.modern])
        cls.legacy = cls.fixture_root / 'legacy.flv'
        cls.legacy.write_bytes(legacy_flv(cls.modern.read_bytes()))
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(cls.fixture_root)))
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/legacy.flv'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.fixture_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wechat-recording-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.room = dict(id='wechat-media', nickname='HEVC 测试', platform='wechat')
        self.settings = dict(ffmpeg_path=str(FFMPEG), max_recordings=2)

    def decode(self, path, min_frames=30):
        result = command(['-nostats', '-progress', 'pipe:1', '-i', path, '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
        self.assertEqual(result.stderr, b'')
        frames = [int(value) for value in re.findall(rb'frame=(\d+)', result.stdout)]
        self.assertGreaterEqual(max(frames), min_frames)

    def finish(self, recorder):
        job = recorder.jobs[self.room['id']]
        job['process'].wait(timeout=20)
        for reader in job['threads']:
            reader.join(2)
        status = recorder.stop(self.room['id'])
        self.assertEqual(status['returncode'], 0, status['error'])
        self.assertTrue(status['has_media'])
        self.assertFalse(job['media_relay'].thread.is_alive())
        return status

    def test_standard_flv_unchanged_and_truncated_tag_rejected(self):
        original = self.modern.read_bytes()
        self.assertEqual(b''.join(normalized_flv(io.BytesIO(original))), original)
        with self.assertRaises(ValueError):
            b''.join(normalized_flv(io.BytesIO(self.legacy.read_bytes()[:-3])))

    def test_stalled_upstream_is_released_when_relay_closes(self):
        waiting = threading.Event()
        release = threading.Event()
        prefix = self.modern.read_bytes()[:13]
        class Stalled(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(prefix + b'\x09')
                self.wfile.flush()
                waiting.set()
                release.wait(5)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Stalled)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        relay = WechatFlvRelay(f'http://127.0.0.1:{server.server_port}/live.flv', {})
        response = None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            response = opener.open(relay.url, timeout=3)
            self.assertEqual(response.read(13), prefix)
            self.assertTrue(waiting.wait(2))
            started = time.monotonic()
            relay.close()
            self.assertLess(time.monotonic() - started, 2)
            acquired = relay.active.acquire(timeout=2)
            self.assertTrue(acquired, 'upstream reader leaked after close')
            relay.active.release()
            self.assertFalse(relay.thread.is_alive())
        finally:
            if response: response.close()
            relay.close()
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_legacy_hevc_audio_decode_after_header_normalization(self):
        fixed = self.root / 'fixed.flv'
        fixed.write_bytes(b''.join(normalized_flv(io.BytesIO(self.legacy.read_bytes()))))
        self.decode(fixed)

    def test_continuous_ts_and_same_file_reconnect_have_video_and_audio(self):
        recorder = Recorder(self.root)
        self.addCleanup(recorder.close)
        recorder.start(self.room, self.url, {}, self.settings)
        first = self.finish(recorder)
        self.decode(first['path'])
        recorder.start(self.room, self.url, {}, dict(self.settings, _record_resume=first['resume']))
        second = self.finish(recorder)
        self.assertEqual(first['path'], second['path'])
        self.decode(second['path'], min_frames=70)

    def test_segmented_ts_keeps_decodable_hevc_and_audio(self):
        recorder = Recorder(self.root)
        self.addCleanup(recorder.close)
        recorder.start(self.room, self.url, {}, dict(self.settings, record_segment_enabled=True, record_segment_seconds=2))
        self.finish(recorder)
        files = list((self.root / 'video').rglob('*.ts'))
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            self.decode(path, min_frames=1)

    def test_player_transcodes_legacy_hevc_to_browser_mp4(self):
        playback = Playback(self.root)
        self.addCleanup(playback.close)
        created = playback.create(self.room, {'resolved_stream_url': self.url}, self.settings)
        session = playback.start(created['id'])
        data, _ = session['process'].communicate(timeout=20)
        self.assertEqual(session['process'].returncode, 0)
        path = self.root / 'player.mp4'
        path.write_bytes(data)
        playback.finish(created['id'])
        self.assertIsNone(session['relay'])
        self.decode(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
