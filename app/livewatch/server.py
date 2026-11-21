"""Loopback-only HTTP interface. The browser and the worker share no external service."""
from __future__ import annotations

import json
import base64
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

from . import __version__
from .avatar import AvatarCache
from .auth import cookie_is_authorized
from .providers import _douyin_input, ProbeError
from .playback import Playback
from .transfer import batch_payloads, decode_import, export_rooms

MAX_BODY = 128 * 1024
MAX_IMPORT_BODY = 7 * 1024 * 1024 + 4096
MAX_BATCH_BODY = 1024 * 1024


def _source_url(room, runtime):
    """Return only a verified official Douyin room URL, including vanity IDs."""
    if room.get('platform') != 'douyin':
        return ''
    resolved = runtime.get('resolved_room_input')
    value = resolved or room.get('room_input', '')
    try:
        kind, identifier = _douyin_input(value, 'room' if resolved else room.get('input_type', 'auto'))
    except (ProbeError, TypeError):
        return ''
    return 'https://live.douyin.com/' + identifier if kind == 'room' else ''


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, root: Path, store, engine, port=0):
        self.root, self.store, self.engine = Path(root), store, engine
        self.web = Path(__file__).parent / "web"
        self.token = secrets.token_urlsafe(32)
        self.avatars = AvatarCache()
        self.playback = Playback(root)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def server_close(self):
        self.playback.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # Do not put live source URLs, cookies, or API bodies into access logs.

    def _reply(self, code, value, mime="application/json; charset=utf-8"):
        if not isinstance(value, bytes):
            value = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(value)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _valid_request(self, mutation=False):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host:
            self._reply(403, {"error": "仅允许本机访问。"})
            return False
        origin = self.headers.get("Origin")
        top_level_entry = (self.command == "GET" and urlsplit(self.path).path == "/"
                           and self.headers.get("Sec-Fetch-Mode") == "navigate"
                           and self.headers.get("Sec-Fetch-Dest") == "document")
        if (origin and origin != self.server.origin) or (self.headers.get("Sec-Fetch-Site") == "cross-site" and not top_level_entry):
            self._reply(403, {"error": "不允许跨站请求。"})
            return False
        if mutation and not secrets.compare_digest(self.headers.get("X-LiveWatch-Token", ""), self.server.token):
            self._reply(403, {"error": "页面会话已失效，请刷新页面。"})
            return False
        return True

    def _body(self, limit=MAX_BODY):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求。")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("请使用 JSON 请求。")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("请求长度无效。") from None
        if size < 0 or size > limit:
            raise ValueError("请求内容过大。")
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("JSON 格式无效。") from None
        if not isinstance(body, dict):
            raise ValueError("请求内容应为对象。")
        return body

    def do_GET(self):
        if not self._valid_request():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/health":
                return self._reply(200, {"ok": True, "app": "LiveWatch", "version": __version__})
            if path.startswith('/api/playback/'):
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[3] == 'status':
                    return self._reply(200, self.server.playback.status(parts[2]))
                if len(parts) == 4 and parts[3] == 'stream.mp4':
                    return self._video(parts[2])
                return self._reply(404, {'error': '播放资源不存在。'})
            if path.startswith("/api/avatar/"):
                room_id = unquote(path.rsplit("/", 1)[1])
                # Resolve the source exclusively from provider metadata for this
                # saved room. Query strings never select an upstream URL.
                room = next((item for item in self.server.engine.snapshot().get("rooms", []) if item["id"] == room_id), None)
                if room is None:
                    return self._reply(404, {"error": "头像不可用。"})
                avatar = self.server.avatars.get(room.get("avatar_url", ""))
                if avatar is None:
                    return self._reply(404, {"error": "头像不可用。"})
                return self._reply(200, avatar[0], avatar[1])
            if path == "/api/state":
                state = self.server.engine.snapshot()
                state["auth"]={"douyin_authorized":cookie_is_authorized(self.server.store.get_settings().get("douyin_cookie",""))}
                settings = state.get("settings", {})
                cookie = settings.pop("douyin_cookie", "")
                settings["douyin_cookie_configured"] = bool(cookie or self.server.store.get_settings().get("douyin_cookie"))
                for room in state.get("rooms", []):
                    room["has_request_headers"] = bool(room.pop("request_headers", {}) or room.get("has_request_headers"))
                    room["has_stream_url"] = bool(room.pop("stream_url", ""))
                    room["has_status_url"] = bool(room.pop("status_url", ""))
                    room.pop("resolved_stream_url", None)
                    room.pop("stream_variants", None)
                    room.pop("transfer_options", None)
                return self._reply(200, state)
            if path.startswith("/api/room-source/"):
                room_id = unquote(path.rsplit("/", 1)[1])
                room = dict(self.server.store.get_room(room_id))
                snapshot = self.server.engine.snapshot()
                runtime = next((r for r in snapshot["rooms"] if r["id"] == room_id), {})
                room["resolved_stream_url"] = runtime.get("resolved_stream_url", "")
                streams=runtime.get("stream_variants",[])
                room_url=_source_url(room,runtime)
                return self._reply(200, {"room": room,"streams":streams,"room_url":room_url})
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in static:
                filename, mime = static[path]
                content = (self.server.web / filename).read_bytes()
                if filename == "index.html":
                    content = content.replace(b"__API_TOKEN__", self.server.token.encode("ascii"))
                return self._reply(200, content, mime)
            return self._reply(404, {"error": "未找到此资源。"})
        except ValueError as exc:
            return self._reply(400, {"error": str(exc)})
        except Exception as exc:
            logging.getLogger("livewatch").error("HTTP read failed: %s", type(exc).__name__)
            return self._reply(500, {"error": "读取失败，请查看日志后重试。"})

    def do_POST(self):
        self._mutate("POST")

    def _video(self, key):
        session = self.server.playback.start(key)
        process = session['process']
        failed = False
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            while True:
                chunk = process.stdout.read1(64 * 1024)
                if not chunk:
                    failed = process.wait(timeout=3) != 0
                    break
                session['state'] = 'playing'
                session['bytes'] += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError):
            pass  # Closing the video releases its source and decoder.
        finally:
            self.server.playback.finish(key, failed=failed)
            process.stdout.close()

    def do_PUT(self):
        self._mutate("PUT")

    def do_DELETE(self):
        self._mutate("DELETE")

    def _mutate(self, method):
        if not self._valid_request(mutation=True):
            return
        path = urlsplit(self.path).path
        try:
            limit = MAX_IMPORT_BODY if path == "/api/import" else MAX_BATCH_BODY if path == "/api/rooms/batch" else MAX_BODY
            body = self._body(limit)
            if method == 'POST' and path == '/api/playback':
                room = self.server.store.get_room(body.get('room_id', ''))
                with self.server.engine.lock:
                    runtime = dict(self.server.engine.states.get(room['id'], {}))
                session = self.server.playback.create(room, runtime, self.server.store.get_settings(), body.get('quality', ''), body.get('protocol', ''))
                return self._reply(201, session)
            if method == 'POST' and path.startswith('/api/playback/') and path.endswith('/close'):
                self.server.playback.close(path.split('/')[-2])
                return self._reply(200, {'ok': True})
            if method == "POST" and path == "/api/auth/douyin":
                cookie=body.get('cookie','')
                if not cookie_is_authorized(cookie): raise ValueError('请先在抖音官方窗口内完成登录，再授权本机监控。')
                self.server.store.update_settings({'douyin_cookie':cookie,'douyin_user_agent':body.get('user_agent','')})
                changed=getattr(self.server.engine,'auth_changed',None)
                if changed: changed()
                return self._reply(200, {'ok':True,'douyin_authorized':True})
            if method == "POST" and path == "/api/rooms/batch":
                payloads = batch_payloads(body.get("text"), body)
                result = self.server.store.add_rooms(payloads)
                self.server.engine.room_changed()
                return self._reply(200, {"ok": True, **result, "warnings": []})
            if method == "POST" and path == "/api/import":
                payloads, warnings = decode_import(body.get("format"), body.get("content_base64"))
                result = self.server.store.add_rooms(payloads)
                self.server.engine.room_changed()
                return self._reply(200, {"ok": True, **result, "warnings": warnings})
            if method == "POST" and path == "/api/export":
                rooms = self.server.store.list_rooms()
                ids = body.get("ids")
                if ids is not None:
                    if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
                        raise ValueError("导出直播间选择无效。")
                    wanted = set(ids)
                    if wanted - {room["id"] for room in rooms}:
                        raise ValueError("导出中包含不存在的直播间。")
                    rooms = [room for room in rooms if room["id"] in wanted]
                if len(rooms) > 1000:
                    raise ValueError("一次最多导出 1000 个直播间。")
                snapshots = {room["id"]: room for room in self.server.engine.snapshot().get("rooms", [])}
                rooms = [{**room, "avatar_url": snapshots.get(room["id"], {}).get("avatar_url", "")} for room in rooms]
                content, mime, filename = export_rooms(body.get("format"), rooms)
                return self._reply(200, {"ok": True, "filename": filename, "mime": mime, "content_base64": base64.b64encode(content).decode("ascii"), "count": len(rooms)})
            if method == "PUT" and path == "/api/rooms/order":
                self.server.store.reorder_rooms(body.get("ids"))
                return self._reply(200, {"ok": True})
            if method == "POST" and path == "/api/rooms":
                room = self.server.store.add_room(body)
                self.server.engine.room_changed(room["id"])
                return self._reply(201, {"ok": True, "room": {"id": room["id"]}})
            if path.startswith("/api/rooms/"):
                room_id = unquote(path.rsplit("/", 1)[1])
                if method == "PUT":
                    self.server.store.update_room(room_id, body)
                elif method == "DELETE":
                    self.server.store.delete_room(room_id)
                else:
                    return self._reply(405, {"error": "不支持此操作。"})
                self.server.engine.room_changed(room_id)
                return self._reply(200, {"ok": True})
            if method == "PUT" and path == "/api/settings":
                previous=self.server.store.get_settings()
                self.server.store.update_settings(body)
                if any(key in body and body[key]!=previous.get(key) for key in ('douyin_cookie','douyin_user_agent','douyin_access_mode')):
                    changed=getattr(self.server.engine,'auth_changed',None)
                    if changed: changed()
                changed=getattr(self.server.engine,'settings_changed',None)
                if changed: changed()
                return self._reply(200, {"ok": True})
            if method == "POST" and path in ("/api/refresh", "/api/bulk"):
                ids = body.get("ids")
                if ids is not None and (not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(i, str) for i in ids)):
                    raise ValueError("直播间选择无效。")
                if path == "/api/refresh":
                    self.server.engine.refresh(ids)
                else:
                    self.server.engine.bulk(body.get("action", ""), ids)
                return self._reply(200, {"ok": True})
            if method == "POST" and path == "/api/open-folder":
                kind = body.get("kind")
                if kind not in ("video", "logs", "time_begin_over"):
                    raise ValueError("目录无效。")
                folder = self.server.root / kind
                folder.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(folder))
                return self._reply(200, {"ok": True})
            if method == "POST" and path == "/api/test-sound":
                if os.name == "nt":
                    import winsound
                    winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC)
                return self._reply(200, {"ok": True, "native_sound_available": os.name == "nt"})
            if method == "POST" and path == "/api/shutdown":
                self._reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._reply(404, {"error": "未找到此操作。"})
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception as exc:
            logging.getLogger("livewatch").error("HTTP mutation failed: %s", type(exc).__name__)
            self._reply(500, {"error": "操作失败，请查看日志后重试。"})
