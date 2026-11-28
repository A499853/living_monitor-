"""Bounded MPEG-TS timestamp inspection and append-only connection joining.

Feed normalized FFmpeg TS output to ``TransportStreamWriter``. A resumed
connection is shifted as a whole (PTS, DTS, PCR and OPCR), while the first
connection is preserved. No transport packet is inserted or removed.
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
from statistics import median

PACKET_SIZE = 188
CLOCK = 90000
WRAP = 1 << 33
TAIL_BYTES = 2 * 1024 * 1024
PREFIX_BYTES = 512 * 1024
_MEDIA_TYPES = {0x01, 0x02, 0x03, 0x04, 0x06, 0x0F, 0x10, 0x11, 0x1B, 0x24, 0x42, 0x81, 0x87}


def _unwrap(value, reference):
    if reference is None:
        return value
    return value + ((int(reference) - value + WRAP // 2) // WRAP) * WRAP


def _timestamp(data):
    if len(data) < 5 or not all(data[index] & 1 for index in (0, 2, 4)):
        return None
    return (((data[0] >> 1) & 7) << 30) | (data[1] << 22) | ((data[2] >> 1) << 15) | (data[3] << 7) | (data[4] >> 1)


def _put_timestamp(refs, value):
    value %= WRAP
    previous = refs[0][0][refs[0][1]]
    values = [
        (previous & 0xF1) | (((value >> 30) & 7) << 1),
        (value >> 22) & 0xFF, ((value >> 14) & 0xFE) | 1,
        (value >> 7) & 0xFF, ((value << 1) & 0xFE) | 1,
    ]
    for (packet, offset), byte in zip(refs, values):
        packet[offset] = byte


def _packet_info(packet):
    if len(packet) != PACKET_SIZE or packet[0] != 0x47:
        raise ValueError('录制数据不是完整的 MPEG-TS 数据包')
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    control = (packet[3] >> 4) & 3
    if control == 0:
        raise ValueError('录制数据包含无效的 MPEG-TS 包头')
    payload = 4
    clocks = []
    if control & 2:
        length = packet[4]
        if 5 + length > PACKET_SIZE:
            raise ValueError('录制数据包含无效的 MPEG-TS 适配字段')
        payload = 5 + length
        if length:
            position = 6
            for flag in (0x10, 0x08):
                if packet[5] & flag:
                    if position + 6 > payload:
                        raise ValueError('录制数据包含不完整的 MPEG-TS 时钟')
                    base = (packet[position] << 25) | (packet[position + 1] << 17) | (packet[position + 2] << 9) | (packet[position + 3] << 1) | (packet[position + 4] >> 7)
                    clocks.append((position, base))
                    position += 6
    return pid, (payload if control & 1 and payload < PACKET_SIZE else None), clocks


class _PesHeaders:
    """Locate PES timestamp bytes even when a header spans transport packets."""
    def __init__(self):
        self.pending = {}

    def feed(self, packet, callback):
        pid, payload, _ = _packet_info(packet)
        if payload is None or packet[1] & 0x80 or packet[3] & 0xC0:
            return
        if packet[1] & 0x40:
            self.pending[pid] = []
        refs = self.pending.get(pid)
        if refs is None:
            return
        refs.extend((packet, index) for index in range(payload, min(PACKET_SIZE, payload + 19 - len(refs))))
        header = bytes(part[index] for part, index in refs)
        if len(header) >= 3 and header[:3] != b'\x00\x00\x01':
            self.pending.pop(pid, None)
            return
        if len(header) < 9:
            return
        flags = header[7] & 0xC0
        needed = 19 if flags == 0xC0 else 14 if flags == 0x80 else 0
        if header[6] & 0xC0 != 0x80 or not needed or header[8] < needed - 9:
            self.pending.pop(pid, None)
            return
        if len(header) < needed:
            return
        pts = _timestamp(header[9:14])
        dts = _timestamp(header[14:19]) if flags == 0xC0 else None
        if pts is not None:
            callback(pid, pts, dts, refs[9:14], refs[14:19] if dts is not None else [])
        self.pending.pop(pid, None)

    def blocked_ids(self):
        return {id(packet) for refs in self.pending.values() for packet, _ in refs}


class _ProgramMap:
    def __init__(self):
        self.pmt_pids = set()
        self.expected = set()
        self.sections = {}

    def feed(self, packet):
        pid, payload, _ = _packet_info(packet)
        if payload is None or pid != 0 and pid not in self.pmt_pids:
            return
        data = packet[payload:]
        if packet[1] & 0x40:
            if not data or data[0] + 1 > len(data):
                return
            data = data[1 + data[0]:]
            self.sections[pid] = bytearray()
        section = self.sections.get(pid)
        if section is None:
            return
        section.extend(data)
        if len(section) < 3:
            return
        length = 3 + ((section[1] & 0x0F) << 8) + section[2]
        if length > 1024:
            self.sections.pop(pid, None)
            return
        if len(section) < length:
            return
        self.sections.pop(pid, None)
        section = section[:length]
        if len(section) < 8 or not section[5] & 1:
            return
        if pid == 0 and section[0] == 0:
            for position in range(8, length - 4, 4):
                if position + 4 <= length - 4 and section[position:position + 2] != b'\x00\x00':
                    self.pmt_pids.add(((section[position + 2] & 0x1F) << 8) | section[position + 3])
        elif section[0] == 2 and length >= 16:
            position = 12 + (((section[10] & 0x0F) << 8) | section[11])
            while position + 5 <= length - 4:
                stream_type = section[position]
                stream_pid = ((section[position + 1] & 0x1F) << 8) | section[position + 2]
                if stream_type in _MEDIA_TYPES:
                    self.expected.add(stream_pid)
                position += 5 + (((section[position + 3] & 0x0F) << 8) | section[position + 4])


class _Timeline:
    def __init__(self, reference=None):
        self.reference = reference
        self.maximum = None
        self.minimum = None
        self.decode_minimum = None
        self.presentation_minimum = None
        self.last_decode = {}
        self.intervals = {}
        self.seen = set()
        self.counters = {}
        self.headers = _PesHeaders()

    def _observe(self, value):
        value = _unwrap(value, self.reference)
        self.reference = value if self.reference is None else max(value, self.reference)
        self.maximum = value if self.maximum is None else max(value, self.maximum)
        self.minimum = value if self.minimum is None else min(value, self.minimum)
        return value

    def _pes(self, pid, pts, dts, _pts_refs, _dts_refs):
        presentation = self._observe(pts)
        self.presentation_minimum = presentation if self.presentation_minimum is None else min(presentation, self.presentation_minimum)
        decode = self._observe(dts) if dts is not None else presentation
        self.decode_minimum = decode if self.decode_minimum is None else min(decode, self.decode_minimum)
        previous = self.last_decode.get(pid)
        if previous is not None and 0 < decode - previous <= CLOCK * 10:
            self.intervals.setdefault(pid, deque(maxlen=32)).append(decode - previous)
        self.last_decode[pid] = decode
        self.seen.add(pid)

    def feed(self, packet):
        pid, _payload, clocks = _packet_info(packet)
        self.counters[pid] = packet[3] & 15
        for _position, value in clocks:
            value = self._observe(value)
            self.decode_minimum = value if self.decode_minimum is None else min(value, self.decode_minimum)
        self.headers.feed(packet, self._pes)

    def snapshot(self):
        steps = [int(median(values)) for values in self.intervals.values() if values]
        step = max(1, min(steps)) if steps else CLOCK // 25
        maximum = self.maximum
        return dict(end_timestamp=maximum, end_seconds=maximum / CLOCK if maximum is not None else 0,
                    next_timestamp=maximum + step if maximum is not None else None,
                    step_ticks=step, counters=dict(self.counters), origin_timestamp=self.presentation_minimum)


def transport_tail(source, reference_timestamp=None, max_bytes=TAIL_BYTES):
    """Inspect at most ``max_bytes`` from a file tail, preserving its position.

    ``reference_timestamp`` is the last persisted unwrapped 90 kHz clock, when
    available. It disambiguates files longer than the 26.5 hour TS clock wrap.
    Identity/ownership verification belongs to the caller before opening files.
    """
    owned = not hasattr(source, 'read')
    handle = open(Path(source), 'rb') if owned else source
    if not owned and not handle.readable():
        return transport_tail(handle.name, reference_timestamp, max_bytes)
    position = handle.tell()
    try:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size % PACKET_SIZE:
            raise ValueError('已有录像的 MPEG-TS 数据包不完整，已保留原文件')
        start = max(0, size - max(PACKET_SIZE, int(max_bytes)))
        start += (-start) % PACKET_SIZE
        handle.seek(start)
        timeline = _Timeline(reference_timestamp)
        while True:
            packet = handle.read(PACKET_SIZE)
            if not packet:
                break
            timeline.feed(bytearray(packet))
        result = timeline.snapshot()
        result['size_bytes'] = size
        # The file's first presentation clock is stable across append attempts.
        # The bounded head read avoids treating a tail-window start as the origin.
        if size:
            handle.seek(0)
            head = _Timeline()
            for _index in range(min(size, 64 * 1024) // PACKET_SIZE):
                head.feed(bytearray(handle.read(PACKET_SIZE)))
            result['origin_timestamp'] = head.presentation_minimum
        return result
    finally:
        if owned:
            handle.close()
        else:
            handle.seek(position)


class TransportStreamWriter:
    """Write one FFmpeg connection into an existing seekable binary handle.

    Open the handle with ``xb`` for a new file or ``r+b``/``a+b`` for a verified
    existing file. One writer/process may own it at a time. ``on_write(count)``
    runs after bytes reach the handle; exceptions propagate to the caller.
    ``finish()`` returns the timeline snapshot for persistence and closes no file.
    """
    def __init__(self, handle, resume=False, on_write=None, reference_timestamp=None):
        self.handle = handle
        self.on_write = on_write
        self.bytes_written = 0
        self.discarded_bytes = 0
        self._raw = bytearray()
        self._prefix = []
        self._queue = deque()
        self._program = _ProgramMap()
        self._prefix_timeline = _Timeline()
        self._headers = _PesHeaders()
        self._finished = False
        tail = transport_tail(handle, reference_timestamp) if resume else {}
        self._target = tail.get('next_timestamp')
        self._origin_timestamp = tail.get('origin_timestamp')
        self._counters = dict(tail.get('counters', {}))
        self._timeline = _Timeline(tail.get('end_timestamp'))
        self._timeline.maximum = tail.get('end_timestamp')
        self._delta = None if self._target is not None else 0
        self.handle.seek(0, os.SEEK_END)

    @property
    def reference_timestamp(self):
        return self._timeline.maximum if self._timeline.maximum is not None else self._timeline.reference

    @property
    def duration_seconds(self):
        origin = self._origin_timestamp
        if origin is None:
            origin = self._timeline.presentation_minimum
        snapshot = self._timeline.snapshot()
        end = snapshot['next_timestamp']
        return max(0, (end - origin) / CLOCK) if origin is not None and end is not None else 0

    def _shift_pes(self, _pid, pts, dts, pts_refs, dts_refs):
        _put_timestamp(pts_refs, pts + self._delta)
        if dts is not None:
            _put_timestamp(dts_refs, dts + self._delta)

    def _rewrite(self, packet):
        pid, payload, clocks = _packet_info(packet)
        if self._target is not None:
            for position, base in clocks:
                shifted = (base + self._delta) % WRAP
                packet[position:position + 4] = bytes((shifted >> 25, (shifted >> 17) & 255,
                                                       (shifted >> 9) & 255, (shifted >> 1) & 255))
                packet[position + 4] = (packet[position + 4] & 0x7F) | ((shifted & 1) << 7)
            if pid in self._counters:
                counter = (self._counters[pid] + (payload is not None)) & 15
                packet[3] = (packet[3] & 0xF0) | counter
            self._counters[pid] = packet[3] & 15
            self._headers.feed(packet, self._shift_pes)
        self._queue.append(packet)

    def _flush_queue(self):
        blocked = self._headers.blocked_ids()
        # A corrupt/incomplete PES header cannot pin unlimited video in memory.
        if blocked and len(self._queue) * PACKET_SIZE > 64 * 1024:
            raise ValueError('重新连接的媒体时间戳头持续不完整，已保留原录像')
        data = bytearray()
        while self._queue and id(self._queue[0]) not in blocked:
            packet = self._queue.popleft()
            self._timeline.feed(packet)
            data.extend(packet)
        if not data:
            return
        view = memoryview(data)
        while view:
            written = self.handle.write(view)
            if written is None or written <= 0:
                raise OSError('录制文件未能继续写入')
            view = view[written:]
        self.handle.flush()
        self.bytes_written += len(data)
        if self.on_write:
            self.on_write(len(data))

    def _start_append(self):
        minimum = self._prefix_timeline.decode_minimum
        if minimum is None:
            raise ValueError('重新连接的媒体缺少有效时间戳，已保留原录像')
        self._delta = self._target - minimum
        for packet in self._prefix:
            self._rewrite(packet)
        self._prefix.clear()
        self._flush_queue()

    def write(self, data):
        if self._finished:
            raise ValueError('录制写入器已经结束')
        self._raw.extend(data)
        count = len(self._raw) // PACKET_SIZE
        for index in range(count):
            packet = bytearray(self._raw[index * PACKET_SIZE:(index + 1) * PACKET_SIZE])
            _packet_info(packet)
            if self._delta is None:
                self._prefix.append(packet)
                self._program.feed(packet)
                self._prefix_timeline.feed(packet)
                if (self._program.expected and self._program.expected <= self._prefix_timeline.seen
                        or len(self._prefix) * PACKET_SIZE >= PREFIX_BYTES):
                    self._start_append()
            else:
                self._rewrite(packet)
        if count:
            del self._raw[:count * PACKET_SIZE]
        self._flush_queue()

    def finish(self):
        if not self._finished:
            if self._prefix:
                self._start_append()
            self._flush_queue()
            # Never append an unresolved timestamp header with the old clock.
            # A connection ending mid-header loses only its unwritten tail.
            self.discarded_bytes = len(self._raw) + len(self._queue) * PACKET_SIZE
            self._queue.clear()
            self._headers.pending.clear()
            self._raw.clear()
            self._finished = True
        result = self._timeline.snapshot()
        result.update(bytes_written=self.bytes_written, discarded_bytes=self.discarded_bytes,
                      duration_seconds=self.duration_seconds, origin_timestamp=self._origin_timestamp
                      if self._origin_timestamp is not None else self._timeline.presentation_minimum)
        return result
