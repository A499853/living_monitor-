"""Offline B-frame/AAC timestamp, wrap and partial-packet append checks."""
from __future__ import annotations

import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'app'))
from livewatch.recording_ts import CLOCK, PACKET_SIZE, WRAP, TransportStreamWriter, transport_tail
from livewatch.recording_ts import _PesHeaders, _packet_info, _put_timestamp, _unwrap

FFMPEG = PROJECT / 'tools' / 'ffmpeg.exe'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def command(arguments):
    return subprocess.run([str(FFMPEG), '-hide_banner', '-loglevel', 'error', *map(str, arguments)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=25, creationflags=FLAGS, check=True)


def packet_clocks(data, reference=None):
    headers = _PesHeaders()
    result = {}
    def observe(pid, pts, dts, *_refs):
        nonlocal reference
        pts = _unwrap(pts, reference)
        dts = _unwrap(dts, reference) if dts is not None else pts
        reference = max(pts, dts, reference if reference is not None else 0)
        result.setdefault(pid, []).append((pts, dts))
    for start in range(0, len(data), PACKET_SIZE):
        headers.feed(bytearray(data[start:start + PACKET_SIZE]), observe)
    return result


def feed_chunks(writer, data):
    cursor = 0
    sizes = (1, 187, 513, 1973, 65536)
    number = 0
    while cursor < len(data):
        size = sizes[number % len(sizes)]
        writer.write(data[cursor:cursor + size])
        cursor += size
        number += 1
    return writer.finish()


class TransportAppendChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recording-ts-join-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, offset=0):
        fixture = self.root / ('source-' + str(offset) + '.ts')
        command(['-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=10',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                 '-t', '3', '-c:v', 'libx264', '-bf', '3', '-g', '10',
                 '-x264-params', 'b-adapt=0', '-pix_fmt', 'yuv420p', '-threads', '1',
                 '-c:a', 'aac', '-output_ts_offset', str(offset), '-f', 'mpegts', fixture])
        return command(['-i', fixture, '-map', '0:v?', '-map', '0:a?', '-c', 'copy',
                        '-muxdelay', '0', '-muxpreload', '0', '-avoid_negative_ts', 'disabled',
                        '-output_ts_offset', '1', '-mpegts_flags', '+initial_discontinuity',
                        '-f', 'mpegts', 'pipe:1']).stdout

    def test_real_bframes_aac_two_connections_decode_in_one_file(self):
        first, second = self.fixture(100), self.fixture(0)
        output = self.root / 'one-recording.ts'
        writes = []
        with output.open('x+b') as handle:
            writer = TransportStreamWriter(handle, on_write=writes.append)
            original = feed_chunks(writer, first)
            self.assertEqual(output.read_bytes(), first, 'the initial connection must be byte-for-byte preserved')
            old = transport_tail(handle)
            self.assertGreater(writer.duration_seconds, 2.9)
            writer = TransportStreamWriter(handle, resume=True, reference_timestamp=original['end_timestamp'])
            joined = feed_chunks(writer, second)
            self.assertGreater(joined['duration_seconds'], 5.9)
            self.assertLess(joined['duration_seconds'], 6.7)
            self.assertEqual(joined['bytes_written'], len(second))
        data = output.read_bytes()
        self.assertEqual(len(data), len(first) + len(second))
        self.assertEqual(sum(writes), len(first))
        clocks = packet_clocks(data)
        self.assertEqual(len(clocks), 2)
        for pid, values in clocks.items():
            self.assertTrue(all(b[1] > a[1] for a, b in zip(values, values[1:])), (pid, values))
        resumed_clocks = packet_clocks(data[len(first):], old['end_timestamp'])
        self.assertTrue(all(min(dts for _, dts in values) >= old['next_timestamp'] for values in resumed_clocks.values()))
        decoded = command(['-nostats', '-progress', 'pipe:1', '-i', output, '-f', 'null', '-'])
        self.assertEqual(decoded.stderr, b'')
        duration = int(re.findall(rb'out_time_us=(\d+)', decoded.stdout)[-1]) / 1e6
        self.assertGreater(duration, 5.9)
        self.assertLess(duration, 6.7)

    def test_wrap_and_persisted_reference_survive_appending(self):
        source = self.fixture()
        shifted = bytearray()
        parser = _PesHeaders()
        delta = WRAP - 2 * CLOCK
        def shift(_pid, pts, dts, pts_refs, dts_refs):
            _put_timestamp(pts_refs, pts + delta)
            if dts is not None:
                _put_timestamp(dts_refs, dts + delta)
        packets = [bytearray(source[index:index + PACKET_SIZE]) for index in range(0, len(source), PACKET_SIZE)]
        for packet in packets:
            _pid, _payload, clocks = _packet_info(packet)
            for position, base in clocks:
                value = (base + delta) % WRAP
                packet[position:position + 4] = bytes((value >> 25, (value >> 17) & 255,
                                                       (value >> 9) & 255, (value >> 1) & 255))
                packet[position + 4] = (packet[position + 4] & 127) | ((value & 1) << 7)
            parser.feed(packet, shift)
        shifted = b''.join(packets)
        handle = io.BytesIO(shifted)
        tail = transport_tail(handle, reference_timestamp=WRAP)
        self.assertGreater(tail['end_timestamp'], WRAP)
        writer = TransportStreamWriter(handle, resume=True, reference_timestamp=tail['end_timestamp'])
        result = feed_chunks(writer, source)
        self.assertGreater(result['end_timestamp'], tail['end_timestamp'] + 2 * CLOCK)
        rescanned = transport_tail(handle, reference_timestamp=result['end_timestamp'])
        self.assertEqual(rescanned['end_timestamp'], result['end_timestamp'])
        values = packet_clocks(handle.getvalue(), WRAP)
        self.assertTrue(all(all(b[1] > a[1] for a, b in zip(items, items[1:])) for items in values.values()))

    def test_partial_last_packet_is_discarded_and_old_corruption_rejected(self):
        source = self.fixture()
        handle = io.BytesIO()
        writer = TransportStreamWriter(handle)
        result = feed_chunks(writer, source + source[:31])
        self.assertEqual(result['discarded_bytes'], 31)
        self.assertEqual(handle.getvalue(), source)
        with self.assertRaisesRegex(ValueError, '不完整'):
            TransportStreamWriter(io.BytesIO(source + b'x'), resume=True)

    def test_pes_header_split_between_transport_packets_is_shifted(self):
        header = bytearray(b'\x00\x00\x01\xe0\x00\x00\x80\xc0\x0a' + b'\x31\x00\x01\x00\x01\x11\x00\x01\x00\x01')
        _put_timestamp([(header, index) for index in range(9, 14)], 2 * CLOCK)
        _put_timestamp([(header, index) for index in range(14, 19)], CLOCK)
        first = bytearray(b'\x47\x41\x00\x30' + bytes([173, 0]) + bytes(172) + header[:10])
        second = bytearray(b'\x47\x01\x00\x11' + header[10:] + bytes(175))
        self.assertEqual(len(first), PACKET_SIZE)
        self.assertEqual(len(second), PACKET_SIZE)
        source = first + second
        handle = io.BytesIO(source)
        tail = transport_tail(handle)
        writer = TransportStreamWriter(handle, resume=True)
        feed_chunks(writer, source)
        appended = packet_clocks(handle.getvalue()[len(source):], tail['end_timestamp'])
        self.assertEqual(len(appended[256]), 1)
        self.assertGreater(appended[256][0][1], 2 * CLOCK)
        previous = handle.getvalue()
        writer = TransportStreamWriter(handle, resume=True)
        writer.write(source + first)
        result = writer.finish()
        self.assertEqual(result['discarded_bytes'], PACKET_SIZE)
        self.assertEqual(len(handle.getvalue()), len(previous) + len(source))


if __name__ == '__main__':
    unittest.main(verbosity=2)
