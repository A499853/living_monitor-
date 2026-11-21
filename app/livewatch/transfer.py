"""Portable room list interchange: legacy KD (base64+zlib JSON) and UTF-8 CSV."""
from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import zlib
from urllib.parse import urlsplit, urlunsplit

from .providers import _douyin_input
from .inputs import canonical_share_url, split_douyin_batch

MAX_FILE_BYTES=5*1024*1024
MAX_DECODED_BYTES=16*1024*1024
MAX_ROOMS=1000
CSV_COLUMNS=('room_id','profile_url')
COLUMN_ALIASES={'直播间ID':'room_id','直播间 ID':'room_id','直播间id':'room_id','直播间号':'room_id',
    '主页链接':'profile_url','主页地址':'profile_url','主播主页':'profile_url','抖音主页':'profile_url','homepage_url':'profile_url','homepage_link':'profile_url','profile_link':'profile_url',
    '平台':'platform','直播间':'room_input','直播间链接':'room_input','抖音号':'room_input','输入类型':'input_type'}


def _platform(value):
    aliases={'douyin':'douyin','抖音':'douyin','wechat':'wechat','视频号':'wechat','微信视频号':'wechat','channels':'wechat'}
    if not isinstance(value,str) or value not in aliases: raise ValueError('仅支持抖音或视频号条目')
    return aliases[value]


def _plain_text(value):
    if not isinstance(value,str): raise ValueError('直播间字段应为文本')
    # Reverse exactly the formula escape produced by this exporter.
    return value[1:] if len(value)>1 and value[0]=="'" and (value[1] in '\t\r\n' or value[1:].lstrip(' \t\r\n').startswith(('=','+','-','@'))) else value


def _safe_cell(value):
    text=str(value)
    return "'"+text if text.startswith(('\t','\r','\n')) or text.lstrip(' \t\r\n').startswith(('=','+','-','@')) else text


def _profile_url(value):
    if not value:
        return ''
    value = _plain_text(value).strip()
    if not value:
        return ''
    kind, identifier = _douyin_input(value)
    if kind=='page' and urlsplit(identifier).hostname=='v.douyin.com':
        return identifier
    if kind != 'profile':
        raise ValueError('主页链接应为抖音 /user/MS4… 主页地址')
    return 'https://www.douyin.com/user/' + identifier


def _identifiers(value, input_type='auto', platform='douyin'):
    """Canonical identifiers only; never carry source URLs or settings."""
    if not isinstance(value, str):
        raise ValueError('直播间 ID 或链接应为文本')
    value = _plain_text(value).strip()
    if value.startswith('wechat:'):
        value, platform = value[len('wechat:'):], 'wechat'
    if platform == 'wechat':
        if not value:
            raise ValueError('直播间 ID 不能为空')
        return 'wechat:' + value, ''
    kind, identifier = _douyin_input(value, input_type)
    if kind == 'profile':
        return '', 'https://www.douyin.com/user/' + identifier
    if kind == 'account':
        return '@' + identifier, ''
    if kind == 'room':
        return identifier, ''
    # Short/share links are retained as identifying links, without tracking data.
    identifier=canonical_share_url(identifier)
    parsed = urlsplit(identifier)
    if parsed.hostname=='webcast.amemv.com': return identifier,''
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')), ''


def _import_room(item):
    if not isinstance(item, dict):
        raise ValueError('直播间条目应为对象')
    normalized = {COLUMN_ALIASES.get(str(key).strip(), str(key).strip()): value
                  for key, value in item.items() if key is not None}
    platform = _platform(normalized.get('platform') or 'douyin')
    input_type = normalized.get('livewatchInputType', normalized.get('input_type', 'auto')) or 'auto'
    if input_type == 'auto' and normalized.get('inputType') in ('account', 'douyin_account'):
        input_type = 'account'
    value = normalized.get('room_id') or normalized.get('url') or normalized.get('room_input') or ''
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    profile = _profile_url(normalized.get('profile_url') or '')
    if value:
        room_id, embedded_profile = _identifiers(value, input_type, platform)
        profile = profile or embedded_profile
    else:
        room_id = ''
    if not room_id and not profile:
        raise ValueError('请填写直播间 ID 或主页链接')
    if room_id.startswith('wechat:'):
        room_input, platform, input_type = room_id[len('wechat:'):], 'wechat', 'auto'
    elif room_id:
        input_type = 'account' if room_id.startswith('@') else 'auto'
        room_input = room_id[1:] if input_type == 'account' else room_id
    else:
        room_input, input_type = profile, 'auto'
    # Preserve a second identifier when both columns describe the same row.
    options = {'profile_url': profile} if room_id and profile else {}
    return {'platform': platform, 'room_input': room_input, 'input_type': input_type,
            'monitor_enabled': True, 'record_enabled': False, 'transfer_options': options}


