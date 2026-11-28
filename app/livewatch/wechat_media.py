"""Normalize Tencent's legacy HEVC FLV tags for the bundled FFmpeg.

Only video tag headers change: HEVC configuration/NAL units and audio remain
untouched. Each recording/player owns a short-lived, loopback-only relay.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import socket
import threading
import urllib.request
from urllib.parse import urlsplit


def needs_flv_relay(room, url):
    return room.get('platform') == 'wechat' and urlsplit(url).path.lower().endswith('.flv')


def _read_exact(source, count, *, eof=False):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = source.read(count - len(chunks))
        if not chunk:
            if eof and not chunks:
                return b''
            raise ValueError('视频号直播流意外结束，等待重新取源')
        chunks.extend(chunk)
    return bytes(chunks)


def normalized_flv(source):
    header = _read_exact(source, 9)
    if header[:3] != b'FLV' or header[3] != 1:
        raise ValueError('视频号直播源不是有效的 FLV')
    offset = int.from_bytes(header[5:9], 'big')
    if not 9 <= offset <= 65536:
        raise ValueError('视频号 FLV 文件头无效')
    yield header + _read_exact(source, offset - 9 + 4)
    while True:
        tag = _read_exact(source, 11, eof=True)
        if not tag:
            return
        size = int.from_bytes(tag[1:4], 'big')
        payload = _read_exact(source, size)
        previous = _read_exact(source, 4)
        if int.from_bytes(previous, 'big') != size + 11:
            raise ValueError('视频号 FLV 数据包不完整')
        if tag[0] == 9 and payload and not payload[0] & 0x80 and payload[0] & 0x0f == 12:
            if len(payload) < 5 or payload[1] not in (0, 1, 2):
                raise ValueError('视频号 HEVC 数据包无效')
            # Legacy: frame/codec, packet type, signed composition time, data.
            # Enhanced: extended/frame/packet type, FourCC, optional CTS, data.
            packet = payload[1]
            payload = bytes([0x80 | (payload[0] & 0x70) | packet]) + b'hvc1' + payload[2 if packet == 1 else 5:]
            tag = tag[:1] + len(payload).to_bytes(3, 'big') + tag[4:]
            previous = (11 + len(payload)).to_bytes(4, 'big')
        yield tag + payload + previous


class WechatFlvRelay:
    def __init__(self, url, headers):
        self.source_url = url
        self.headers = dict(headers)
        self.path = '/' + secrets.token_urlsafe(24) + '/stream.flv'
        self.error = ''
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.active = threading.Lock()
        self.connections = set()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != relay.path or relay.closed.is_set():
                    self.send_error(404)
                    return
                if not relay.active.acquire(blocking=False):
                    self.send_error(503)
                    return
                response = None
                upstream = None
                started = False
                self.connection.settimeout(15)
                with relay.lock:
                    relay.connections.add(self.connection)
                try:
                    if relay.closed.is_set():
                        return
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    response = opener.open(urllib.request.Request(relay.source_url, headers=relay.headers), timeout=15)
                    upstream = response.fp.raw._sock
                    with relay.lock:
                        if relay.closed.is_set():
                            return
                        relay.connections.add(upstream)
                    for chunk in normalized_flv(response):
                        if relay.closed.is_set():
                            break
                        if not started:
                            self.send_response(200)
                            self.send_header('Content-Type', 'video/x-flv')
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            started = True
                        self.wfile.write(chunk)
                    self.wfile.flush()
                except (OSError, ValueError):
                    if not relay.closed.is_set():
                        relay.error = '视频号媒体连接中断或格式无效，等待重新取源'
                    if not started:
                        try:
                            self.send_error(502)
                        except OSError:
                            pass
                finally:
                    self.close_connection = True
                    with relay.lock:
                        relay.connections.discard(self.connection)
                        relay.connections.discard(upstream)
                    if response is not None:
                        response.close()
                    relay.active.release()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + self.path
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1},
                                       name='wechat-flv-relay', daemon=True)
        self.thread.start()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            sockets = list(self.connections)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            # Buffered HTTP responses retain a socket makefile reference, so
            # socket.close() alone defers closing its OS handle. Closing the
            # socket itself also interrupts Windows' pending select/recv.
            try:
                connection._real_close()
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
