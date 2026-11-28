"""Live WeChat Channels checks through a user-started local page bridge.

The bridge executes joinLive again for every check. A captured URL or a saved
response is never a status source. Session identifiers do not discover the next
broadcast; a new capture must update them until account discovery is verified.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request


def _validate_base(base):
    if not isinstance(base, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/?', base):
        raise ValueError('视频号桥接地址必须是 http://127.0.0.1:端口。')
    port = int(base.removesuffix('/').rsplit(':', 1)[1])
    if not 1 <= port <= 65535:
        raise ValueError('视频号桥接端口无效。')
    return f'http://127.0.0.1:{port}'


def validate_bridge(config):
    """Return a normalized bridge configuration, or raise safe ValueError.

    No network access occurs. Unknown fields are rejected so captured cookies,
    response bodies and arbitrary URLs cannot become part of this contract.
    """
    fields = {'api_base', 'object_id', 'nonce_id', 'live_id', 'username'}
    if not isinstance(config, dict) or set(config) - fields:
        raise ValueError('视频号桥接配置字段无效。')
    result = {'api_base': _validate_base(config.get('api_base', 'http://127.0.0.1:2022'))}
    for key in ('object_id', 'live_id'):
        value = config.get(key)
        if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,31}', value):
            raise ValueError('视频号场次和作品 ID 必须为有效的十进制字符串。')
        result[key] = value
    nonce = config.get('nonce_id')
    if not isinstance(nonce, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,512}', nonce):
        raise ValueError('视频号场次校验标识无效，请重新捕获。')
    result['nonce_id'] = nonce
    username = config.get('username', '')
    if not isinstance(username, str) or (username and not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}@finder', username)):
        raise ValueError('视频号账号标识无效，请重新捕获。')
    result['username'] = username
    return result


def bridge_identity(room):
    """Stable tuple used to fence checks after capture or endpoint changes."""
    options = room.get('transfer_options')
    if not isinstance(options, dict) or 'wechat_bridge' not in options:
        return ()
    config = options['wechat_bridge']
    if not isinstance(config, dict):
        return ('invalid',)
    return tuple(str(config.get(key, '')) for key in ('api_base', 'object_id', 'nonce_id', 'live_id', 'username'))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A local service must not redirect requests or session IDs elsewhere.
        return None


def _zero(value):
    return type(value) is int and value == 0


def _identifier(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,31}', value):
        return value
    return ''


def _seconds(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value):
        return int(value)
    return None


def bridge_status(api_base):
    """Check page-bridge availability, with a three-second hard deadline.

    Always returns {available: bool, message: str}. Availability means that a
    page is connected, not that any captured broadcast is live.
    """
    from .providers import ProbeError, _DeadlineHTTPHandler, _PROBE_CONTEXT, _request
    previous = getattr(_PROBE_CONTEXT, 'deadline', None)
    deadline = time.monotonic() + 3
    if type(previous) in (int, float):
        deadline = min(deadline, previous)
    _PROBE_CONTEXT.deadline = deadline
    try:
        url = _validate_base(api_base) + '/api/channels/status'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), _DeadlineHTTPHandler())
        body, _, resolved = _request(url, {'Accept': 'application/json', 'Cache-Control': 'no-cache'}, limit=65536, opener=opener)
        payload = json.loads(body.decode('utf-8-sig'))
        if (resolved != url or not isinstance(payload, dict) or not _zero(payload.get('code'))
                or not isinstance(payload.get('data'), dict) or type(payload['data'].get('available')) is not bool):
            return {'available': False, 'message': '视频号桥接未返回有效连接状态。'}
        available = payload['data']['available']
        return {'available': available, 'message': '已连接微信视频号页面。' if available else '本机桥接已启动，请打开微信视频号页面。'}
    except (ProbeError, ValueError, UnicodeError, RecursionError):
        return {'available': False, 'message': '无法连接视频号桥接，请检查本机地址并启动捕获工具。'}
    finally:
        _PROBE_CONTEXT.deadline = previous


def check_bridge(room, settings):
    """Execute one local joinLive request and return a tri-state ProbeResult.

    ProviderRegistry owns the deadline context. No media is fetched, even for
    recording or high-frequency checks. Failures never confirm an offline state.
    """
    from .providers import (ProbeError, ProbeResult, _DeadlineHTTPHandler,
                            _platform_time, _request, validate_headers, validate_url)

    def unknown(message, code='wechat_bridge_invalid_response', diagnostic=''):
        return ProbeResult(None, message=message, error_code=code, diagnostic=diagnostic)

    try:
        config = validate_bridge(room.get('transfer_options', {}).get('wechat_bridge'))
    except (ValueError, AttributeError):
        return unknown('视频号桥接配置无效，请重新捕获直播。', 'wechat_bridge_invalid_config')
    query = urllib.parse.urlencode({'oid': config['object_id'], 'nid': config['nonce_id'], 'id': config['live_id']})
    url = config['api_base'] + '/api/channels/live/profile?' + query
    # Explicitly disable environment/system proxies, including when NO_PROXY is
    # absent. Preserve the application's cancellable socket/deadline handler.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), _DeadlineHTTPHandler())
    try:
        body, _, resolved = _request(url, {'Accept': 'application/json', 'Cache-Control': 'no-cache'}, opener=opener)
        if resolved != url:
            return unknown('视频号桥接返回了重定向地址，无法确认直播状态。')
        payload = json.loads(body.decode('utf-8-sig'))
    except ProbeError as exc:
        return unknown('视频号桥接连接失败或超时，请保持本机捕获工具和微信视频号页面打开。',
                       'wechat_bridge_unavailable', exc.diagnostic)
    except (ValueError, UnicodeError, RecursionError):
        return unknown('视频号桥接未返回有效 JSON，无法确认直播状态。')
    if not isinstance(payload, dict) or not _zero(payload.get('code')):
        return unknown('视频号桥接尚未就绪，请保持微信视频号页面打开。', 'wechat_bridge_unavailable')
    envelope = payload.get('data')
    if not isinstance(envelope, dict) or not _zero(envelope.get('errCode')):
        return unknown('微信视频号查询未成功，请检查页面连接或重新捕获直播。')
    data = envelope.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('baseResponse'), dict) or not _zero(data['baseResponse'].get('Ret')):
        return unknown('微信视频号未返回明确成功结果，无法确认直播状态。')
    info = data.get('liveInfo')
    if not isinstance(info, dict) or _identifier(info.get('liveId')) != config['live_id']:
        return unknown('微信视频号返回的场次不匹配，请重新捕获当前直播。', 'wechat_bridge_session_mismatch')
    sdk = data.get('liveSdkInfo', {})
    if not isinstance(sdk, dict) or ('liveId' in sdk and _identifier(sdk['liveId']) != config['live_id']):
        return unknown('微信视频号播放信息的场次不匹配。', 'wechat_bridge_session_mismatch')
    echoed = envelope.get('payload', {})
    if not isinstance(echoed, dict) or any(key in echoed and str(echoed[key]) != config[field]
            for key, field in (('liveId', 'live_id'), ('objectId', 'object_id'), ('objectNonceId', 'nonce_id'))):
        return unknown('微信视频号查询标识不匹配，请重新捕获。', 'wechat_bridge_session_mismatch')
    server_time = _seconds(data.get('serverTime'))
    if 'serverTime' in data and (server_time is None or abs(time.time() - server_time) > 300):
        return unknown('微信视频号返回的状态时间已过期或无效，请重新检查。', 'wechat_bridge_stale')
    status = info.get('liveStatus')
    ended_seconds = _seconds(info.get('endTime'))
    started_seconds = _seconds(info.get('startTime'))
    started = _platform_time(started_seconds) if started_seconds is not None else ''
    ended = _platform_time(ended_seconds) if ended_seconds else ''
    metadata = dict(live_session_id=config['live_id'], live_started_at=started,
                    started_at_source='wechat.joinLive.startTime' if started else '')
    if type(status) is not int:
        return unknown('微信视频号直播状态字段无效，暂不能判断。')
    if status == 1 and ended_seconds == 0:
        live = True
    elif status != 1 and ended and started and ended_seconds > started_seconds:
        # No offline enum is guessed: a valid explicit end for this same session
        # is required, together with a non-live status and coherent start time.
        return ProbeResult(False, live_ended_at=ended, **metadata)
    else:
        return unknown('微信视频号尚未提供一致的开播或结束信息，暂不能判断。')
    stream = ''
    for value in (info.get('streamUrl'), sdk.get('liveCdnUrl')):
        if not value:
            continue
        try:
            stream = validate_url(value)
            break
        except ProbeError:
            pass
    try:
        headers = validate_headers(room.get('request_headers') or {}) if stream else {}
    except ProbeError:
        stream, headers = '', {}
    path = urllib.parse.urlsplit(stream).path.lower() if stream else ''
    protocol = next((kind for kind in ('flv', 'ts', 'mp4') if path.endswith('.' + kind)), 'hls')
    variants = [{'quality': 'unknown', 'label': '视频号当前直播源', 'protocol': protocol,
                 'url': stream, 'source_key': 'wechat_bridge'}] if stream else []
    message = '已重新查询本场直播；下一场需重新捕获。' if stream else '本场已开播，但未返回可用直播源；下一场需重新捕获。'
    return ProbeResult(live, stream_url=stream, headers=headers, stream_variants=variants, message=message, **metadata)
