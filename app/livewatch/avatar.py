"""Bounded, credential-free avatar cache for the local application UI."""
from __future__ import annotations

from collections import OrderedDict
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
ALLOWED_DOMAINS = ('douyinpic.com', 'byteimg.com', 'pstatp.com', 'toutiaoimg.com', 'douyincdn.com')


class AvatarUnavailable(ValueError):
    """Contains no source URL, response body, or sensitive request data."""


def validate_avatar_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise AvatarUnavailable('头像地址不可用')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AvatarUnavailable('头像地址不可用')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ValueError()
        if not host or host.endswith('.') or not host.isascii():
            raise ValueError()
        if not any(host == domain or host.endswith('.' + domain) for domain in ALLOWED_DOMAINS):
            raise ValueError()
        return urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))
    except ValueError:
        raise AvatarUnavailable('头像地址不可用') from None


def public_addresses(host):
    try:
        values = {result[4][0] for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not values or any(not ipaddress.ip_address(value).is_global for value in values):
            raise ValueError()
        return sorted(values)
    except (OSError, ValueError):
        raise AvatarUnavailable('头像网络不可用') from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to validated DNS results while verifying TLS for the CDN name."""
    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self.public_address = address

    def connect(self):
        raw = socket.create_connection((self.public_address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def image_type(body):
    if len(body) >= 24 and body.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if len(body) >= 4 and body.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if len(body) >= 13 and body[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if len(body) >= 16 and body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def download_avatar(value):
    url = validate_avatar_url(value)
    deadline = time.monotonic() + 4
    for _ in range(4):
        parsed = urlsplit(url)
        addresses = public_addresses(parsed.hostname)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AvatarUnavailable('头像网络超时')
        connection = PinnedHTTPSConnection(parsed.hostname, addresses[0], min(remaining, 4))
        try:
            target = parsed.path + ('?' + parsed.query if parsed.query else '')
            connection.request('GET', target, headers={'User-Agent': 'LiveWatch/2.0', 'Accept': 'image/png,image/jpeg,image/webp,image/gif', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location', '')
                if not location:
                    raise AvatarUnavailable('头像跳转不可用')
                url = validate_avatar_url(urljoin(url, location))
                continue
            if response.status != 200 or response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
                raise AvatarUnavailable('头像响应不可用')
            mime = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            mime = {'image/jpg': 'image/jpeg'}.get(mime, mime)
            if mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                raise AvatarUnavailable('头像格式不可用')
            length = response.getheader('Content-Length')
            if length is not None:
                try:
                    if not 0 < int(length) <= MAX_IMAGE_BYTES:
                        raise ValueError()
                except ValueError:
                    raise AvatarUnavailable('头像尺寸不可用') from None
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AvatarUnavailable('头像网络超时')
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(64 * 1024, MAX_IMAGE_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise AvatarUnavailable('头像尺寸不可用')
                chunks.append(chunk)
            body = b''.join(chunks)
            if image_type(body) != mime:
                raise AvatarUnavailable('头像格式不可用')
            return body, mime
        finally:
            connection.close()
    raise AvatarUnavailable('头像跳转过多')


class AvatarCache:
    def __init__(self, fetcher=None, clock=None, ttl=180):
        self.fetcher = fetcher or download_avatar
        self.clock = clock or time.monotonic
        self.ttl = max(60, min(int(ttl), 300))
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.inflight = {}
        self.slots = threading.BoundedSemaphore(8)
        self.bytes = 0

    def get(self, value):
        try:
            url = validate_avatar_url(value)
        except AvatarUnavailable:
            return None
        with self.lock:
            entry = self.cache.get(url)
            if entry and entry[0] > self.clock():
                self.cache.move_to_end(url)
                return entry[1]
            if entry:
                self.bytes -= len(entry[1][0]) if entry[1] else 0
                self.cache.pop(url)
            waiter = self.inflight.get(url)
            if waiter is None:
                if len(self.inflight) >= 128:
                    return None
                waiter = self.inflight[url] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            waiter.wait(4)
            with self.lock:
                cached = self.cache.get(url)
                return cached[1] if cached and cached[0] > self.clock() else None
        result = None
        try:
            if self.slots.acquire(timeout=0.1):
                try:
                    result = self.fetcher(url)
                    if result and (len(result[0]) > MAX_IMAGE_BYTES or image_type(result[0]) != result[1]):
                        result = None
                finally:
                    self.slots.release()
        except (AvatarUnavailable, OSError, http.client.HTTPException, ValueError):
            result = None
        finally:
            with self.lock:
                self.cache[url] = (self.clock() + (self.ttl if result else 30), result)
                self.bytes += len(result[0]) if result else 0
                while len(self.cache) > 128 or self.bytes > MAX_CACHE_BYTES:
                    _, removed = self.cache.popitem(last=False)
                    self.bytes -= len(removed[1][0]) if removed[1] else 0
                self.inflight.pop(url, None)
                waiter.set()
        return result
