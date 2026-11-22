"""Bounded local video sessions; FFmpeg reads the selected source anonymously."""
from __future__ import annotations

import secrets
import subprocess
import threading
import time

from .providers import _source_headers, validate_headers, validate_url
from .recorder import Recorder
from .media_options import select_stream


class Playback:
    def __init__(self, root):
        self.recorder = Recorder(root)
        self.lock = threading.RLock()
        self.sessions = {}

    def create(self, room, runtime, settings, quality='', protocol=''):
        executable = self.recorder.executable(settings)
        if not executable:
            raise ValueError('未找到 FFmpeg，请先在监控设置中配置。')
        streams = list(runtime.get('stream_variants', []))
        if not streams:
            url = runtime.get('resolved_stream_url') or room.get('stream_url')
            if url:
                streams = [{'url': url, 'quality': 'unknown', 'protocol': 'flv' if '.flv' in url else 'hls'}]
        explicit = bool(quality or protocol)
        selected = select_stream(streams, quality if explicit else settings.get('playback_quality', 'below_origin_2'),
                                 protocol if explicit else settings.get('playback_protocol', 'hls'), strict=explicit)
        url = validate_url(selected['url'])
        headers = _source_headers(url, validate_headers(runtime.get('record_headers') or room.get('request_headers') or {}))
        with self.lock:
            self._prune()
            if len(self.sessions) >= 4:
                raise ValueError('最多同时打开 4 个直播画面，请先关闭一个。')
            key = secrets.token_urlsafe(24)
            self.sessions[key] = dict(url=url, headers=headers, executable=str(executable), process=None, created=time.monotonic(), state='ready', bytes=0, nickname=room.get('nickname') or '直播画面')
            label = selected.get('label') or {'origin':'原画','blue':'蓝光','ultra':'超清','high':'高清','standard':'标清','low':'流畅'}.get(selected.get('quality'), '当前直播源')
            return {'id': key, 'nickname': self.sessions[key]['nickname'], 'quality': label, 'stream_path': '/api/playback/' + key + '/stream.mp4'}

    def _prune(self):
        for key, session in list(self.sessions.items()):
            if session['process'] is None and time.monotonic() - session['created'] > 120:
                self.sessions.pop(key, None)

    def start(self, key):
        with self.lock:
            self._prune()
            session = self.sessions.get(key)
            if session is None:
                raise ValueError('播放会话已结束，请重新打开。')
            if session['state'] != 'ready':
                raise ValueError('播放会话已使用，请重新打开直播画面。')
            args = [session['executable'], '-hide_banner', '-loglevel', 'error', '-nostdin', '-rw_timeout', '15000000', '-protocol_whitelist', 'http,https,tcp,tls,crypto']
            if session['headers']:
                args += ['-headers', ''.join(f'{key}: {value}\r\n' for key, value in session['headers'].items())]
            args += ['-i', session['url'], '-map', '0:v:0', '-map', '0:a:0?',
                     '-vf', 'scale=w=min(1280\\,iw):h=-2', '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
                     '-pix_fmt', 'yuv420p', '-g', '50', '-keyint_min', '25', '-c:a', 'aac', '-b:a', '128k', '-threads', '2',
                     '-movflags', 'frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '1000000', '-f', 'mp4', 'pipe:1']
            try:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except OSError:
                session['state'] = 'failed'
                raise ValueError('无法启动直播播放器，请检查 FFmpeg 配置。') from None
            session.update(process=process, state='connecting')
            return session

    def status(self, key):
        with self.lock:
            session = self.sessions.get(key)
            if not session:
                return {'state': 'closed', 'bytes': 0}
            return {'state': session['state'], 'bytes': session['bytes']}

    def finish(self, key, failed=False):
        with self.lock:
            session = self.sessions.get(key)
            if session:
                session['state'] = 'failed' if failed else 'ended'
                self._stop(session)

    @staticmethod
    def _stop(session):
        process = session.get('process')
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            # The HTTP reader owns stdout and closes it in its finally block.
            session['process'] = None

    def close(self, key=None):
        with self.lock:
            for item in ([key] if key else list(self.sessions)):
                session = self.sessions.pop(item, None)
                if session:
                    self._stop(session)