def decode_import(format_name,content_base64):
    if format_name not in ('kd','csv'): raise ValueError('仅支持 .kd 或 .csv 文件')
    if not isinstance(content_base64,str) or len(content_base64)>MAX_FILE_BYTES*4//3+8: raise ValueError('导入文件过大（最多 5 MB）')
    try: raw=base64.b64decode(content_base64,validate=True)
    except (ValueError,binascii.Error): raise ValueError('导入文件编码无效') from None
    if not raw or len(raw)>MAX_FILE_BYTES: raise ValueError('导入文件为空或超过 5 MB')
    warnings=[]
    if format_name=='kd':
        try:
            packed=base64.b64decode(re.sub(rb'\s+',b'',raw),validate=True)
            decoder=zlib.decompressobj()
            unpacked=decoder.decompress(packed,MAX_DECODED_BYTES+1)
            if len(unpacked)>MAX_DECODED_BYTES or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                raise ValueError()
            items=json.loads(unpacked.decode('utf-8-sig'))
        except (ValueError,zlib.error,UnicodeError,binascii.Error,RecursionError):
            raise ValueError('无法识别 KD：支持 Base64 + zlib + UTF-8 JSON 格式；加密或其他厂商格式不支持') from None
        if not isinstance(items,list): raise ValueError('KD 顶层必须为直播间数组')
        convert=_import_room
    else:
        try: text=raw.decode('utf-8-sig')
        except UnicodeError:
            try: text=raw.decode('gb18030');warnings.append('文件按 GB18030 读取；导出统一为 UTF-8 BOM。')
            except UnicodeError: raise ValueError('CSV 编码无效，请保存为 UTF-8') from None
        try:
            reader=csv.DictReader(io.StringIO(text,newline=''))
            if not reader.fieldnames or not any(COLUMN_ALIASES.get(name.strip(),name.strip()) in ('room_id','profile_url','room_input','url') for name in reader.fieldnames):
                raise ValueError('CSV 缺少 room_id（直播间 ID）或 profile_url（主页链接）列')
            items=[]
            for item in reader:
                if len(items)>=MAX_ROOMS: raise ValueError('一次最多导入 1000 个直播间')
                if any(value and str(value).strip() for value in item.values()): items.append(item)
        except csv.Error: raise ValueError('CSV 内容格式无效或单元格过长') from None
        convert=_import_room
    warnings.append('仅导入直播间 ID 和主页链接；默认开启监控、关闭自动录制，昵称、备注、直播源和其它配置不导入。')
    if not items: raise ValueError('文件中没有直播间条目')
    if len(items)>MAX_ROOMS: raise ValueError('一次最多导入 1000 个直播间')
    payloads=[]
    for item in items:
        try: payloads.append(convert(item))
        except (ValueError,TypeError,AttributeError) as exc: payloads.append({'_import_error':str(exc)})
    return payloads,warnings


def batch_payloads(text,defaults):
    if not isinstance(text,str) or len(text)>512*1024: raise ValueError('批量链接内容过长')
    allowed={key:defaults[key] for key in ('platform','input_type','monitor_enabled','record_enabled','remark') if key in defaults}
    values=split_douyin_batch(text) if allowed.get('platform','douyin')=='douyin' else [value.strip() for value in text.splitlines() if value.strip()]
    if not values: raise ValueError('请每行填写一个直播间链接或标识')
    if len(values)>MAX_ROOMS: raise ValueError('一次最多添加 1000 个直播间')
    return [{**allowed,'room_input':value} for value in values]


def export_rooms(format_name,rooms):
    if format_name not in ('kd','csv'): raise ValueError('仅支持 .kd 或 .csv 格式')
    items=[]
    for room in rooms:
        room_id,profile=_identifiers(room['room_input'],room.get('input_type','auto'),room.get('platform','douyin'))
        options=room.get('transfer_options',{})
        if not profile and isinstance(options,dict):
            # The only retained metadata is a validated second identifier.
            profile=_profile_url(options.get('profile_url') or '')
        items.append({'room_id':room_id,'profile_url':profile})
    if format_name=='kd':
        data=base64.b64encode(zlib.compress(json.dumps(items,ensure_ascii=False,separators=(',',':')).encode('utf-8'),9))
        return data,'application/octet-stream','直播间列表.kd'
    output=io.StringIO(newline='');writer=csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for item in items:
        writer.writerow([_safe_cell(item[key]) for key in CSV_COLUMNS])
    return output.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8','直播间列表.csv'
