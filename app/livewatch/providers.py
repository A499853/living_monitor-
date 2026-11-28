"""Public room metadata and authorized user-supplied source probes.
Unknown/errors never count as a confirmed offline transition.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import errno
import html
import hashlib
import http.cookiejar
import http.client
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from .auth import authorized_monitor_cookie

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
MAX_BODY = 4 * 1024 * 1024
TIMEOUT = 8
_PROBE_CONTEXT = threading.local()
_DNS_SLOTS = threading.BoundedSemaphore(8)


class _ProbeDeadlineExceeded(TimeoutError):
    pass


def _remaining_probe_time():
    deadline = getattr(_PROBE_CONTEXT, 'deadline', None)
    if deadline is None:
        return TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ProbeDeadlineExceeded()
    # A configured check deadline owns the full DNS/connect/read budget. The
    # legacy eight-second socket default applies only without such a deadline.
    return min(60, remaining)


def _resolve_before_deadline(host, port):
    """Bound worker waiting; OS DNS calls cannot be cancelled by Python.

    At most eight daemon resolvers may outlive their caller. A resolver that
    returns late only releases its slot; it cannot populate a cookie jar or
    issue a connection after the check's deadline.
    """
    remaining = _remaining_probe_time()
    if not _DNS_SLOTS.acquire(blocking=False):
        raise _ProbeDeadlineExceeded()
    done = threading.Event()
    result = []
    def resolve():
        try:
            result.append(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
        except Exception as exc:
            result.append(exc)
        finally:
            _DNS_SLOTS.release()
            done.set()
    try:
        threading.Thread(target=resolve, daemon=True, name='probe-dns').start()
    except Exception:
        _DNS_SLOTS.release()
        raise
    if not done.wait(remaining):
        raise _ProbeDeadlineExceeded()
    _remaining_probe_time()
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


class _DeadlineConnection:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._deadline = getattr(_PROBE_CONTEXT, 'deadline', None)
        self._deadline_socket = None
        self._create_connection = self._connect_before_deadline
        remaining = _remaining_probe_time()
        timer = threading.Timer(remaining, self._expire_socket)
        timer.daemon = True
        self._deadline_timer = timer
        getattr(_PROBE_CONTEXT, 'watchdogs', []).append(timer)
        timer.start()

    def _expire_socket(self):
        current = self._deadline_socket
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current.close()
            except OSError:
                pass

    def _connect_before_deadline(self, address, timeout=None, source_address=None):
        _PROBE_CONTEXT.phase = 'dns'
        addresses = _resolve_before_deadline(*address)
        _PROBE_CONTEXT.phase = 'connect'
        last_error = None
        for family, kind, protocol, _, target in addresses:
            remaining = _remaining_probe_time()
            current = socket.socket(family, kind, protocol)
            self._deadline_socket = current
            try:
                current.settimeout(remaining)
                if source_address:
                    current.bind(source_address)
                current.connect(target)
                _remaining_probe_time()
                return current
            except OSError as exc:
                current.close()
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError('DNS returned no usable addresses')

    def connect(self):
        # HTTPConnection.connect also supports configured proxy tunnels.
        http.client.HTTPConnection.connect(self)
        _remaining_probe_time()


class _DeadlineHTTPConnection(_DeadlineConnection, http.client.HTTPConnection):
    pass


class _DeadlineHTTPSConnection(_DeadlineConnection, http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname,
                                              do_handshake_on_connect=False)
        self._deadline_socket = self.sock
        self.sock.settimeout(_remaining_probe_time())
        self.sock.do_handshake()
        _remaining_probe_time()


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        if getattr(_PROBE_CONTEXT, 'deadline', None) is None:
            return super().http_open(request)
        return self.do_open(_DeadlineHTTPConnection, request)


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        if getattr(_PROBE_CONTEXT, 'deadline', None) is None:
            return super().https_open(request)
        return self.do_open(_DeadlineHTTPSConnection, request, context=self._context)

@dataclass
class ProbeResult:
    live: bool | None
    nickname: str = ''
    stream_url: str = ''
    headers: dict[str, str] = field(default_factory=dict)
    message: str = ''
    live_started_at: str = ''
    live_session_id: str = ''
    avatar_url: str = ''
    started_at_source: str = ''
    stream_variants: list[dict] = field(default_factory=list)
    error_code: str = ''
    retry_after: int = 0
    resolved_room_input: str = ''
    diagnostic: str = ''
    live_ended_at: str = ''

class ProbeError(ValueError):
    """Safe error message without URLs, headers or credentials."""
    def __init__(self, message, code='invalid_input', retry_after=0, diagnostic=''):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.diagnostic = sanitize_probe_diagnostic(diagnostic)


def sanitize_probe_diagnostic(value):
    """Keep bounded facts only; response bodies, URLs and credentials stay out."""
    if not isinstance(value, str):
        return ''
    facts = {}
    for token in value[:512].split():
        key, _, item = token.partition('=')
        field = key.removeprefix('page_')
        if field == 'http' and re.fullmatch(r'[1-5][0-9]{2}', item):
            facts[key] = item
        elif field == 'platform_status' and re.fullmatch(r'-?[0-9]{1,9}', item):
            facts[key] = item
        elif field == 'body' and item in ('empty', 'non_json', 'missing_room', 'verification'):
            facts[key] = item
        elif field == 'stage' and item in ('room_web_enter', 'room_page', 'account_lookup', 'account_session', 'source_request', 'share_resolve'):
            facts[key] = item
        elif field == 'network' and item in ('dns', 'tls', 'timeout', 'connect', 'reset', 'unreachable', 'read', 'protocol', 'transport', 'deadline'):
            facts[key] = item
        elif field == 'phase' and item in ('dns', 'connect', 'read'):
            facts[key] = item
        elif field == 'elapsed_ms' and re.fullmatch(r'[0-9]{1,7}', item) and int(item) <= 3600000:
            facts[key] = item
        elif field == 'bytes' and re.fullmatch(r'[0-9]{1,8}', item) and int(item) <= MAX_BODY + 1:
            facts[key] = item
        elif field == 'retry_after' and re.fullmatch(r'[0-9]{1,5}', item) and int(item) <= 86400:
            facts[key] = item
        elif field == 'content_type' and item in ('json', 'html', 'text', 'other', 'missing'):
            facts[key] = item
        elif key in ('access', 'page_access') and item in ('anonymous', 'authorized'):
            facts[key] = item
        elif key == 'page_result' and item in ('failed', 'invalid', 'cooldown', 'skipped_network', 'deferred'):
            facts[key] = item
    return ' '.join(f'{key}={facts[key]}' for key in sorted(facts))


def anonymous_error_message(error_code, diagnostic=''):
    facts = dict(token.split('=', 1) for token in sanitize_probe_diagnostic(diagnostic).split())
    if facts.get('http') == '444':
        reason = '匿名直播接口暂时拒绝访问（HTTP 444）'
    elif error_code == 'rate_limited' or facts.get('http') == '429':
        reason = '匿名直播接口请求过于频繁（HTTP 429）'
    elif facts.get('http') in ('401', '403'):
        reason = f"匿名直播接口拒绝访问（HTTP {facts['http']}）"
    elif facts.get('body') == 'empty':
        reason = '匿名直播接口返回空响应'
    elif facts.get('body') == 'non_json':
        reason = '匿名直播接口返回非 JSON 内容（可能为验证页面）'
    elif 'platform_status' in facts:
        reason = f"匿名直播接口返回平台错误（状态码 {facts['platform_status']}）"
    else:
        reason = '匿名直播接口访问受限'
    return reason + '，本房间将自动重试。'


def monitor_error_message(error_code, diagnostic=''):
    facts = dict(token.split('=', 1) for token in sanitize_probe_diagnostic(diagnostic).split())
    if facts.get('access') != 'authorized':
        return anonymous_error_message(error_code, diagnostic)
    if error_code in ('auth_required', 'verification_required') or facts.get('body') == 'verification':
        return '授权监控需要重新登录或完成平台验证，请在抖音官方窗口处理后重试。'
    if facts.get('body') == 'empty':
        reason = '授权直播接口返回空响应'
    elif facts.get('http') in ('429', '444'):
        reason = '授权直播接口暂时限制访问（HTTP ' + facts['http'] + '）'
    elif facts.get('body') == 'non_json':
        reason = '授权直播接口返回非 JSON 内容，尚不能确认直播状态'
    elif 'platform_status' in facts:
        reason = '授权直播接口返回平台错误（状态码 ' + facts['platform_status'] + '）'
    else:
        reason = '授权直播接口暂未返回有效直播状态'
    return reason + '，将按监控策略自动重试。'

def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise ProbeError('请输入有效 HTTP 或 HTTPS 链接。')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProbeError('链接包含无效控制字符。')
    try:
        p = urllib.parse.urlsplit(value.strip())
        if p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or p.password is not None:
            raise ValueError
        p.port
    except ValueError:
        raise ProbeError('仅支持不含用户名密码的 HTTP 或 HTTPS 链接。') from None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=-._~"), urllib.parse.quote(p.query, safe="%=&/:?@!$'()*+,;+-._~"), ''))

def validate_headers(headers):
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > 40:
        raise ProbeError('请求头必须为 JSON 对象，最多 40 项。')
    result = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", key) or len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ProbeError('请求头无效，不可包含换行符。')
        try:
            value.encode('latin-1')
        except UnicodeEncodeError:
            raise ProbeError('请求头内容需为 ASCII 或 Latin-1 字符。') from None
        if key.lower() in ('host', 'content-length', 'transfer-encoding', 'connection'):
            raise ProbeError('不可覆盖 Host、连接或报文长度字段。')
        result[key] = value
    return result

def _douyin_target(url):
    host = (urllib.parse.urlsplit(url).hostname or '').lower().rstrip('.')
    domains = ('douyin.com', 'amemv.com', 'iesdouyin.com', 'douyincdn.com', 'douyinliving.com', 'douyinvod.com', 'douyinpic.com', 'snssdk.com', 'bytecdn.cn', 'bytefcdnrd.com', 'ibytedtos.com')
    return any(host == domain or host.endswith('.' + domain) for domain in domains)


def _anonymous_headers(headers):
    allowed = {'user-agent', 'accept', 'accept-language', 'accept-encoding', 'range', 'referer', 'origin'}
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _source_headers(url, headers):
    return _anonymous_headers(headers) if _douyin_target(url) else dict(headers)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = validate_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if redirected and (before.scheme.lower(), before.netloc.lower()) != (after.scheme.lower(), after.netloc.lower()):
            for key in list(redirected.headers):
                if key.lower() not in ('user-agent', 'accept', 'accept-language', 'accept-encoding', 'range'):
                    redirected.remove_header(key)
        return redirected


_SHARE_HOSTS = frozenset(('v.douyin.com', 'live.douyin.com', 'www.douyin.com',
    'douyin.com', 'www.iesdouyin.com', 'm.iesdouyin.com', 'iesdouyin.com', 'webcast.amemv.com'))


def _share_url(value):
    url = validate_url(value)
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in _SHARE_HOSTS
            or parsed.port not in (None, 443)):
        raise ProbeError('分享链接跳转到了不支持的地址，已停止解析。')
    return url


class _ShareRedirect(SafeRedirect):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _share_url(newurl))


def _share_opener():
    # Public guest traffic only. This instance is never shared with any account
    # lookup or authorized monitor transport, even on same-host redirects.
    jar = http.cookiejar.CookieJar(policy=_PublicCookiePolicy())
    return urllib.request.build_opener(_ShareRedirect(), _DeadlineHTTPHandler(),
        _DeadlineHTTPSHandler(), _StripAnonymousInput(),
        urllib.request.HTTPCookieProcessor(jar), _IdentityGuard(anonymous=True, public_cookies=True))


def _share_target(value):
    parsed = urllib.parse.urlsplit(_share_url(value))
    path = urllib.parse.unquote(parsed.path).rstrip('/')
    host = parsed.hostname
    if host == 'live.douyin.com':
        identifier = path.lstrip('/')
        if re.fullmatch(r'[A-Za-z0-9._-]{1,64}', identifier):
            return 'room', identifier, {}
    profile = re.fullmatch(r'/(?:share/)?user/(MS4[A-Za-z0-9._-]{8,200})', path)
    if host in ('www.douyin.com', 'douyin.com', 'www.iesdouyin.com', 'm.iesdouyin.com', 'iesdouyin.com') and profile:
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
        if any(key in query and query[key] != [profile.group(1)] for key in ('sec_uid', 'sec_user_id')):
            raise ProbeError('主页分享链接中的账号标识不一致，已停止解析。', 'target_mismatch', 120)
        return 'profile', profile.group(1), {'sec_uid': profile.group(1)}
    session = re.fullmatch(r'/(?:douyin/)?webcast/reflow/([0-9]{1,32})', path)
    if host == 'webcast.amemv.com' and session:
        if int(session.group(1)) <= 0:
            raise ProbeError('直播分享链接缺少有效场次，请重新复制直播分享链接。')
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
        owners = query.get('sec_user_id') or query.get('sec_uid') or []
        if len(owners) != 1 or not re.fullmatch(r'MS4[A-Za-z0-9._-]{8,200}', owners[0]):
            raise ProbeError('直播分享链接缺少可核实的主播标识，请重新复制该主播的直播分享链接。', 'target_mismatch', 120)
        if any(key in query and query[key] != owners for key in ('sec_uid', 'sec_user_id')):
            raise ProbeError('直播分享链接中的主播标识不一致，已停止解析。', 'target_mismatch', 120)
        return 'session', session.group(1), {'sec_uid': owners[0]}
    return None


PUBLIC_COOKIE_NAMES = frozenset(('ttwid', '__ac_nonce', '__ac_signature', 'msToken'))


class _PublicCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """A visitor jar accepts only public names from anonymous platform traffic."""
    def set_ok(self, cookie, request):
        return cookie.name in PUBLIC_COOKIE_NAMES and _douyin_target(request.full_url) and super().set_ok(cookie, request)

    def return_ok(self, cookie, request):
        return cookie.name in PUBLIC_COOKIE_NAMES and _douyin_target(request.full_url) and super().return_ok(cookie, request)


class _StripAnonymousInput(urllib.request.BaseHandler):
    # Run before HTTPCookieProcessor (500). Caller headers never seed this jar;
    # only the CookieProcessor may attach server-issued visitor cookies later.
    handler_order = 100
    def http_request(self, request):
        for key in list(dict(request.header_items())):
            if key.lower() not in {'user-agent', 'accept', 'accept-language', 'accept-encoding', 'range', 'referer', 'origin'}:
                request.remove_header(key)
        return request
    https_request = http_request


class _IdentityGuard(urllib.request.BaseHandler):
    handler_order = 1000
    def __init__(self, account_lookup=False, anonymous=False, public_cookies=False):
        self.account_lookup = account_lookup
        self.anonymous = anonymous
        self.public_cookies = public_cookies

    def http_request(self, request):
        parsed = urllib.parse.urlsplit(request.full_url)
        identity_endpoint = self.account_lookup and parsed.scheme == 'https' and (parsed.hostname or '').lower().rstrip('.') == 'www.douyin.com' and parsed.path in ('/aweme/v1/web/discover/search/', '/aweme/v1/web/user/profile/other/')
        if self.anonymous or (self.account_lookup and not identity_endpoint) or (_douyin_target(request.full_url) and not identity_endpoint):
            for key, value in list(dict(request.header_items()).items()):
                if key.lower() == 'cookie' and self.public_cookies and _douyin_target(request.full_url):
                    public = [part.strip() for part in value.split(';') if part.partition('=')[0].strip() in PUBLIC_COOKIE_NAMES and '=' in part]
                    request.remove_header(key)
                    if public:
                        request.add_unredirected_header('Cookie', '; '.join(public))
                elif key.lower() not in {'user-agent', 'accept', 'accept-language', 'accept-encoding', 'range', 'referer', 'origin'}:
                    request.remove_header(key)
        return request
    https_request = http_request


class _MonitorIdentityGuard(urllib.request.BaseHandler):
    """Attach the opted-in account only to exact first-party monitor endpoints.

    Credentials never travel on caller headers or a CookieJar, so redirects
    cannot move them to a CDN, account API, another host, or plain HTTP.
    """
    handler_order = 1100

    def __init__(self, cookie, room_id=''):
        self.cookie = cookie
        self.room_id = room_id

    def http_request(self, request):
        for key in list(dict(request.header_items())):
            if key.lower() not in {'user-agent', 'accept', 'accept-language', 'accept-encoding', 'range', 'referer', 'origin'}:
                request.remove_header(key)
        parsed = urllib.parse.urlsplit(request.full_url)
        path = urllib.parse.unquote(parsed.path)
        room_page = bool(self.room_id) and path.rstrip('/') == '/' + self.room_id
        monitor_api = path == '/webcast/room/web/enter/'
        if (parsed.scheme == 'https' and parsed.hostname == 'live.douyin.com'
                and parsed.port in (None, 443) and not parsed.username
                and not parsed.password and (room_page or monitor_api)):
            request.add_unredirected_header('Cookie', self.cookie)
        return request

    https_request = http_request


def _authorized_monitor_opener(cookie, room_id=''):
    return urllib.request.build_opener(SafeRedirect(), _DeadlineHTTPHandler(),
        _DeadlineHTTPSHandler(), _MonitorIdentityGuard(authorized_monitor_cookie(cookie), room_id))


def _scope_monitor_room(opener, identifier):
    for handler in getattr(opener, 'handlers', ()):
        if isinstance(handler, _MonitorIdentityGuard): handler.room_id = identifier


def _opener(account_lookup=False):
    return urllib.request.build_opener(SafeRedirect(), _DeadlineHTTPHandler(), _DeadlineHTTPSHandler(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _IdentityGuard(account_lookup=account_lookup))


def _anonymous_opener():
    # This jar is newly created here and never shared with account lookup.
    # Session/account cookies are rejected at storage and request boundaries.
    jar = http.cookiejar.CookieJar(policy=_PublicCookiePolicy())
    return urllib.request.build_opener(SafeRedirect(), _DeadlineHTTPHandler(), _DeadlineHTTPSHandler(), _StripAnonymousInput(), urllib.request.HTTPCookieProcessor(jar), _IdentityGuard(anonymous=True, public_cookies=True))


def _anonymous_result(result):
    result.headers = _anonymous_headers(result.headers)
    result.diagnostic = sanitize_probe_diagnostic(result.diagnostic)
    if result.error_code in ('auth_required', 'verification_required'):
        result.error_code = 'anonymous_restricted'
        result.retry_after = max(120, result.retry_after)
    if result.error_code in ('anonymous_restricted', 'rate_limited'):
        result.message = anonymous_error_message(result.error_code, result.diagnostic)
    return result


def _monitor_result(result, access):
    result.headers = _anonymous_headers(result.headers)
    if access != 'authorized':
        return _anonymous_result(result)
    result.diagnostic = sanitize_probe_diagnostic(result.diagnostic + ' access=authorized')
    facts = dict(token.split('=', 1) for token in result.diagnostic.split())
    if result.live is None:
        if facts.get('body') == 'verification' or facts.get('page_body') == 'verification':
            result.error_code = 'verification_required'
        elif result.error_code == 'auth_required' and facts.get('http') not in ('401', '403'):
            result.error_code = 'platform_unavailable'
        if result.error_code in ('auth_required', 'verification_required', 'anonymous_restricted', 'platform_unavailable', 'rate_limited'):
            result.message = monitor_error_message(result.error_code, result.diagnostic)
    return result


def _retry_after(headers):
    """Read server timing without retaining any header values in diagnostics."""
    value = next((str(value).strip() for key, value in headers.items() if key.lower() == 'retry-after'), '') if headers else ''
    if not value or len(value) > 128:
        return 0
    if value.isascii() and value.isdigit():
        return min(86400, int(value))
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            return 0
        return min(86400, max(0, math.ceil((deadline - datetime.now(timezone.utc)).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return 0


def _network_diagnostic(exc, phase, started):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    deadline = getattr(_PROBE_CONTEXT, 'deadline', None)
    if isinstance(reason, _ProbeDeadlineExceeded) or (deadline is not None and time.monotonic() >= deadline):
        category = 'deadline'
    elif isinstance(reason, socket.gaierror):
        category = 'dns'
    elif isinstance(reason, ssl.SSLError):
        category = 'tls'
    elif isinstance(reason, (TimeoutError, socket.timeout)) or getattr(reason, 'winerror', None) == 10060:
        category = 'timeout'
    elif isinstance(reason, ConnectionResetError) or getattr(reason, 'winerror', None) == 10054:
        category = 'reset'
    elif isinstance(reason, ConnectionRefusedError) or getattr(reason, 'winerror', None) == 10061:
        category = 'connect'
    elif getattr(reason, 'errno', None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN) or getattr(reason, 'winerror', None) in (10050, 10051, 10065):
        category = 'unreachable'
    elif isinstance(reason, http.client.HTTPException):
        category = 'protocol'
    else:
        # An unstructured URLError.reason can contain credentials or URLs; never
        # classify by copying or pattern-matching that untrusted message.
        category = 'read' if phase == 'read' else 'transport'
    elapsed = min(3600000, max(0, round((time.monotonic() - started) * 1000)))
    return f'network={category} phase={phase} elapsed_ms={elapsed}'


def _body_diagnostic(data, headers):
    content = next((str(value).lower().partition(';')[0].strip() for key, value in headers.items() if key.lower() == 'content-type'), '')
    kind = 'missing' if not content else 'json' if content == 'application/json' or content.endswith('+json') else 'html' if content in ('text/html', 'application/xhtml+xml') else 'text' if content.startswith('text/') else 'other'
    return f'bytes={min(len(data), MAX_BODY + 1)} content_type={kind}'


def _response_diagnostic(data, headers):
    status = headers.get('_livewatch_http_status')
    return _body_diagnostic(data, headers) + (f' http={status}' if isinstance(status, int) and 100 <= status <= 599 else '')


def _verification_page(data):
    # Recognize explicit challenge markup, not arbitrary mentions of captcha in
    # the platform's normal JavaScript bundle. No challenge is executed here.
    sample = data[:262144].decode('utf-8', 'ignore').lower()
    return bool(re.search(r'<title[^>]*>[^<]*(?:captcha|verification|安全验证|验证中心|访问验证)', sample)
                or re.search(r'<(?:div|iframe|form)[^>]+(?:id|class|src)=["\'][^"\']*(?:captcha-verify|verify-center|captcha_verify)', sample))


def _page_diagnostic(value, result):
    return 'page_result=' + result + ' ' + ' '.join('page_' + token for token in sanitize_probe_diagnostic(value).split())


def _request(url, headers=None, limit=MAX_BODY, partial=False, opener=None):
    url = validate_url(url)
    merged = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'} | validate_headers(headers or {})
    started = time.monotonic()
    phase = 'connect'
    previous_watchdogs = getattr(_PROBE_CONTEXT, 'watchdogs', None)
    watchdogs = _PROBE_CONTEXT.watchdogs = []
    _PROBE_CONTEXT.phase = phase
    try:
        timeout = _remaining_probe_time()
        with (opener or _opener()).open(urllib.request.Request(url, headers=merged), timeout=timeout) as response:
            phase = 'read'
            _PROBE_CONTEXT.phase = phase
            _remaining_probe_time()
            data = response.read(limit if partial else limit + 1)
            _remaining_probe_time()
            if not partial and len(data) > limit:
                raise ProbeError('平台返回数据过大，无法解析。')
            response_headers = dict(response.headers.items())
            status = getattr(response, 'status', None)
            if isinstance(status, int): response_headers['_livewatch_http_status'] = status
            return data, response_headers, validate_url(response.geturl())
    except urllib.error.HTTPError as exc:
        code = exc.code
        retry_after = _retry_after(exc.headers)
        diagnostic = f'http={code}' + (f' retry_after={retry_after}' if retry_after else '')
        exc.close()
        if code in (401, 403):
            raise ProbeError(f'访问被拒绝（HTTP {code}），请在独立登录窗口重新登录或完成验证。', 'auth_required', max(300, retry_after), diagnostic) from None
        if code in (429, 444):
            raise ProbeError(f'平台暂时限制访问（HTTP {code}），正在降低重试频率。', 'rate_limited', max(60, retry_after), diagnostic) from None
        raise ProbeError(f'平台返回 HTTP {code}，尚不能确认直播状态。', 'http_error', max(60, retry_after), diagnostic) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
        raise ProbeError('网络连接失败或超时，尚不能确认直播状态。', 'network_error', 30, _network_diagnostic(exc, getattr(_PROBE_CONTEXT, 'phase', phase), started)) from None
    except (ValueError, UnicodeError):
        raise ProbeError('链接或请求头格式无效。') from None
    finally:
        for timer in watchdogs:
            timer.cancel()
        _PROBE_CONTEXT.watchdogs = previous_watchdogs

def _json(data):
    try:
        return json.loads(data.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError):
        kind = 'empty' if not data.strip() else 'non_json'
        raise ProbeError('平台返回空内容或验证页面，请在独立登录窗口完成登录或验证。', 'auth_required', 300, 'body=' + kind) from None

def _objects(value, depth=0):
    if depth > 24:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _objects(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child, depth + 1)

def _nickname(value):
    if not isinstance(value, dict):
        return ''
    for key in ('owner', 'anchor', 'user'):
        obj = value.get(key)
        if isinstance(obj, dict) and isinstance(obj.get('nickname'), str):
            return obj['nickname'][:200]
    return value.get('nickname', '')[:200] if isinstance(value.get('nickname'), str) else ''

def _platform_time(value):
    """Normalize a genuine platform timestamp; never synthesize observation time."""
    if isinstance(value, bool) or value is None:
        return ''
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value.strip())):
            stamp = float(value)
            if stamp >= 100000000000:
                stamp /= 1000.0
            if not 946684800 <= stamp <= time.time() + 300:
                return ''
            result = datetime.fromtimestamp(stamp, timezone.utc)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            # A timezone-less string cannot identify a real instant reliably.
            if result.tzinfo is None or not 946684800 <= result.timestamp() <= time.time() + 300:
                return ''
        else:
            return ''
        return result.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return ''


def _avatar(owner):
    if not isinstance(owner, dict):
        return ''
    for key in ('avatar_thumb', 'avatar_medium', 'avatar_large', 'avatar'):
        image = owner.get(key)
        values = image.get('url_list', []) if isinstance(image, dict) else [image] if isinstance(image, str) else []
        for url in values if isinstance(values, list) else []:
            if isinstance(url, str) and url.startswith(('http://', 'https://')):
                try:
                    return validate_url(url)
                except ProbeError:
                    continue
    return ''


def _elapsed_start(data, payload=None, source_prefix='douyin.room.'):
    """Infer only from an explicitly named current-broadcast duration.

    Generic duration/elapsed_time fields are deliberately excluded: Douyin
    contains preview durations, media durations, booking windows and account
    totals that do not measure the current broadcast. Units must be explicit.
    """
    if not isinstance(data, dict):
        return '', ''
    reference = time.time()
    extra = payload.get('extra') if isinstance(payload, dict) else None
    if isinstance(extra, dict):
        server_now = _platform_time(extra.get('now'))
        if server_now:
            reference = datetime.fromisoformat(server_now).timestamp()
    for key, divisor in (('live_duration_seconds', 1), ('live_duration_ms', 1000)):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            elapsed = float(value) / divisor
        except (ValueError, OverflowError):
            continue
        # A finite nonnegative current-session duration up to 31 days.
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 31 * 86400:
            continue
        started = _platform_time(reference - elapsed)
        if started:
            return started, source_prefix + key + '(推算)'
    return '', ''


def start_time_priority(source):
    """Explicit broadcast start > elapsed inference; creation is not a start."""
    source = str(source or '')
    if source.rsplit('.', 1)[-1] in ('create_time', 'createTime'):
        return 0
    if '推算' in source:
        return 2
    return 3


def _room_metadata(room, payload=None):
    if not isinstance(room, dict):
        return {}
    actual, source = '', ''
    for key in ('live_start_time', 'start_time', 'liveStartTime', 'startTime'):
        actual = _platform_time(room.get(key))
        if actual:
            source = 'douyin.room.' + key
            break
    if not actual and room.get('status') == 2:
        actual, source = _elapsed_start(room, payload)
    identifier = room.get('id_str') or room.get('room_id') or room.get('id') or room.get('roomId')
    identifier = str(identifier) if isinstance(identifier, (str, int)) and not isinstance(identifier, bool) else ''
    if not re.fullmatch(r'[0-9]{1,32}', identifier):
        identifier = ''
    owner = next((room.get(key) for key in ('owner', 'anchor', 'user') if isinstance(room.get(key), dict)), {})
    ended = ''
    if room.get('status') == 4:
        for key in ('live_end_time', 'end_time', 'liveEndTime', 'endTime', 'finish_time'):
            ended = _platform_time(room.get(key))
            if ended:
                break
        if ended and actual and ended < actual:
            ended = ''
    return {'live_started_at': actual, 'started_at_source': source, 'live_session_id': identifier, 'avatar_url': _avatar(owner), 'live_ended_at': ended}


def _quality_label(key, declared=''):
    # A native key named origin can be advertised as 超清; the platform's
    # declared label wins. Never invent four qualities from a single URL.
    known = {'原画': 'origin', '蓝光': 'blue', '超清': 'ultra', '高清': 'high', '标清': 'standard', '流畅': 'low'}
    for label, quality in known.items():
        if label in declared:
            return quality, label
    defaults = {'origin': ('origin', '原画'), 'ORIGIN': ('origin', '原画'),
                'uhd': ('blue', '蓝光'), 'UHD': ('blue', '蓝光'),
                'hd': ('ultra', '超清'), 'FULL_HD1': ('ultra', '超清'),
                'HD1': ('high', '高清'), 'sd': ('high', '高清'), 'SD2': ('high', '高清'),
                'ld': ('standard', '标清'), 'SD1': ('standard', '标清'),
                'md': ('low', '流畅')}
    return defaults.get(key, ('other', declared or str(key)))


def stream_variants(stream):
    if not isinstance(stream, dict):
        return []
    variants, seen = [], set()
    def append(key, protocol, url, declared='', details=None):
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return
        try:
            url = validate_url(url)
        except ProbeError:
            return
        if url in seen:
            return
        quality, label = _quality_label(str(key), declared)
        variant = dict(quality=quality, label=label, protocol=protocol, url=url, source_key=str(key))
        details = details if isinstance(details, dict) else {}
        for field, value in [('resolution', details.get('resolution')), ('codec', details.get('v_codec')),
                             ('bitrate', details.get('v_bit_rate'))]:
            if isinstance(value, (str, int, float)):
                variant[field] = value
        variants.append(variant)
        seen.add(url)
    sdk = stream.get('live_core_sdk_data')
    pull = sdk.get('pull_data', {}) if isinstance(sdk, dict) else {}
    options = pull.get('options', {}) if isinstance(pull, dict) else {}
    declared = {}
    if isinstance(options, dict):
        for item in options.get('qualities', []) if isinstance(options.get('qualities'), list) else []:
            if isinstance(item, dict) and isinstance(item.get('sdk_key'), str) and not item.get('disable'):
                declared[item['sdk_key']] = item
        default = options.get('default_quality')
        if isinstance(default, dict) and isinstance(default.get('sdk_key'), str):
            declared.setdefault(default['sdk_key'], default)
    data = pull.get('stream_data') if isinstance(pull, dict) else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, RecursionError):
            data = None
    qualities = data.get('data', {}) if isinstance(data, dict) else {}
    if isinstance(qualities, dict):
        for key, value in qualities.items():
            if key in ('ao', 'audio', 'audio_only'):
                continue
            main = value.get('main') if isinstance(value, dict) else None
            if not isinstance(main, dict):
                continue
            info = declared.get(key, {})
            for protocol in ('hls', 'flv'):
                append(key, protocol, main.get(protocol), str(info.get('name', '')), info)
    for field, protocol in [('hls_pull_url_map', 'hls'), ('flv_pull_url', 'flv')]:
        values = stream.get(field)
        if isinstance(values, dict):
            for key, url in values.items():
                append(key, protocol, url)
    # The default HLS URL has no independent quality declaration. Preserve it
    # as a default source only if it differs from every declared quality.
    if isinstance(stream.get('hls_pull_url'), str):
        append('default', 'hls', stream['hls_pull_url'], '平台默认')
    rank = {'origin': 0, 'blue': 1, 'ultra': 2, 'high': 3, 'standard': 4, 'low': 5, 'other': 6}
    return sorted(variants, key=lambda item: (rank.get(item['quality'], 6), item['protocol'] != 'hls'))


def _pick_url(stream):
    variants = stream_variants(stream)
    return variants[0]['url'] if variants else ''


def _is_room(value):
    return isinstance(value, dict) and any(key in value for key in ('stream_url', 'owner', 'id_str', 'room_id', 'roomId', 'title'))

def _target_room(payload,include_identity=False):
    """Select the requested room, never a recommendation elsewhere in a response."""
    def selected(room,fallback,container=None):
        identities=[]
        if isinstance(room,dict):
            # Keep identity attached to the same selected object, including
            # supported nested roomInfo containers and their selected owner.
            sources=[room,container]
            sources.extend(room.get(key) for key in ('owner','anchor','user'))
            if isinstance(container,dict): sources.append(container.get('anchor'))
            for source in sources:
                if isinstance(source,dict):
                    identities.extend(source[key] for key in ('web_rid','webRid')
                                      if source.get(key) not in (None,''))
        return (room,fallback,identities) if include_identity else (room,fallback)
    data = payload.get('data')
    if isinstance(data, dict) and 'data' in data:
        rooms = data['data']
        room = rooms[0] if isinstance(rooms, list) and rooms and isinstance(rooms[0], dict) else None
        return selected(room,_nickname(data),data)
    if isinstance(data, dict) and _is_room(data.get('room')):
        return selected(data['room'],_nickname(data),data)
    if _is_room(payload) and 'status' in payload:
        return selected(payload,'')
    # Supported page-state containers only. Never recursively select arbitrary
    # objects with status=2: pages also contain unrelated live recommendations.
    queue = [payload]
    allowed = ('app', 'initialState', 'state', 'loaderData', 'props', 'pageProps', 'roomStore', 'roomInfo', 'initialData', 'liveRoom')
    for _ in range(24):
        if not queue:
            break
        next_level = []
        for container in queue:
            if not isinstance(container, dict):
                continue
            info = container.get('roomInfo')
            if isinstance(info, dict) and _is_room(info.get('room')):
                room = dict(info['room'])
                if not any(isinstance(room.get(k), dict) for k in ('owner', 'anchor', 'user')) and isinstance(info.get('anchor'), dict):
                    room['owner'] = info['anchor']
                return selected(room,_nickname(info),info)
            if _is_room(container.get('room')):
                return selected(container['room'],_nickname(container),container)
            for key in allowed:
                child = container.get(key)
                if isinstance(child, dict):
                    next_level.append(child)
        queue = next_level
    return selected(None,'')

def parse_douyin_data(payload, expected_sec_uid='',expected_web_rid=''):
    if not isinstance(payload, dict):
        return ProbeResult(None, message='抖音返回了无法识别的数据。')
    if payload.get('status_code') not in (None, 0):
        status_code = payload.get('status_code')
        diagnostic = 'platform_status=' + str(status_code) if type(status_code) in (int, str) and re.fullmatch(r'-?[0-9]{1,9}', str(status_code)) else ''
        return ProbeResult(None, message='抖音接口返回平台错误，尚不能确认直播状态。', error_code='auth_required', retry_after=300, diagnostic=diagnostic)
    room,fallback,identities=_target_room(payload,include_identity=True)
    if expected_web_rid and any(type(value) not in (str,int) or str(value)!=str(expected_web_rid) for value in identities):
        return ProbeResult(None,message='页面返回了非目标直播间，已拒绝使用。',error_code='target_mismatch')
    status = room.get('status') if isinstance(room, dict) else None
    if type(status) is int and status in (2, 4) and _is_room(room):
        if expected_sec_uid:
            owner = next((room.get(key) for key in ('owner', 'anchor', 'user') if isinstance(room.get(key), dict)), {})
            if owner.get('sec_uid') != expected_sec_uid:
                return ProbeResult(None, message='直播间所属账号无法核实，已拒绝使用非目标直播间。', error_code='target_mismatch', retry_after=120)
        name = _nickname(room) or fallback
        if status == 4:
            return ProbeResult(False, nickname=name, **_room_metadata(room, payload))
        url = _pick_url(room.get('stream_url'))
        return ProbeResult(True, nickname=name, stream_url=url, stream_variants=stream_variants(room.get('stream_url')), message='已开播，但平台暂未提供可录制直播源。' if not url else '', **_room_metadata(room, payload))
    return ProbeResult(None, nickname=fallback, message='未取得有效直播状态；请核对直播间链接，接口暂未返回可识别的目标房间数据。', error_code='platform_unavailable', diagnostic='body=missing_room')

def _page_states(body):
    page = body.decode('utf-8', errors='replace')
    for match in re.finditer(r'''<script\b[^>]*\bid=["'](?:RENDER_DATA|__NEXT_DATA__)["'][^>]*>(.*?)</script>''', page, re.I | re.S):
        content = html.unescape(match.group(1).strip())
        for candidate in (content, urllib.parse.unquote(content)):
            try:
                yield json.loads(candidate)
                break
            except (ValueError, RecursionError):
                pass
    # Current Douyin pages stream React state as JSON strings in __pace_f.
    # Decode data only, never execute page scripts. Restrict extraction to the
    # component's state.roomStore; recommendations elsewhere are not our room.
    decoder = json.JSONDecoder()
    chunks = []
    for script in re.finditer(r'<script\b[^>]*>(.*?)</script>', page, re.I | re.S):
        content = script.group(1).strip()
        match = re.match(r'self\.__pace_f\.push\(\s*', content)
        if not match:
            continue
        try:
            frame, _ = decoder.raw_decode(content[match.end():])
            if isinstance(frame, list) and len(frame) == 2 and frame[0] == 1 and isinstance(frame[1], str):
                chunks.append(frame[1])
        except (ValueError, RecursionError):
            continue
    flight = ''.join(chunks).encode('utf-8')
    cursor = 0
    while cursor < len(flight):
        header = re.match(rb'[0-9a-fA-F]+:', flight[cursor:])
        if not header:
            break
        cursor += header.end()
        # React text records have a UTF-8 byte length, not a newline delimiter.
        # A stream URL record can run directly into the next component record.
        text_record = re.match(rb'T([0-9a-fA-F]+),', flight[cursor:])
        if text_record:
            cursor += text_record.end() + int(text_record.group(1), 16)
            continue
        end = flight.find(b'\n', cursor)
        if end < 0:
            break
        record = flight[cursor:end]
        cursor = end + 1
        if not record.startswith(b'['):
            continue
        try:
            component = json.loads(record)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(component, list) and len(component) == 4 and component[0] == '$' and isinstance(component[3], dict):
            state = component[3].get('state')
            if isinstance(state, dict) and isinstance(state.get('roomStore'), dict):
                yield {'roomStore': state['roomStore']}

def parse_douyin_html(body, expected_sec_uid='', expected_web_rid=''):
    for state in _page_states(body):
        result = parse_douyin_data(state,expected_sec_uid,expected_web_rid)
        if result.live is not None or result.error_code == 'target_mismatch':
            return result
    if _verification_page(body):
        return ProbeResult(None, message='平台页面要求完成验证，请在抖音官方窗口处理。', error_code='verification_required', diagnostic='body=verification')
    return ProbeResult(None, message='抖音页面没有可识别直播状态，请核对直播间链接，稍后自动重试。', error_code='platform_unavailable', diagnostic='body=missing_room')

def _douyin_input(value, input_type='auto'):
    from .inputs import normalize_douyin_text
    try: value=normalize_douyin_text(value)
    except ValueError as exc: raise ProbeError(str(exc),'invalid_input') from None
    if input_type not in ('auto', 'room', 'account'):
        raise ProbeError('输入类型无效，请选择直播间号或抖音账号。')
    if value.startswith('@'):
        value = value[1:]
        input_type = 'account'
    if input_type != 'account' and re.fullmatch(r'[0-9]{1,32}', value):
        return 'room', value
    if not value.startswith(('http://', 'https://')):
        if re.fullmatch(r'[A-Za-z0-9._-]{2,64}', value):
            # Bare IDs follow the room input's default. Account search is
            # explicit via the selector or @ prefix, never an automatic fallback.
            return ('account' if input_type == 'account' else 'room'), value
        raise ProbeError('请填写直播间号、抖音账号 ID 或有效抖音链接。')
    url = validate_url(value)
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or '').lower()
    if host not in ('live.douyin.com','v.douyin.com','www.douyin.com','douyin.com','www.iesdouyin.com','iesdouyin.com','m.iesdouyin.com','webcast.amemv.com'):
        raise ProbeError('请输入抖音直播间链接、抖音分享链接或直播间数字号。')
    if host == 'live.douyin.com':
        room_id = p.path.strip('/')
        # Official web_rid accepts vanity paths such as lrc568568 as well as
        # numeric room numbers. A live URL is authoritative even if an imported
        # row was tagged as an account; never replace it with account search.
        if re.fullmatch(r'[0-9]{1,32}', room_id) or (re.fullmatch(r'[A-Za-z0-9._-]{2,64}', room_id) and re.search(r'[A-Za-z]', room_id)):
            return 'room', room_id
        raise ProbeError('抖音直播间链接缺少有效直播间号。')
    if host in ('www.douyin.com', 'douyin.com') and p.path.startswith('/user/'):
        sec_uid = p.path.split('/user/', 1)[1].strip('/').split('/')[0]
        if re.fullmatch(r'MS4[A-Za-z0-9._-]{8,200}', sec_uid):
            return 'profile', sec_uid
    return 'page', url

class ProviderRegistry:
    # Public room/status endpoints do not need the phone's account session.
    # Sources which really require authorization report it at their own boundary.
    requires_auth = False
    adaptive_polling = True
    paced_requests = True
    offline_confirmations = 2
    def __init__(self):
        self._local = threading.local()
        self._metadata_lock = threading.Lock()
        self._metadata_cache = {}
        self._account_cache = {}
        self._auth_generation = 0
        self._auth_fingerprint = None
        self._auth_pinned = False
        self._page_attempts = {}
        self._page_failures = {}
        self._status_page_pending = set()
        self._share_cache = {}

    @staticmethod
    def _credential_fingerprint(settings):
        credential = str(settings.get('douyin_cookie', '')) + '\0' + str(settings.get('douyin_user_agent', USER_AGENT)) + '\0' + str(settings.get('douyin_access_mode', 'anonymous')) + '\0' + str(settings.get('monitor_identity_isolation', True))
        return hashlib.sha256(credential.encode('utf-8')).digest()

    def _clear_auth_locked(self):
        self._auth_generation += 1
        self._account_cache.clear()
        self._page_attempts.clear()
        self._page_failures.clear()
        self._status_page_pending.clear()
        self._share_cache.clear()

    def invalidate_auth(self, settings=None):
        """Fence old requests and optionally pin the current app authorization."""
        with self._metadata_lock:
            self._clear_auth_locked()
            self._auth_pinned = settings is not None
            if settings is not None:
                self._auth_fingerprint = self._credential_fingerprint(settings)
        # Each worker replaces its own CookieJar lazily. Replacing the shared
        # threading.local object here lets an old request populate a new session.

    reset_session = invalidate_auth

    def _auth_state(self, settings):
        fingerprint = self._credential_fingerprint(settings)
        with self._metadata_lock:
            # A queued task carrying old settings must not switch authorization
            # back after the engine has explicitly installed the new credentials.
            if self._auth_pinned and fingerprint != self._auth_fingerprint:
                return None
            if fingerprint != self._auth_fingerprint:
                self._clear_auth_locked()
                self._auth_fingerprint = fingerprint
            generation = self._auth_generation
            if getattr(self._local, 'auth_generation', None) != generation:
                self._local.__dict__.clear()
                self._local.auth_generation = generation
            return generation

    def check(self, room, settings):
        # Recovery attempts share one absolute deadline across account lookup,
        # redirects, bootstrap/API/page requests, media reads and enrichment.
        previous = getattr(_PROBE_CONTEXT, 'deadline', None)
        deadline = settings.get('_probe_deadline')
        _PROBE_CONTEXT.deadline = deadline if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and math.isfinite(deadline) else None
        try:
            return self._check(room, settings)
        finally:
            _PROBE_CONTEXT.deadline = previous

    def _check(self, room, settings):
        generation = None
        try:
            platform = room.get('platform')
            if platform not in ('douyin', 'wechat'):
                raise ProbeError('不支持的平台。')
            if settings.get('_metadata_only'):
                return self._session_metadata(room, settings)
            options = room.get('transfer_options')
            if platform == 'wechat' and isinstance(options, dict) and 'wechat_bridge' in options:
                from .wechat import check_bridge
                return check_bridge(room, settings)
            headers = validate_headers(room.get('request_headers') or {})
            if room.get('status_url'):
                return self._status(room, headers, status_only=bool(settings.get('_status_only', False)))
            if room.get('stream_url'):
                if settings.get('_status_only') and not room.get('record_enabled'):
                    raise ProbeError('仅有直播源无法可靠确认下播。高频状态监控请配置状态接口，或关闭高频状态模式使用媒体探测。', 'status_source_required', 300)
                return self._stream(room, headers)
            if platform == 'douyin':
                generation = self._auth_state(settings)
                if generation is None:
                    return ProbeResult(None, message='授权已更新，正在使用新的登录资料重试。', error_code='auth_changed', retry_after=1)
                return self._douyin(room, settings, headers)
            return ProbeResult(None, message='视频号暂不支持仅凭账号 ID 或分享页自动查询。请填写有效直播源；自动判断开关播需配置状态接口。')
        except ProbeError as exc:
            result = ProbeResult(None, message=str(exc), error_code=exc.code, retry_after=exc.retry_after, diagnostic=exc.diagnostic)
            source = room.get('status_url') or room.get('stream_url')
            if source:
                result.diagnostic = sanitize_probe_diagnostic(result.diagnostic + ' stage=source_request')
            if source and _douyin_target(source):
                result = _anonymous_result(result)
            return result
        except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
            return ProbeResult(None, message='平台数据格式发生变化，暂不能确认直播状态。')

    def _status(self, room, headers, status_only=False):
        request_headers = _source_headers(room['status_url'], headers)
        body, _, _ = _request(room['status_url'], request_headers, opener=_anonymous_opener() if _douyin_target(room['status_url']) else None)
        data = _json(body)
        if not isinstance(data, dict) or type(data.get('live')) is not bool:
            raise ProbeError('状态接口需返回 JSON 对象，live 必须为 true 或 false。')
        name = data.get('nickname', '')
        if not isinstance(name, str):
            raise ProbeError('状态接口 nickname 必须为字符串。')
        started = _platform_time(data.get('live_started_at') or data.get('started_at'))
        time_source = 'status_api.started_at' if started else ''
        if not started and data['live'] is True:
            started, time_source = _elapsed_start(data, data, 'status_api.')
        session_id = data.get('live_session_id') or data.get('room_id') or ''
        session_id = str(session_id)[:128] if isinstance(session_id, (str, int)) and not isinstance(session_id, bool) else ''
        avatar = data.get('avatar_url') or ''
        if avatar:
            avatar = validate_url(avatar)
        metadata = dict(live_started_at=started, live_session_id=session_id, avatar_url=avatar, started_at_source=time_source)
        if not data['live']:
            return ProbeResult(False, nickname=name[:200], **metadata)
        if status_only and not room.get('record_enabled'):
            # State consumers never need to inspect media URLs or credentials.
            # A malformed/expired optional stream cannot invalidate a clear
            # boolean status; recording probes retain the existing media data.
            return ProbeResult(True, nickname=name[:200], **metadata)
        url = room.get('stream_url') or data.get('stream_url') or ''
        try:
            if url: url = validate_url(url)
            merged = _source_headers(url, headers | validate_headers(data.get('headers') or {}))
        except ProbeError:
            # Media selection and room state are independent. A broken optional
            # source must not erase an explicit successful live observation.
            return ProbeResult(True,nickname=name[:200],message='状态接口确认已开播，但直播源或媒体请求头无效，请重新取源。',**metadata)
        path=urllib.parse.urlsplit(url).path.lower() if url else ''
        protocol=next((kind for kind in ('flv','ts','mp4') if path.endswith('.'+kind)),'hls')
        variants=[{'quality':'unknown','label':'当前直播源','protocol':protocol,'url':url,'source_key':'status_api'}] if url else []
        return ProbeResult(True, nickname=name[:200], stream_url=url, headers=merged, stream_variants=variants,
                           message='状态接口报告已开播，但未提供直播源。' if not url else '', **metadata)

    def _stream(self, room, headers):
        headers = _source_headers(room['stream_url'], headers)
        body, _, url = _request(room['stream_url'], headers | {'Range': 'bytes=0-4095'}, 4096, True, opener=_anonymous_opener() if _douyin_target(room['stream_url']) else None)
        headers = _source_headers(url, headers)
        start = body.lstrip(b'\xef\xbb\xbf \r\n\t')
        playlist = start.startswith(b'#EXTM3U') and (b'#EXTINF' in start or b'#EXT-X-STREAM-INF' in start)
        flv = start.startswith(b'FLV') and len(start) >= 9
        mpegts = len(body) > 188 and body[0] == 0x47 and body[188] == 0x47
        mp4 = len(body) > 12 and body[4:8] in (b'ftyp', b'styp')
        if not any((playlist, flv, mpegts, mp4)):
            raise ProbeError('直播源未返回可识别媒体，链接可能过期或需要请求头。')
        return ProbeResult(True, stream_url=url, headers=headers, stream_variants=[{'quality':'other','label':'手动直播源','protocol':'hls' if playlist else 'flv' if flv else 'ts' if mpegts else 'mp4','url':url,'source_key':'manual'}], message='直播源可访问；停止开播需由状态接口确认，链接失效只记为异常。')

    def _resolve_account(self, kind, identifier, headers, opener):
        """Resolve an exact Douyin account, never a recommended/partial user."""
        cache_key = (kind, identifier.casefold())
        now = time.monotonic()
        with self._metadata_lock:
            generation = getattr(self._local, 'auth_generation', self._auth_generation)
            cached = self._account_cache.get(cache_key) if generation == self._auth_generation else None
        identity_checked = now
        if cached and now - cached['checked'] < 600:
            user = dict(cached['user'])
            identity_checked = cached['checked']
        elif kind == 'profile':
            user = {'sec_uid': identifier}
        else:
            params = {'device_platform': 'webapp', 'aid': '6383', 'channel': 'channel_pc_web',
                      'search_channel': 'aweme_user_web', 'keyword': identifier, 'search_source': 'normal_search',
                      'query_correct_type': '1', 'is_filter_search': '0', 'from_group_id': '', 'offset': '0',
                      'count': '10', 'pc_client_type': '1', 'version_code': '170400', 'version_name': '17.4.0'}
            body, _, _ = _request('https://www.douyin.com/aweme/v1/web/discover/search/?' + urllib.parse.urlencode(params), headers, opener=opener)
            payload = _json(body)
            if not isinstance(payload, dict) or payload.get('status_code') not in (None, 0):
                raise ProbeError('账号查询未通过平台验证，请在独立登录窗口登录或完成验证。', 'auth_required', 300)
            users = payload.get('user_list')
            if not isinstance(users, list):
                raise ProbeError('平台账号查询未返回有效结果，可能需要登录或验证。', 'auth_required', 300)
            matches = {}
            for item in users:
                candidate = item.get('user_info') if isinstance(item, dict) else None
                if not isinstance(candidate, dict):
                    continue
                account_ids = [str(candidate.get(field, '')) for field in ('unique_id', 'short_id')]
                if identifier.casefold() not in [value.casefold() for value in account_ids if value]:
                    continue
                sec_uid = candidate.get('sec_uid')
                if isinstance(sec_uid, str) and re.fullmatch(r'MS4[A-Za-z0-9._-]{8,200}', sec_uid):
                    matches[sec_uid] = candidate
            if len(matches) != 1:
                raise ProbeError('没有找到唯一且完全匹配的抖音账号 ID，请核对抖音号或粘贴用户主页链接。', 'account_not_found', 300)
            user = dict(next(iter(matches.values())))
        sec_uid = user.get('sec_uid')
        if not isinstance(sec_uid, str) or not re.fullmatch(r'MS4[A-Za-z0-9._-]{8,200}', sec_uid):
            raise ProbeError('未解析到目标账号的有效用户标识，请使用用户主页链接。', 'account_not_found', 300)
        params = {'device_platform': 'webapp', 'aid': '6383', 'channel': 'channel_pc_web', 'sec_user_id': sec_uid, 'personal_center_strategy': '1'}
        body, _, _ = _request('https://www.douyin.com/aweme/v1/web/user/profile/other/?' + urllib.parse.urlencode(params), headers, opener=opener)
        payload = _json(body)
        if not isinstance(payload, dict) or payload.get('status_code') not in (None, 0) or not isinstance(payload.get('user'), dict):
            raise ProbeError('目标账号资料查询未通过验证，请在独立登录窗口重新登录或完成验证。', 'auth_required', 300)
        profile = payload['user']
        if profile.get('sec_uid') != sec_uid:
            raise ProbeError('平台返回的账号与目标不一致，已拒绝使用该结果。', 'target_mismatch', 300)
        # Never carry a stale active room from a cached/search user record when
        # the current exact profile omits it or reports offline.
        for field in ('web_rid', 'webRid', 'room_id_str', 'room_id', 'live_room_id', 'room_data', 'live_status'):
            user.pop(field, None)
        user.update(profile)
        with self._metadata_lock:
            if generation == self._auth_generation:
                # Cache only account identity; profile/live state is requested
                # anew on every monitoring poll, including offline accounts.
                identity_fields = ('sec_uid', 'unique_id', 'short_id', 'nickname', 'avatar_thumb', 'avatar_medium', 'avatar_large', 'avatar')
                self._account_cache[cache_key] = {'checked': identity_checked, 'user': {key:user[key] for key in identity_fields if key in user}}
                if len(self._account_cache) > 1000:
                    oldest = min(self._account_cache, key=lambda key: self._account_cache[key]['checked'])
                    self._account_cache.pop(oldest, None)
        # web_rid is a stable browser room number. room_id is a per-broadcast
        # session ID; never feed it into the web_rid endpoint as if equivalent.
        if type(user.get('live_status')) is int and user['live_status'] == 0:
            return 'offline', '', user
        web_rid = user.get('web_rid') or user.get('webRid')
        session_id = user.get('room_id_str') or user.get('room_id') or user.get('live_room_id')
        room_data = user.get('room_data')
        if isinstance(room_data, str):
            try:
                room_data = json.loads(room_data)
            except (ValueError, RecursionError):
                room_data = None
        if isinstance(room_data, dict):
            web_rid = web_rid or room_data.get('web_rid') or room_data.get('webRid')
            session_id = session_id or room_data.get('id_str') or room_data.get('room_id')
        if isinstance(web_rid, (str, int)) and not isinstance(web_rid, bool):
            web_rid = str(web_rid)
            if (re.fullmatch(r'[0-9]{1,32}', web_rid) and int(web_rid) > 0) or (re.fullmatch(r'[A-Za-z0-9._-]{2,64}', web_rid) and re.search(r'[A-Za-z]', web_rid)):
                return 'room', web_rid, user
        if isinstance(session_id, (str, int)) and re.fullmatch(r'[0-9]{1,32}', str(session_id)) and int(session_id) > 0:
            return 'session', str(session_id), user
        if type(user.get('live_status')) is int and user['live_status'] == 0:
            return 'offline', '', user
        raise ProbeError('已找到目标账号，但平台未返回可查询的直播间；请使用直播间链接。', 'platform_unavailable', 120)

    def _check_account_session(self, session_id, user, headers):
        params = urllib.parse.urlencode({'room_id': session_id, 'type_id': '0', 'live_id': '1', 'app_id': '1128'})
        # Cookies collected for douyin.com are not sent to a different domain.
        body, _, _ = _request('https://webcast.amemv.com/webcast/room/reflow/info/?' + params,
                              {'Referer': 'https://live.douyin.com/', 'User-Agent': USER_AGENT}, opener=_anonymous_opener())
        payload = _json(body)
        target, _ = _target_room(payload) if isinstance(payload, dict) else (None, '')
        if not isinstance(target, dict) or str(target.get('id_str') or target.get('id') or '') != session_id:
            raise ProbeError('返回的直播场次与目标账号不一致，已拒绝使用该结果。', 'target_mismatch', 120)
        owner = target.get('owner')
        if not isinstance(owner, dict) or owner.get('sec_uid') != user.get('sec_uid'):
            raise ProbeError('直播间所属账号无法核实，已拒绝使用非目标直播间。', 'target_mismatch', 120)
        result = parse_douyin_data(payload)
        result.headers = {'Referer': 'https://live.douyin.com/', 'User-Agent': USER_AGENT}
        result = _anonymous_result(result)
        result.resolved_room_input = 'https://www.douyin.com/user/' + user['sec_uid']
        return result

    def _resolve_share_url(self, canonical_url):
        """Resolve only the target encoded by an official share redirect."""
        canonical_url = _share_url(canonical_url)
        target = _share_target(canonical_url)
        if target is not None:
            return target
        now = time.monotonic()
        generation = getattr(self._local, 'auth_generation', self._auth_generation)
        with self._metadata_lock:
            cached = self._share_cache.get(canonical_url) if generation == self._auth_generation else None
            if cached and now < cached['expires']:
                kind, identifier, metadata = cached['target']
                return kind, identifier, dict(metadata)
        try:
            body, _, resolved = _request(canonical_url,
                {'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9',
                 'Referer': 'https://www.douyin.com/'}, opener=_share_opener())
            target = _share_target(resolved)
        except ProbeError as exc:
            raise ProbeError(str(exc), exc.code, exc.retry_after,
                             exc.diagnostic + ' stage=share_resolve') from None
        if target is None:
            if _verification_page(body):
                raise ProbeError('分享页面要求验证，请在抖音官方窗口完成后重新复制链接。',
                                 'verification_required', 300, 'body=verification stage=share_resolve')
            raise ProbeError('分享链接尚未返回可核实的主页或直播间地址，请重新复制完整分享链接。',
                             'platform_unavailable', 60, 'body=missing_room stage=share_resolve')
        kind, identifier, metadata = target
        with self._metadata_lock:
            if generation == self._auth_generation:
                self._share_cache[canonical_url] = {'target': (kind, identifier, dict(metadata)),
                    'expires': time.monotonic() + (60 if kind == 'session' else 600)}
                while len(self._share_cache) > 512:
                    oldest = min(self._share_cache, key=lambda key: self._share_cache[key]['expires'])
                    self._share_cache.pop(oldest, None)
        return kind, identifier, dict(metadata)

    def _session_metadata(self, room, settings):
        """Supplement an already confirmed broadcast without probing its status.

        Only the coordinator's platform session ID selects the public reflow
        request. Room URLs, supplied status/media endpoints and credentials are
        never used here. Consumers must merge metadata only; this response is
        not a new observation of the monitored room's current live state.
        """
        sid = settings.get('_metadata_session_id')
        if (room.get('platform') != 'douyin' or room.get('status_url') or room.get('stream_url')
                or not isinstance(sid, str) or not re.fullmatch(r'[0-9]{1,32}', sid)):
            return ProbeResult(None, message='没有可补采开关播时间的平台场次。', error_code='metadata_unavailable')
        ended = bool(settings.get('_metadata_ended'))
        result = self._enrich_douyin(ProbeResult(not ended, live_session_id=sid), metadata_only=True)
        if (result.live_ended_at or result.live_started_at if ended else result.live_started_at or result.avatar_url):
            return result
        # Accept metadata only from the expected state of the exact session.
        # Empty, failed and mismatched responses are not status observations.
        return ProbeResult(None, live_session_id=sid, message='平台暂未提供本场'+('下播' if ended else '开播')+'时间，稍后继续补采。',
                           error_code=result.error_code or 'metadata_pending', retry_after=max(60, result.retry_after))

    def _enrich_douyin(self, result, metadata_only=False):
        """Fetch a session's real timestamps; never replace its observed live state.

        web-enter omits start_time on some rooms. The public mobile reflow API
        provides it, but can also return stale sessions. An ended metadata-only
        lookup requires that same session to be explicitly offline. Match both
        ID and state before accepting metadata. No caller cookies or custom
        authorization headers are sent to the supplemental API.
        """
        sid = result.live_session_id
        ended = metadata_only and result.live is False
        if (result.live is not True and not ended) or (result.live_started_at and start_time_priority(result.started_at_source) >= 3) or not sid:
            return result
        # A broadcast's cached live metadata cannot satisfy an ended lookup.
        cache_key = 'ended:' + sid if ended else sid
        now = time.monotonic()
        with self._metadata_lock:
            cached = self._metadata_cache.get(cache_key)
            cached_exact = bool(cached and (cached['metadata'].get('live_ended_at') and cached['metadata'].get('live_started_at') if ended else
                cached['metadata'].get('live_started_at') and start_time_priority(cached['metadata'].get('started_at_source', '')) >= 3))
            if cached and (cached_exact or now - cached['checked'] < max(60, cached.get('retry_after', 0))):
                metadata = cached['metadata']
                retry = False
                if metadata_only:
                    result.retry_after = max(0, math.ceil(cached['checked'] + cached.get('retry_after', 60) - now))
                    result.error_code = cached.get('error_code', '')
            else:
                metadata = {}
                retry = True
                self._metadata_cache[cache_key] = {'checked': now, 'metadata': {}}
                if len(self._metadata_cache) > 512:
                    oldest = min(self._metadata_cache, key=lambda key: self._metadata_cache[key]['checked'])
                    if oldest != cache_key:
                        self._metadata_cache.pop(oldest, None)
        if retry:
            params = urllib.parse.urlencode({'room_id': sid, 'type_id': '0', 'live_id': '1', 'app_id': '1128'})
            try:
                body, _, _ = _request('https://webcast.amemv.com/webcast/room/reflow/info/?' + params,
                                      {'Referer': 'https://live.douyin.com/', 'User-Agent': USER_AGENT}, opener=_anonymous_opener())
                details = parse_douyin_data(_json(body))
                if details.live_session_id == sid and details.live is result.live:
                    keys = ('live_started_at', 'started_at_source', 'live_ended_at') if ended else ('live_started_at', 'started_at_source', 'avatar_url')
                    metadata = {key: getattr(details, key) for key in keys}
                elif details.live is None and details.retry_after:
                    raise ProbeError(details.message, details.error_code, details.retry_after)
                with self._metadata_lock:
                    self._metadata_cache[cache_key] = {'checked': now, 'metadata': metadata}
            except ProbeError as exc:
                # Failure leaves live status/stream intact, and does not fabricate
                # a start time. Honor supplemental endpoint Retry-After too.
                retry_after = max(60, exc.retry_after)
                with self._metadata_lock:
                    self._metadata_cache[cache_key] = {'checked': now, 'metadata': {},
                                                'retry_after': retry_after, 'error_code': exc.code}
                if metadata_only:
                    result.retry_after = retry_after
                    result.error_code = exc.code
        if metadata.get('live_started_at') and (not result.live_started_at or start_time_priority(metadata.get('started_at_source', '')) >= start_time_priority(result.started_at_source)):
            result.live_started_at = metadata['live_started_at']
            result.started_at_source = metadata.get('started_at_source', '')
        if not result.avatar_url:
            result.avatar_url = metadata.get('avatar_url', '')
        if ended:
            result.live_ended_at = metadata.get('live_ended_at', '')
        return result

    def _douyin(self, room, settings, headers):
        raw = room.get('room_input', '')
        if not isinstance(raw, str) or not raw.strip():
            raise ProbeError('请输入抖音直播间链接或数字房间号。')
        kind, identifier = _douyin_input(raw, room.get('input_type', 'auto'))
        status_only = bool(settings.get('_status_only', False))
        isolate_identity = settings.get('monitor_identity_isolation', True) is not False
        share_user = None
        if kind == 'page':
            if status_only:
                target = _share_target(identifier)
                if target is None:
                    raise ProbeError('高频状态监控需要固定直播间链接（live.douyin.com/房间号）或状态接口；请先把短分享链接替换为直播间链接。', 'status_source_required', 300)
                kind, identifier, share_user = target
            else:
                kind, identifier, share_user = self._resolve_share_url(identifier)
        # A shared session is one broadcast, not a permanent browser room id.
        # With authorization, follow only its verified owner through the exact
        # current profile so later broadcasts of that same anchor are found.
        if kind == 'session' and not status_only and not isolate_identity and settings.get('douyin_cookie') and share_user:
            kind, identifier = 'profile', share_user['sec_uid']
        if status_only and kind != 'room':
            raise ProbeError('高频状态监控需要可跨场次复用的房间地址，不轮询账号资料或旧场次。请使用固定直播间链接（live.douyin.com/房间号）或状态接口。', 'status_source_required', 300)
        access = settings.get('douyin_access_mode', 'anonymous')
        if access not in ('anonymous', 'authorized'):
            raise ProbeError('监控访问方式无效，请在设置中重新选择。')
        # Account resolution, anonymous monitoring and opted-in room monitoring
        # use distinct openers. Recording/CDN headers always remain anonymous.
        # Isolation also covers account lookup: even profile/search requests
        # must not reuse the account session currently watched on the phone.
        request_headers = validate_headers(_anonymous_headers({'User-Agent': USER_AGENT, 'Referer': 'https://live.douyin.com/', 'Accept-Language': 'zh-CN,zh;q=0.9'} | headers))
        request_headers['User-Agent'] = USER_AGENT
        self._local.user_agent = USER_AGENT
        opener = getattr(self._local, 'anonymous_opener', None)
        if opener is None:
            opener = self._local.anonymous_opener = _anonymous_opener()
        if access == 'authorized' and not isolate_identity:
            try:
                cookie = authorized_monitor_cookie(settings.get('douyin_cookie', ''))
            except ValueError as exc:
                raise ProbeError(str(exc), 'auth_required', 300, 'access=authorized') from None
            request_headers['User-Agent'] = settings.get('douyin_user_agent') or USER_AGENT
            opener = getattr(self._local, 'monitor_opener', None)
            if opener is None:
                opener = self._local.monitor_opener = _authorized_monitor_opener(cookie)
        if kind == 'session':
            try:
                return _anonymous_result(self._check_account_session(identifier, share_user or {}, request_headers))
            except ProbeError as exc:
                diagnostic = sanitize_probe_diagnostic(exc.diagnostic + ' stage=account_session')
                raise ProbeError(str(exc), exc.code, exc.retry_after, diagnostic) from None
        page = None
        expected_sec_uid = ''
        if kind in ('account', 'profile'):
            cookie = settings.get('douyin_cookie', '')
            if not isolate_identity and not cookie:
                raise ProbeError('按抖音账号查询需要先在软件的独立窗口登录抖音。', 'auth_required', 300)
            if isolate_identity:
                account_headers = {'User-Agent': USER_AGENT, 'Referer': 'https://www.douyin.com/'}
                account_opener = opener
            else:
                account_headers = {'User-Agent': settings.get('douyin_user_agent') or USER_AGENT, 'Referer': 'https://www.douyin.com/', 'Cookie': cookie}
                account_opener = getattr(self._local, 'account_opener', None)
                if account_opener is None:
                    account_opener = self._local.account_opener = _opener(account_lookup=True)
            try:
                kind, identifier, user = self._resolve_account(kind, identifier, account_headers, account_opener)
            except ProbeError as exc:
                diagnostic = sanitize_probe_diagnostic(exc.diagnostic + ' stage=account_lookup')
                if isolate_identity and exc.code in ('auth_required', 'verification_required'):
                    raise ProbeError('匿名账号查询受到限制；请改用固定直播间链接，身份隔离模式不会使用手机账号登录资料。', 'anonymous_restricted', max(120, exc.retry_after), diagnostic) from None
                if exc.code == 'rate_limited':
                    raise ProbeError('账号资料查询频率受限，稍后重试。', 'account_rate_limited', exc.retry_after, diagnostic) from None
                raise ProbeError(str(exc), exc.code, exc.retry_after, diagnostic) from None
            expected_sec_uid = user.get('sec_uid', '')
            if kind == 'offline':
                return ProbeResult(False, nickname=_nickname(user), avatar_url=_avatar(user), resolved_room_input='https://www.douyin.com/user/' + str(user.get('sec_uid', '')))
            if kind == 'session':
                try:
                    return _anonymous_result(self._check_account_session(identifier, user, request_headers))
                except ProbeError as exc:
                    diagnostic = sanitize_probe_diagnostic(exc.diagnostic + ' stage=account_session')
                    if exc.code == 'auth_required':
                        raise ProbeError(anonymous_error_message('anonymous_restricted', diagnostic), 'anonymous_restricted', max(120, exc.retry_after), diagnostic) from None
                    raise ProbeError(str(exc), exc.code, exc.retry_after, diagnostic) from None
        monitor_access = 'anonymous' if isolate_identity else access
        monitor_opener = getattr(self._local, 'anonymous_opener', None) if isolate_identity else opener
        if monitor_opener is None:
            monitor_opener = self._local.anonymous_opener = _anonymous_opener()
        if isolate_identity:
            request_headers['User-Agent'] = USER_AGENT
        try:
            page_not_before=settings.get('_status_page_not_before',0)
            if isinstance(page_not_before,bool) or not isinstance(page_not_before,(int,float)) or not math.isfinite(page_not_before): page_not_before=0
            result = self._douyin_anonymous(kind, identifier, expected_sec_uid, request_headers, monitor_opener, monitor_access,
                                             status_only=status_only,status_page_not_before=page_not_before,
                                             single_request=bool(settings.get('_status_single_request')),
                                             page_requested=bool(settings.get('_status_page_requested')))
            if result.live is None and result.error_code == 'platform_unavailable' and room.get('input_type', 'auto') == 'auto' and not raw.strip().startswith(('@', 'http://', 'https://')):
                result.message += ' 若填写的是抖音账号，请选择“抖音账号”或在 ID 前加 @。'
            return result
        except ProbeError as exc:
            diagnostic = sanitize_probe_diagnostic(exc.diagnostic + ' stage=room_page' + (' access=authorized' if monitor_access == 'authorized' else ''))
            if monitor_access == 'authorized':
                return _monitor_result(ProbeResult(None, message=str(exc), error_code=exc.code, retry_after=exc.retry_after, diagnostic=diagnostic), monitor_access)
            if exc.code == 'auth_required':
                raise ProbeError(anonymous_error_message('anonymous_restricted', diagnostic), 'anonymous_restricted', max(120, exc.retry_after), diagnostic) from None
            raise ProbeError(str(exc), exc.code, exc.retry_after, diagnostic) from None

    def _status_room_page(self,identifier,expected_sec_uid,request_headers,opener,access):
        """Establish a public visitor and use this exact page's current state.

        Leave time to return through the worker pipe before its hard deadline;
        otherwise a slow initial response would repeatedly destroy its new jar.
        """
        deadline=getattr(_PROBE_CONTEXT,'deadline',None)
        if deadline is not None and deadline-time.monotonic()<=.35:
            return ProbeResult(None,message='访客初始化等待下一次有期限检查。',error_code='platform_unavailable',retry_after=1,diagnostic='page_result=cooldown')
        if deadline is not None: _PROBE_CONTEXT.deadline=deadline-.25
        try:
            page,response_headers,resolved=_request('https://live.douyin.com/'+identifier,request_headers,opener=opener)
        finally:
            _PROBE_CONTEXT.deadline=deadline
        self._local.status_bootstrapped=True
        self._local.bootstrapped=True
        self._local.last_bootstrap=time.monotonic()
        final=urllib.parse.urlsplit(resolved)
        if final.hostname!='live.douyin.com' or final.path.strip('/')!=identifier:
            return ProbeResult(None,message='页面跳转到了非目标直播间，已停止使用。',error_code='target_mismatch',retry_after=120,diagnostic='stage=room_page')
        result=_monitor_result(parse_douyin_html(page,expected_sec_uid,identifier),access)
        result.diagnostic=sanitize_probe_diagnostic(result.diagnostic+' stage=room_page '+_response_diagnostic(page,response_headers))
        if result.live is not None:
            result.headers=request_headers
            result.resolved_room_input='https://live.douyin.com/'+identifier
        return result

    def _status_page_needed(self,identifier,access,not_before):
        cold=access=='anonymous' and not getattr(self._local,'status_bootstrapped',False)
        return cold or identifier in self._status_page_pending and time.monotonic()>=max(not_before,self._page_attempts.get(identifier,0))

    @staticmethod
    def _status_page_budget():
        deadline=getattr(_PROBE_CONTEXT,'deadline',None)
        return TIMEOUT if deadline is None else max(0,deadline-time.monotonic())

    def _douyin_anonymous(self, kind, identifier, expected_sec_uid, request_headers, opener, access='anonymous', status_only=False,status_page_not_before=0,single_request=False,page_requested=False):
        if status_only and kind != 'room':
            raise ProbeError('高频状态监控需要固定直播间地址或状态接口。', 'status_source_required', 300)
        page = None
        page_facts = ''
        page_failure = ''
        page_retry_after = 0
        generation = getattr(self._local, 'auth_generation', self._auth_generation)
        if access == 'authorized': _scope_monitor_room(opener, identifier if kind == 'room' else '')
        status_page_attempted=False
        if status_only and (self._status_page_needed(identifier,access,status_page_not_before) or
                            page_requested and time.monotonic()>=status_page_not_before):
            with self._metadata_lock:
                self._status_page_pending.discard(identifier)
                self._page_attempts[identifier]=time.monotonic()+30
            status_page_attempted=True
            try:
                result=self._status_room_page(identifier,expected_sec_uid,request_headers,opener,access)
            except ProbeError as exc:
                raise ProbeError(str(exc),exc.code,exc.retry_after,exc.diagnostic+' stage=room_page') from None
            if result.live is not None or result.error_code=='target_mismatch': return result
            if single_request: return result
            page_failure=_page_diagnostic(result.diagnostic,'invalid')
            if self._status_page_budget()<.75:
                return result
        if kind == 'page':
            body, response_headers, resolved = _request(identifier, request_headers, opener=opener)
            page_facts = _response_diagnostic(body, response_headers)
            self._local.bootstrapped = True
            self._local.last_bootstrap = time.monotonic()
            final = urllib.parse.urlsplit(resolved)
            if (final.hostname or '').lower() not in ('live.douyin.com', 'www.douyin.com', 'douyin.com', 'v.douyin.com'):
                raise ProbeError('抖音分享链接跳转到了不支持的页面。')
            if final.hostname == 'live.douyin.com':
                kind, identifier = _douyin_input(resolved)
                page = body
            else:
                for state in _page_states(body):
                    target, _ = _target_room(state)
                    rid = target.get('web_rid') or target.get('webRid') if isinstance(target, dict) else None
                    if isinstance(rid, (str, int)) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', str(rid)):
                        kind, identifier = 'room', str(rid)
                        break
                if kind != 'room':
                    result = parse_douyin_html(body)
                    if result.live is not None:
                        result.headers = request_headers
                        return self._enrich_douyin(result)
                    raise ProbeError('分享页未解析到直播间号。请复制 live.douyin.com/直播间号 的直播间链接。')
            if access == 'authorized': _scope_monitor_room(opener, identifier if kind == 'room' else '')
        # Reuse an anonymous transport per worker. Bootstrap once, then bound
        # subsequent large-page refreshes independently for each room.
        bootstrap_attempted = False
        # A high-frequency status wave should call only the compact room-enter
        # endpoint. Full room HTML is useful metadata/fallback, but it is slow
        # and can trigger verification on a weak connection.
        if not status_only and not getattr(self._local, 'bootstrapped', False):
            bootstrap_attempted = True
            self._local.last_bootstrap = time.monotonic()
            try:
                page, response_headers, _ = _request('https://live.douyin.com/' + identifier, request_headers, opener=opener)
                page_facts = _response_diagnostic(page, response_headers)
                self._local.bootstrapped = True
            except ProbeError as exc:
                # Record an attempted bootstrap too, avoiding full-page polling.
                self._local.bootstrapped = True
                page_failure = _page_diagnostic(exc.diagnostic, 'failed')
                page_retry_after = exc.retry_after
                if exc.code in ('network_error','rate_limited','verification_required','auth_required'):
                    # DNS/connection failure to this host is shared by the API.
                    # Let the scheduler back off instead of making two more
                    # requests before it can observe this room's failure.
                    raise ProbeError(str(exc), exc.code, exc.retry_after,
                                     exc.diagnostic + ' stage=room_page') from None
        params = {'aid': '6383', 'app_name': 'douyin_web', 'live_id': '1', 'device_platform': 'web', 'language': 'zh-CN', 'browser_language': 'zh-CN', 'browser_platform': 'Win32', 'browser_name': 'Chrome', 'browser_version': '131.0.0.0', 'web_rid': identifier, 'enter_from': 'web_live', 'room_id_str': '', 'cookie_enabled': 'true', 'platform': 'web'}
        api = 'https://live.douyin.com/webcast/room/web/enter/?' + urllib.parse.urlencode(params)
        api_error = ''
        api_error_code = ''
        api_retry_after = 0
        api_diagnostic = ''
        api_body_facts = ''
        try:
            body, response_headers, _ = _request(api, request_headers, opener=opener)
            api_body_facts = _response_diagnostic(body, response_headers)
            result = parse_douyin_data(_json(body), expected_sec_uid)
            if result.live is None:
                result.diagnostic = sanitize_probe_diagnostic(result.diagnostic + ' stage=room_web_enter ' + api_body_facts)
            result = _monitor_result(result, access)
            if result.error_code == 'target_mismatch':
                return result
            if result.live is not None:
                result.headers = request_headers
                result.resolved_room_input = 'https://live.douyin.com/' + identifier
                return result if status_only else self._enrich_douyin(result)
            api_error = result.message
            api_error_code, api_retry_after = result.error_code, result.retry_after
            api_diagnostic = result.diagnostic
        except ProbeError as exc:
            api_error = str(exc)
            api_error_code, api_retry_after = exc.code, exc.retry_after
            api_diagnostic = sanitize_probe_diagnostic(exc.diagnostic + ' stage=room_web_enter ' + api_body_facts + (' access=authorized' if access == 'authorized' else ''))
        api_facts=dict(token.split('=',1) for token in sanitize_probe_diagnostic(api_diagnostic).split())
        access_restricted=(api_error_code in ('rate_limited','verification_required') or
                           api_facts.get('http') in ('401','403','429','444') or
                           api_facts.get('body')=='verification' or int(api_facts.get('retry_after','0'))>0)
        if access_restricted:
            # Retrying via HTML after a rejection still hits the same platform.
            # Return the restriction to the coordinator, including full-source
            # probes, so its cooldown cannot be hidden by an older page body.
            return _monitor_result(ProbeResult(None,message=api_error,error_code=api_error_code or 'platform_unavailable',
                retry_after=api_retry_after,diagnostic=api_diagnostic),access)
        if status_only:
            # Empty compact responses can recover with a normal public page
            # which refreshes visitor cookies. Bound large-page traffic per room,
            # and carry it to a later check if this request used its time budget.
            now=time.monotonic()
            with self._metadata_lock:
                page_due=now>=max(status_page_not_before,self._page_attempts.get(identifier,0))
                if len(self._page_attempts)>2048:
                    self._page_attempts={key:at for key,at in self._page_attempts.items() if at>now}
                    self._status_page_pending.intersection_update(self._page_attempts)
            facts=dict(token.split('=',1) for token in sanitize_probe_diagnostic(api_diagnostic).split())
            server_retry=int(facts.get('retry_after','0'))
            restricted=(api_error_code in ('rate_limited','verification_required') or
                        facts.get('http') in ('401','403','429','444') or facts.get('body')=='verification')
            if not status_page_attempted and page_due and not server_retry and not restricted:
                if single_request:
                    # The next scheduler slot owns the fallback request. Carry
                    # the hint through the parent so changing workers is safe.
                    with self._metadata_lock:
                        if len(self._status_page_pending)<2048: self._status_page_pending.add(identifier)
                    return _monitor_result(ProbeResult(None,message=api_error,
                        error_code=api_error_code or 'platform_unavailable',retry_after=max(api_retry_after,5),
                        diagnostic=api_diagnostic+' page_result=deferred'),access)
                if self._status_page_budget()<.75:
                    with self._metadata_lock:
                        if len(self._status_page_pending)<2048: self._status_page_pending.add(identifier)
                else:
                    with self._metadata_lock: self._page_attempts[identifier]=now+30
                    try:
                        result=self._status_room_page(identifier,expected_sec_uid,request_headers,opener,access)
                        if result.live is not None or result.error_code=='target_mismatch':
                            return result
                        page_failure=_page_diagnostic(result.diagnostic,'invalid')
                    except ProbeError as exc:
                        page_failure=_page_diagnostic(exc.diagnostic+' stage=room_page','failed')
                        page_retry_after=exc.retry_after
            return _monitor_result(ProbeResult(None,
                message=api_error or '抖音状态接口暂未确认直播状态，稍后自动重试。',
                error_code=api_error_code or 'platform_unavailable',
                retry_after=max(api_retry_after,page_retry_after,5),diagnostic=api_diagnostic+' '+page_failure),access)
        now = time.monotonic()
        with self._metadata_lock:
            current_generation = generation == self._auth_generation
            page_due = now >= (self._page_attempts.get(identifier, 0) if current_generation else 0)
            if current_generation and (page is not None or page_due):
                self._page_attempts[identifier] = now + 30
            if len(self._page_attempts) > 2048:
                self._page_attempts = {key: due for key, due in self._page_attempts.items() if due > now}
                self._page_failures = {key: value for key, value in self._page_failures.items() if key in self._page_attempts}
            previous_page_failure = self._page_failures.get(identifier, '') if current_generation else ''
        if page is None and api_error_code == 'network_error':
            page_failure = _page_diagnostic('', 'skipped_network')
        elif page is None and page_due and not bootstrap_attempted:
            try:
                page, response_headers, _ = _request('https://live.douyin.com/' + identifier, request_headers, opener=opener)
                page_facts = _response_diagnostic(page, response_headers)
            except ProbeError as exc:
                page_failure = _page_diagnostic(exc.diagnostic, 'failed')
                page_retry_after = exc.retry_after
        elif page is None and not page_due:
            page_failure = previous_page_failure + ' page_result=cooldown'
            cached_facts = dict(token.split('=', 1) for token in previous_page_failure.split())
            page_retry_after = int(cached_facts.get('page_retry_after', '0'))
        if page is not None:
            result = _monitor_result(parse_douyin_html(page, expected_sec_uid, identifier), access)
            if result.error_code == 'target_mismatch':
                return result
            if result.live is not None:
                result.headers = request_headers
                result.resolved_room_input = 'https://live.douyin.com/' + identifier
                # Page responses are much larger than the status API. When the
                # API is unavailable use fresh page state at a slower cadence.
                server_retry = dict(token.split('=', 1) for token in api_diagnostic.split()).get('retry_after', '0')
                result.retry_after = max(30, int(server_retry))
                result.diagnostic = 'stage=room_page' + (' access=authorized' if access == 'authorized' else '')
                with self._metadata_lock:
                    if generation == self._auth_generation: self._page_failures.pop(identifier, None)
                return self._enrich_douyin(result)
            page_failure = _page_diagnostic(result.diagnostic + ' ' + page_facts, 'invalid')
        if page_failure and 'page_result=cooldown' not in page_failure:
            with self._metadata_lock:
                if generation == self._auth_generation: self._page_failures[identifier] = sanitize_probe_diagnostic(page_failure)
        return _monitor_result(ProbeResult(None, message=api_error or '抖音接口暂未返回有效直播状态，稍后自动重试。', error_code=api_error_code or 'platform_unavailable', retry_after=max(api_retry_after or 60, page_retry_after), diagnostic=api_diagnostic + ' ' + page_failure), access)
