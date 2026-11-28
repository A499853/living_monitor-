"""Receive explicitly selected Channels identities; never treat capture as status.

The optional upstream proxy injects our user script. Only identity fields cross
this boundary. Every actual monitoring observation is a fresh joinLive request.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time

from .wechat import validate_bridge
from .wechat_runtime import WechatRuntime, RuntimeErrorSafe


class CaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class CaptureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def valid_origin(self):
        return self.headers.get('Origin', '') in ('', 'https://channels.weixin.qq.com')

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.headers.get('Origin') == 'https://channels.weixin.qq.com':
            self.send_header('Access-Control-Allow-Origin', 'https://channels.weixin.qq.com')
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def authorized(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        return (self.valid_origin() and self.headers.get('Host') == expected
                and secrets.compare_digest(self.path, '/capture/' + self.server.capture_token))

    def do_OPTIONS(self):
        self.reply(200 if self.authorized() else 403, {})

    def do_POST(self):
        if not self.authorized():
            return self.reply(403, {'code': 403, 'msg': '捕获会话无效，请重新生成脚本。'})
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('不支持分块请求。')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('请使用 JSON 请求。')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                raise ValueError('捕获信息过大或为空。')
            payload = json.loads(self.rfile.read(size))
            candidate = self.server.capture_manager.receive(payload, self.server.capture_token)
            self.reply(200, {'code': 0, 'data': {'received': True, 'id': candidate['id']}})
        except (ValueError, UnicodeError, TimeoutError):
            self.reply(400, {'code': 400, 'msg': '直播标识无效或捕获已停止，请重新打开直播。'})


class WechatCapture:
    def __init__(self, root, store, engine):
        self.root, self.store, self.engine = Path(root), store, engine
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.closed = False
        self.server = None
        self.thread = None
        self.candidates = {}
        self.bridge_api = 'http://127.0.0.1:2022'
        self.script_path = self.root / 'data' / 'wechat-capture' / 'global.js'
        self.runtime = WechatRuntime(self.root)
        self.runtime_error = ''
        try:
            self.runtime.recover()
        except RuntimeErrorSafe as exc:
            self.runtime_error = str(exc)

    @staticmethod
    def api_base(value):
        # One canonical validator for both the UI and provider configuration.
        return validate_bridge(dict(api_base=value, object_id='1', nonce_id='test',
                                    live_id='1', username=''))['api_base']

    def snapshot(self):
        with self.lock:
            self._expire()
            return dict(running=self.server is not None,
                        port=self.server.server_port if self.server else None,
                        script_path=str(self.script_path) if self.script_path.exists() else '',
                        bridge_api=self.bridge_api,
                        runtime=self.runtime.snapshot(),
                        runtime_error=self.runtime_error,
                        candidates=[{k: item[k] for k in ('id', 'nickname', 'username', 'live_id', 'captured_at')}
                                    for item in reversed(list(self.candidates.values()))],
                        message='捕获仅添加候选；加入监控后每次向微信重新查询本场状态。下次开播需重新捕获。')

    def start(self, bridge_api, managed=False, consent=False):
        with self.lifecycle_lock:
            if self.closed:
                raise ValueError('软件正在退出，不能重新启动视频号接收。')
            if managed and not consent:
                raise ValueError('启动内置捕获需要先确认临时证书与系统代理设置。')
            if managed and self.api_base(bridge_api) != self.runtime.api_base:
                raise ValueError('内置捕获使用固定的本机地址，请恢复默认地址。')
            self._start(bridge_api)
            if managed:
                try:
                    self.runtime.start(self.script_path)
                    self.runtime_error = ''
                except RuntimeErrorSafe as exc:
                    self.runtime_error = str(exc)
                    self._stop()
                    raise ValueError(str(exc)) from None
            return self.snapshot()

    def _start(self, bridge_api):
        api = self.api_base(bridge_api)
        with self.lock:
            if self.server:
                if api != self.bridge_api:
                    raise ValueError('请先停止接收，再更改桥接地址。')
                return self.snapshot()
            self.bridge_api = api
            self.candidates.clear()
            try:
                server = CaptureServer(('127.0.0.1', 13949), CaptureHandler)
            except OSError:
                server = CaptureServer(('127.0.0.1', 0), CaptureHandler)
            server.capture_manager = self
            server.capture_token = secrets.token_urlsafe(32)
            endpoint = f'http://127.0.0.1:{server.server_port}/capture/{server.capture_token}'
            try:
                script = Path(__file__).with_name('wechat_capture.js').read_text('utf-8')
                script = script.replace('__LIVEWATCH_CAPTURE_ENDPOINT__', json.dumps(endpoint))
                self.script_path.parent.mkdir(parents=True, exist_ok=True)
                self.script_path.write_text(script, encoding='utf-8')
                self.server = server
                self.thread = threading.Thread(target=server.serve_forever,
                                               kwargs={'poll_interval': 0.1}, daemon=True,
                                               name='wechat-capture')
                self.thread.start()
            except BaseException:
                self.server = None
                server.server_close()
                raise
            return self.snapshot()

    def stop(self):
        with self.lifecycle_lock:
            return self._stop()

    def close(self):
        with self.lifecycle_lock:
            self.closed = True
            self._stop()
            try:
                self.runtime.stop()
                self.runtime_error = ''
            except RuntimeErrorSafe as exc:
                self.runtime_error = str(exc)
                import logging
                logging.getLogger('livewatch').error('WeChat cleanup pending: %s', exc)
            return self.snapshot()

    def stop_bridge(self):
        with self.lifecycle_lock:
            self._stop()
            try:
                self.runtime.stop()
                self.runtime_error = ''
            except RuntimeErrorSafe as exc:
                self.runtime_error = str(exc)
                raise ValueError(str(exc)) from None
            return self.snapshot()

    def _stop(self):
        with self.lock:
            server, self.server = self.server, None
            thread, self.thread = self.thread, None
            self.candidates.clear()
        if server:
            server.shutdown()
            server.server_close()
            if thread:
                thread.join(timeout=2)
        return self.snapshot()

    def _expire(self):
        now = time.monotonic()
        self.candidates = {key: item for key, item in self.candidates.items()
                           if now - item['_received'] < 900}

    def receive(self, payload, token):
        if not isinstance(payload, dict) or set(payload) - {'object_id', 'nonce_id', 'live_id', 'username', 'nickname'}:
            raise ValueError('仅接收直播标识。')
        nickname = payload.get('nickname', '')
        if not isinstance(nickname, str) or len(nickname) > 160 or any(ord(c) < 32 for c in nickname):
            raise ValueError('昵称无效。')
        with self.lock:
            if not self.server or not secrets.compare_digest(token, self.server.capture_token):
                raise ValueError('捕获已停止。')
            config = validate_bridge({key: payload.get(key, '') for key in
                                      ('object_id', 'nonce_id', 'live_id', 'username')}
                                     | {'api_base': self.bridge_api})
            self._expire()
            # A newly captured session replaces the previous candidate for the
            # same account. Remove its opaque ID too, so a stale browser row
            # cannot rebind monitoring back to an older broadcast.
            if config['username']:
                self.candidates = {key: item for key, item in self.candidates.items()
                                   if item['username'] != config['username']
                                   or item['config'] == config}
            existing = next((item for item in self.candidates.values()
                             if item['config'] == config), None)
            key = existing['id'] if existing else secrets.token_hex(12)
            if len(self.candidates) >= 100 and not existing:
                self.candidates.pop(next(iter(self.candidates)))
            item = dict(id=key, nickname=nickname.strip(), username=config['username'],
                        live_id=config['live_id'], config=config,
                        captured_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        _received=time.monotonic())
            self.candidates[key] = item
            return item

    def add(self, candidate_id):
        with self.lock:
            self._expire()
            item = self.candidates.get(candidate_id) if isinstance(candidate_id, str) else None
            if not item:
                raise ValueError('候选已过期或已停止捕获，请在微信直播中重新捕获。')
            config = dict(item['config'])
            identity = config['username'] or ('session:' + config['live_id'])
            room_input = 'wx:' + identity
            # Serialize application with monitoring. Captured live flags and media
            # are deliberately absent: only fresh probes can open/close sessions.
            with self.engine.lock:
                existing = next((room for room in self.store.list_rooms()
                                 if room['platform'] == 'wechat' and room['room_input'] == room_input), None)
                if existing:
                    options = dict(existing.get('transfer_options') or {})
                    options['wechat_bridge'] = config
                    room = self.store.update_room(existing['id'], dict(
                        transfer_options=options, nickname=item['nickname'] or existing['nickname'],
                        stream_url='', status_url=''))
                else:
                    room = self.store.add_room(dict(platform='wechat', room_input=room_input,
                        nickname=item['nickname'], monitor_enabled=True, record_enabled=False,
                        transfer_options={'wechat_bridge': config}))
                self.engine.room_changed(room['id'])
                self.engine.refresh([room['id']])
            return dict(ok=True, room={'id': room['id']}, updated=existing is not None)

    def check(self, bridge_api):
        # The provider's deadline-aware transport avoids proxy loops and remote
        # redirects. No personal account or stream information is returned here.
        from .wechat import bridge_status
        return bridge_status(self.api_base(bridge_api))
