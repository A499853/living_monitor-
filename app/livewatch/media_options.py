"""Persisted media preferences, shared by playback and recording output."""
from pathlib import Path, PureWindowsPath
import os
import re

QUALITY_ORDER = ('origin', 'blue', 'ultra', 'high', 'standard', 'low', 'other', 'unknown')
QUALITY_PREFERENCES = ('below_origin_1', 'below_origin_2', *QUALITY_ORDER)


def select_stream(streams, quality='below_origin_2', protocol='hls', strict=False):
    candidates = [s for s in streams if s.get('url')]
    if protocol:
        matching = [s for s in candidates if s.get('protocol') == protocol]
        if matching or strict:
            candidates = matching
    if strict and quality:
        candidates = [s for s in candidates if s.get('quality') == quality]
    if not candidates:
        raise ValueError('当前未取得所选直播源，请等待检查完成或选择其他画质。')
    def rank(stream):
        value = stream.get('quality', 'unknown')
        return QUALITY_ORDER.index(value) if value in QUALITY_ORDER else len(QUALITY_ORDER)
    candidates.sort(key=rank)
    qualities = list(dict.fromkeys(s.get('quality', 'unknown') for s in candidates))
    if quality in ('below_origin_1', 'below_origin_2'):
        chosen = qualities[min(int(quality[-1]), len(qualities) - 1)]
        return next(s for s in candidates if s.get('quality', 'unknown') == chosen)
    if quality in QUALITY_ORDER:
        desired = QUALITY_ORDER.index(quality)
        return next((s for s in candidates if rank(s) >= desired), candidates[-1])
    return candidates[0]


def validate_record_directory(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError('录制存储位置无效或过长')
    value = value.strip()
    if not value:
        return ''
    path = PureWindowsPath(value)
    normalized=value.replace('/', '\\')
    if normalized.startswith(('\\\\.\\','\\\\?\\')) or (os.name=='nt' and path.root and not path.drive):
        raise ValueError('录制存储位置请使用完整盘符路径或相对目录')
    if any(c in value for c in '<>"|?*') or '..' in path.parts or (path.drive and not path.root):
        raise ValueError('录制存储位置必须为有效目录，不能包含上级路径或通配符')
    components=path.parts[1:] if path.anchor else path.parts
    if any(part.endswith((' ','.')) or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part) for part in components):
        raise ValueError('录制存储位置包含保留名称或无效的目录结尾')
    if any(':' in part for part in path.parts[1:] if part != path.anchor) or (not path.drive and ':' in value):
        raise ValueError('录制存储位置包含无效字符')
    return value


def recording_directory(root, settings, room=None):
    value = (room or {}).get('record_directory') or settings.get('recording_directory') or 'video'
    path = Path(validate_record_directory(value))
    if not path.is_absolute():
        path = Path(root) / path
    return path.absolute()
