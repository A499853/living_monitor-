"""Recording-only names; broadcast CSV folders retain their own identifiers."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re


def _windows_units(value):
    return len(str(value).encode('utf-16-le')) // 2


def safe_nickname(value, limit=72):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(value or ''))
    value = ''.join(char for char in value if not 0xD800 <= ord(char) <= 0xDFFF)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    value = value[:limit].rstrip(' .') or '未命名直播间'
    while _windows_units(value) > limit:
        value = value[:-1]
    value = value.rstrip(' .') or '直播间'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', value):
        value = '_' + value
    return value


def recording_room_directory(room):
    """Use the displayed name and platform, including for identically named rooms."""
    nickname = safe_nickname(room.get('nickname'))
    platform = safe_nickname(room.get('platform') or 'unknown', limit=20)
    return nickname + '_' + platform


def recording_output_path(directory, when=None):
    """Choose a timestamp name without modifying or replacing existing files.

    The caller starts the writer before choosing another path. Reserve space for
    collision and segment suffixes under the same 240 UTF-16-unit Windows budget
    used for recording paths elsewhere in the application.
    """
    directory = Path(directory)
    stamp = (when or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')
    longest = directory.resolve() / (stamp + '_9999_000000.ts')
    if _windows_units(longest) > 240:
        raise ValueError('软件目录路径过长，请移动到较短的目录后重新录制')
    for number in range(10000):
        suffix = '' if number == 0 else f'_{number:03d}'
        output = directory / (stamp + suffix + '.ts')
        if not os.path.lexists(output) and not os.path.lexists(output.with_name(output.stem + '_000000.ts')):
            return output
    raise ValueError('同一时间的录制文件数量过多，请更换保存目录')
