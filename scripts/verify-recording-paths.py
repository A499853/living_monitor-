"""Local recording naming checks, independent of user data and media streams."""
from datetime import datetime
from pathlib import Path
import re
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'app'))
from livewatch.recording_paths import recording_output_path, recording_room_directory, safe_nickname


class RecordingPaths(unittest.TestCase):
    def test_chinese_name_and_platform_without_room_id(self):
        room = dict(nickname='闽上茗', platform='douyin', id='first-room')
        self.assertEqual(recording_room_directory(room), '闽上茗_douyin')
        self.assertEqual(recording_room_directory(dict(room, id='second-room')), '闽上茗_douyin')
        self.assertEqual(recording_room_directory(dict(room, platform='wechat')), '闽上茗_wechat')

    def test_unsafe_names_stay_one_windows_component(self):
        for nickname in ['../../主播\\: *?<>|"', ' . ', '\x00\x1f\ud800主播', 'A\nB']:
            with self.subTest(nickname=repr(nickname)):
                result = recording_room_directory(dict(nickname=nickname, platform='douyin'))
                self.assertIsNone(re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', result))
                self.assertFalse(result.startswith(('.', ' ')))
                self.assertFalse(result.endswith(('.', ' ')))
        self.assertEqual(recording_room_directory(dict(platform='douyin')), '未命名直播间_douyin')

    def test_windows_reserved_names_and_unicode_length(self):
        for name in ['CON', 'prn', 'Aux.txt', 'NUL', 'COM1', 'LPT9', 'COM¹', 'CONIN$']:
            with self.subTest(name=name):
                self.assertTrue(safe_nickname(name).startswith('_'))
        self.assertEqual(safe_nickname('主播.  '), '主播')
        self.assertLessEqual(len(safe_nickname('😀' * 100).encode('utf-16-le')) // 2, 72)
        self.assertLessEqual(len(recording_room_directory(dict(nickname='中' * 500, platform='douyin'))), 79)

    def test_timestamp_only_and_collisions_preserve_files(self):
        when = datetime(2026, 11, 27, 8, 9, 10)
        with tempfile.TemporaryDirectory(prefix='livewatch-paths-') as folder:
            directory = Path(folder) / '闽上茗_douyin'
            directory.mkdir()
            first = recording_output_path(directory, when)
            self.assertEqual(first.name, '2026-11-27_08-09-10.ts')
            self.assertFalse(first.exists(), 'path selection does not create an output')
            first.write_bytes(b'existing recording')
            second = recording_output_path(directory, when)
            self.assertEqual(second.name, '2026-11-27_08-09-10_001.ts')
            segment = second.with_name(second.stem + '_000000.ts')
            segment.write_bytes(b'existing segment')
            self.assertEqual(recording_output_path(directory, when).name, '2026-11-27_08-09-10_002.ts')
            self.assertEqual(first.read_bytes(), b'existing recording')
            self.assertEqual(segment.read_bytes(), b'existing segment')

    def test_windows_total_path_budget_and_astral_units(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-paths-') as folder:
            root = Path(folder).resolve()
            root_units = len(str(root).encode('utf-16-le')) // 2
            suffix_units = len('/2026-11-27_08-09-10_9999_000000.ts')
            component_units = 240 - root_units - 1 - suffix_units
            self.assertGreater(component_units, 0)
            directory = root / ('中' * component_units)
            output = recording_output_path(directory, datetime(2026, 11, 27, 8, 9, 10))
            self.assertLessEqual(len(str(output.resolve()).encode('utf-16-le')) // 2, 240)
            for long_directory in [root / ('中' * (component_units + 1)), root / ('😀' * (component_units // 2 + 1))]:
                with self.subTest(directory=str(long_directory)):
                    with self.assertRaisesRegex(ValueError, '路径过长'):
                        recording_output_path(long_directory)


if __name__ == '__main__':
    unittest.main(verbosity=2)
