"""Deterministic offline checks for monitor scheduling and stale-result fences.

All probes are controlled with Events; no external network, credentials, media,
or user database is used. Run with tools\\python\\python.exe -B this-file.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "app"))

from livewatch.engine import Engine
from livewatch.providers import ProbeResult
from livewatch.storage import Store


@dataclass
class Call:
    room: dict
    settings: dict
    release: threading.Event = field(default_factory=threading.Event)
    result: ProbeResult = field(default_factory=lambda: ProbeResult(False))


class ControlledProvider:
    requires_auth = False
    adaptive_polling = True
    offline_confirmations = 2

    def __init__(self):
        self.condition = threading.Condition()
        self.calls = []
        self.active = {}
        self.peak_total = 0
        self.peak_official = 0
        self.peak_per_room = 0
        self.closed = False
        self.auth_resets = 0

    def check(self, room, settings):
        call = Call(dict(room), dict(settings))
        with self.condition:
            if self.closed:
                call.release.set()
            self.calls.append(call)
            self.active[room["id"]] = self.active.get(room["id"], 0) + 1
            self.peak_per_room = max(self.peak_per_room, self.active[room["id"]])
            self.peak_total = max(self.peak_total, sum(self.active.values()))
            official = sum(
                self.active.get(candidate["id"], 0)
                for candidate in {item.room["id"]: item.room for item in self.calls}.values()
                if candidate["platform"] == "douyin"
                and not candidate.get("status_url") and not candidate.get("stream_url")
            )
            self.peak_official = max(self.peak_official, official)
            self.condition.notify_all()
        try:
            if not call.release.wait(5):
                raise AssertionError("Test did not release a controlled probe")
            return call.result
        finally:
            with self.condition:
                self.active[room["id"]] -= 1
                self.condition.notify_all()

    def wait_call(self, name, number=1, timeout=2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                matching = [call for call in self.calls if call.room["nickname"] == name]
                if len(matching) >= number:
                    return matching[number - 1]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Probe {name!r} #{number} did not start")
                self.condition.wait(remaining)

    def names(self):
        with self.condition:
            return [call.room["nickname"] for call in self.calls]

    def invalidate_auth(self, _settings):
        self.auth_resets += 1

    def release_all(self):
        with self.condition:
            self.closed = True
            for call in self.calls:
                call.release.set()


class NoRecorder:
    def status(self, _rid):
        return None

    def available(self, _settings):
        return True

    def close(self):
        return []


class MonitorConcurrencyChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="livewatch-concurrency-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "隔离测试 with spaces")
        self.addCleanup(self.store.close)
        self.provider = ControlledProvider()
        self.engine = None
        self.addCleanup(self.close_engine)
        # Expected synthetic 429 events are asserted through the store; avoid
        # writing their Chinese messages to legacy Windows console encodings.
        self.previous_logging_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, self.previous_logging_level)

    def close_engine(self):
        self.provider.release_all()
        if self.engine is not None:
            self.engine.close()

    def configure(self, names, *, official=(), mode="independent", concurrency=2, cap=2):
        self.store.update_settings({
            "monitor_schedule_mode": mode, "monitor_batch_size": concurrency,
            "monitor_douyin_concurrency": cap, "room_check_delay_seconds": 0,
            "monitor_interval_minutes": 0.1, "sound_enabled": False,
            "min_free_disk_gib": 0,
            # These tests exercise the legacy metadata scheduler invariants.
            # The production high-frequency profile is covered separately by
            # verify-high-frequency-monitor.py.
            "status_only_mode": False,
        })
        rooms = []
        for name in names:
            payload = {"platform": "douyin", "room_input": "test_" + name,
                       "nickname": name, "monitor_enabled": True, "record_enabled": False}
            if name not in official:
                payload["status_url"] = "http://127.0.0.1:9/" + name
            rooms.append(self.store.add_room(payload))
        self.store.reorder_rooms([room["id"] for room in rooms])
        self.rooms = {room["nickname"]: room for room in rooms}
        self.engine = Engine(self.store, providers=self.provider, recorder=NoRecorder())

    def await_state(self, predicate, message="Monitor callback did not complete"):
        deadline = time.monotonic() + 2
        while True:
            with self.engine.lock:
                if predicate():
                    return
                self.engine.wake.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(message)
            self.engine.wake.wait(min(remaining, 0.05))

    def finish(self, name, result=None, number=1):
        call = self.provider.wait_call(name, number)
        if result is not None:
            call.result = result
        call.release.set()
        rid = call.room["id"]
        self.await_state(lambda: rid not in self.engine.pending)
        return call

    def test_independent_slot_fills_before_slow_room_finishes(self):
        self.configure(["slow", "fast", "next"])
        self.engine._tick()
        slow = self.provider.wait_call("slow")
        self.provider.wait_call("fast")
        self.finish("fast")
        self.engine._tick()
        self.provider.wait_call("next")
        self.assertFalse(slow.release.is_set())
        self.assertIn(self.rooms["slow"]["id"], self.engine.pending)
        self.finish("next")
        self.finish("slow")
        self.assertEqual(self.provider.peak_total, 2)
        self.assertEqual(self.provider.peak_per_room, 1)
        self.assertFalse(self.engine.round_active)
        # A finished round still observes the configured round interval.
        self.engine._tick()
        self.assertEqual(self.provider.names(), ["slow", "fast", "next"])

    def test_batch_mode_preserves_completion_barrier(self):
        self.configure(["slow", "fast", "next"], mode="batch")
        self.engine._tick()
        self.provider.wait_call("slow")
        self.finish("fast")
        self.engine._tick()
        self.assertNotIn("next", self.provider.names())
        self.finish("slow")
        self.engine._tick()
        self.finish("next")
        self.assertEqual(self.provider.peak_total, 2)

    def test_recovering_room_does_not_hold_healthy_room_queue(self):
        self.configure(["flaky", "first", "next"])
        rid = self.rooms["flaky"]["id"]
        self.engine.states[rid].update(confirmed_live=True, live_status="live")
        self.engine._tick()
        self.finish("flaky", ProbeResult(None, error_code="network_error", message="连接中断"))
        self.finish("first")
        with self.engine.lock:
            # Keep the recovery in a waiting window without wall-clock sleeps.
            self.engine.recoveries[rid]["next_at"] = time.monotonic() + 5
        self.engine._tick()
        self.provider.wait_call("next")
        self.assertTrue(self.engine.states[rid]["confirmed_live"])
        self.assertEqual(self.engine.states[rid]["live_status"], "live")
        self.assertTrue(self.engine.round_active)
        self.finish("next")
        with self.engine.lock:
            self.engine.recoveries[rid]["next_at"] = 0
        self.engine._tick()
        self.finish("flaky", ProbeResult(True), number=2)
        self.assertNotIn(rid, self.engine.recoveries)
        self.assertFalse(any(event["kind"] == "live_ended" for event in self.store.list_events()))
        self.assertEqual(self.provider.peak_per_room, 1)

    def test_official_limit_leaves_capacity_for_custom_sources(self):
        self.configure(["d0", "d1", "d2", "d3", "custom"],
                       official={"d0", "d1", "d2", "d3"}, concurrency=4, cap=2)
        self.engine._tick()
        self.provider.wait_call("d0")
        self.provider.wait_call("d1")
        self.provider.wait_call("custom")
        self.assertNotIn("d2", self.provider.names())
        self.assertNotIn("d3", self.provider.names())
        self.finish("d0")
        self.engine._tick()
        self.provider.wait_call("d2")
        self.finish("d1")
        self.engine._tick()
        self.provider.wait_call("d3")
        self.finish("d2")
        self.finish("d3")
        self.finish("custom")
        self.assertEqual(self.provider.peak_official, 2)
        self.assertLessEqual(self.provider.peak_total, 4)
        self.assertEqual(set(self.provider.names()), {"d0", "d1", "d2", "d3", "custom"})

    def test_repeated_refresh_never_duplicates_inflight_room(self):
        self.configure(["single"], concurrency=4)
        self.engine._tick()
        self.provider.wait_call("single")
        for _ in range(5):
            self.engine.refresh()
            self.engine._tick()
        self.assertEqual(self.provider.names(), ["single"])
        self.assertEqual(self.provider.peak_per_room, 1)
        self.finish("single")

    def test_independent_dispatch_spacing_and_settings_change(self):
        self.configure(["first", "second", "third"], concurrency=3)
        self.store.update_settings({"room_check_delay_seconds": 60})
        self.engine._tick()
        self.provider.wait_call("first")
        for _ in range(3):
            self.engine._tick()
        self.assertEqual(self.provider.names(), ["first"])
        # Editing the delay affects a current dispatch wait immediately; a
        # slow first request need not finish before the other slots can fill.
        self.store.update_settings({"room_check_delay_seconds": 0})
        self.engine.settings_changed()
        self.engine._tick()
        self.provider.wait_call("second")
        self.provider.wait_call("third")
        self.assertEqual(self.provider.peak_total, 3)
        for name in ("first", "second", "third"):
            self.finish(name)

    def test_lowering_concurrency_does_not_submit_until_old_work_drains(self):
        self.configure(["a", "b", "c", "d"], concurrency=3)
        self.engine._tick()
        for name in ("a", "b", "c"):
            self.provider.wait_call(name)
        self.store.update_settings({"monitor_batch_size": 1})
        self.engine.settings_changed()
        for completed in (None, "a", "b"):
            if completed is not None:
                self.finish(completed)
            self.engine._tick()
            self.assertNotIn("d", self.provider.names())
        self.finish("c")
        self.engine._tick()
        self.finish("d")
        self.assertEqual(self.provider.peak_total, 3)

    def test_round_cannot_restart_while_final_probe_is_pending(self):
        self.configure(["slow", "fast"])
        self.engine._tick()
        self.provider.wait_call("slow")
        self.finish("fast")
        first_round = self.engine.round_number
        with self.engine.lock:
            # Even an old prior-round timestamp cannot release the current
            # round barrier while its last real probe still owns a slot.
            self.engine.round_finished_at = time.monotonic() - 100
        for _ in range(4):
            self.engine._tick()
        self.assertTrue(self.engine.round_active)
        self.assertEqual(self.engine.round_number, first_round)
        self.assertEqual(self.provider.names(), ["slow", "fast"])
        self.finish("slow")
        self.assertFalse(self.engine.round_active)

    def test_shared_backoff_preserves_custom_work_and_half_open_single_probe(self):
        self.configure(["a", "b", "c", "custom"], official={"a", "b", "c"},
                       concurrency=4, cap=3)
        self.engine._tick()
        self.provider.wait_call("custom")
        for name in ("a", "b", "c"):
            self.finish(name, ProbeResult(None, message="访问受限", error_code="rate_limited",
                                          retry_after=60, diagnostic="http=429 retry_after=60"))
        self.assertGreater(self.engine.douyin_blocked_until, time.monotonic())
        self.assertFalse(self.engine.recoveries, "Explicit rate limits must not enter fast retries")
        official_ids = [self.rooms[name]["id"] for name in ("a", "b", "c")]
        self.engine.refresh(official_ids)
        self.engine._tick()
        self.assertEqual(len(self.provider.names()), 4)

        # Expire only the platform/room cooldown clocks under controlled test
        # conditions. Do not wait a real minute or change production clocks.
        with self.engine.lock:
            self.engine.douyin_blocked_until = time.monotonic() - 1
            for rid in official_ids:
                self.engine.due[rid] = 0
        self.engine.refresh(official_ids)
        self.engine._tick()
        self.provider.wait_call("a", 2)
        self.engine._tick()
        self.assertEqual(len(self.provider.names()), 5, "Half-open must issue only one official probe")
        self.assertIn(self.rooms["custom"]["id"], self.engine.pending)
        self.finish("a", ProbeResult(False), number=2)
        self.assertGreater(self.engine.douyin_blocked_until, time.monotonic())
        self.assertEqual(self.engine.douyin_recovery_successes, 1)

        with self.engine.lock:
            self.engine.douyin_blocked_until = time.monotonic() - 1
        self.engine.refresh([self.rooms["b"]["id"], self.rooms["c"]["id"]])
        self.engine._tick()
        self.provider.wait_call("b", 2)
        self.assertEqual(len(self.provider.names()), 6)
        self.finish("b", ProbeResult(False), number=2)
        self.assertEqual(self.engine.douyin_blocked_until, 0)
        self.engine._tick()
        self.finish("c", ProbeResult(False), number=2)
        self.finish("custom")

    def test_fast_failing_recoveries_share_dispatch_time_with_healthy_queue(self):
        self.configure(["r0", "r1", "held", "healthy"], concurrency=3)
        self.engine._tick()
        self.provider.wait_call("held")
        for name in ("r0", "r1"):
            self.finish(name, ProbeResult(None, message="短暂失败", error_code="network_error"))
        self.store.update_settings({"room_check_delay_seconds": 1})
        clock = time.monotonic() + 2
        with self.engine.lock:
            self.engine.last_probe_submitted_at = clock - 2
            for rescue in self.engine.recoveries.values():
                rescue["next_at"] = 0
        # Patch only scheduler invocations; condition waits retain a real clock
        # and failures therefore still have a bounded wall-clock timeout.
        with patch("livewatch.engine.time.monotonic", return_value=clock):
            self.engine._tick()
        self.provider.wait_call("r0", 2)
        self.finish("r0", ProbeResult(None, message="仍然失败", error_code="network_error"), number=2)
        with self.engine.lock:
            for rescue in self.engine.recoveries.values():
                rescue["next_at"] = 0
        with patch("livewatch.engine.time.monotonic", return_value=clock + 1.1):
            self.engine._tick()
        self.provider.wait_call("healthy")
        self.assertEqual(self.provider.names().count("r1"), 1,
                         "An immediate retry failure must not consume the next healthy dispatch")
        self.assertEqual(self.provider.peak_per_room, 1)
        self.finish("healthy")
        self.finish("held")

    def test_changed_source_discards_old_result_and_keeps_recheck(self):
        self.configure(["source"], concurrency=2)
        room = self.rooms["source"]
        rid = room["id"]
        self.engine._tick()
        old = self.provider.wait_call("source")
        self.store.update_room(rid, {"status_url": "http://127.0.0.1:9/new-source"})
        self.engine.room_changed(rid)
        self.engine._tick()
        self.assertEqual(len(self.provider.names()), 1)
        old.result = ProbeResult(True, nickname="STALE")
        old.release.set()
        self.await_state(lambda: rid not in self.engine.pending)
        self.assertNotEqual(self.store.get_room(rid)["nickname"], "STALE")
        self.assertIsNone(self.engine.states[rid]["confirmed_live"])
        self.engine._tick()
        new = self.provider.wait_call("source", number=2)
        self.assertEqual(new.room["status_url"], "http://127.0.0.1:9/new-source")
        self.finish("source", ProbeResult(False), number=2)
        self.assertEqual(self.engine.states[rid]["live_status"], "waiting")
        self.assertEqual(self.provider.peak_per_room, 1)

    def test_changed_authorization_discards_old_result(self):
        self.configure(["auth"], concurrency=2)
        rid = self.rooms["auth"]["id"]
        self.engine._tick()
        old = self.provider.wait_call("auth")
        self.engine.auth_changed()
        self.engine._tick()
        self.assertEqual(self.provider.names(), ["auth"])
        old.result = ProbeResult(True, nickname="STALE_AUTH")
        old.release.set()
        self.await_state(lambda: rid not in self.engine.pending)
        self.assertIsNone(self.engine.states[rid]["confirmed_live"])
        self.assertNotEqual(self.store.get_room(rid)["nickname"], "STALE_AUTH")
        self.engine._tick()
        self.finish("auth", ProbeResult(False), number=2)
        self.assertEqual(self.provider.auth_resets, 1)
        self.assertEqual(self.provider.peak_per_room, 1)

    def test_deleted_room_is_not_resurrected_by_late_result(self):
        self.configure(["deleted"])
        rid = self.rooms["deleted"]["id"]
        self.engine._tick()
        old = self.provider.wait_call("deleted")
        self.store.delete_room(rid)
        self.engine.room_changed(rid)
        old.result = ProbeResult(True)
        old.release.set()
        self.await_state(lambda: rid not in self.engine.pending)
        self.assertNotIn(rid, self.engine.states)
        self.assertEqual(self.store.list_rooms(), [])
        self.assertEqual(self.store.get_runtime(rid), {})

    def test_shutdown_suppresses_late_state_and_new_submissions(self):
        self.configure(["closing", "queued"], concurrency=1)
        self.engine._tick()
        old = self.provider.wait_call("closing")
        old.result = ProbeResult(True)
        closed = threading.Event()
        failure = []

        def close():
            try:
                self.engine.close()
            except BaseException as exc:
                failure.append(exc)
            finally:
                closed.set()

        thread = threading.Thread(target=close, name="test-engine-close")
        thread.start()
        self.assertTrue(self.engine.stop_event.wait(2))
        self.engine._tick()
        old.release.set()
        self.assertTrue(closed.wait(2), "Engine shutdown did not join released probes")
        thread.join(2)
        self.assertFalse(failure)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.provider.names(), ["closing"])
        rid = self.rooms["closing"]["id"]
        self.assertIsNone(self.engine.states[rid]["confirmed_live"])
        self.assertFalse(any(event["kind"] == "live_started" for event in self.store.list_events()))


def benchmark():
    """Compare healthy queue latency with the same 80 ms slow probe fixture."""
    class LatencyProvider:
        requires_auth = False
        adaptive_polling = True

        def __init__(self):
            self.lock = threading.Lock()
            self.finished = {}

        def check(self, room, _settings):
            if room["nickname"] == "slow":
                threading.Event().wait(0.08)
            with self.lock:
                self.finished[room["nickname"]] = time.perf_counter()
            return ProbeResult(False)

    results = {"fixture": "one 80 ms probe, five immediate healthy probes, concurrency 2, delay 0"}
    for mode in ("batch", "independent"):
        with tempfile.TemporaryDirectory(prefix="livewatch-scheduler-bench-") as directory:
            store = Store(directory)
            store.update_settings({"monitor_schedule_mode": mode, "monitor_batch_size": 2,
                                   "room_check_delay_seconds": 0, "sound_enabled": False,
                                   "min_free_disk_gib": 0})
            rooms = [store.add_room({"platform": "wechat", "room_input": name,
                                     "nickname": name, "status_url": "http://127.0.0.1:9/" + name})
                     for name in ("slow", "h0", "h1", "h2", "h3", "h4")]
            store.reorder_rooms([room["id"] for room in rooms])
            provider = LatencyProvider()
            engine = Engine(store, providers=provider, recorder=NoRecorder())
            try:
                started = time.perf_counter()
                deadline = time.monotonic() + 3
                while True:
                    engine.wake.clear()
                    engine._tick()
                    with provider.lock:
                        finished = provider.finished.get("h4")
                    if finished is not None:
                        results[mode + "_healthy_queue_ms"] = round((finished - started) * 1000, 2)
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("Offline scheduler benchmark timed out")
                    engine.wake.wait(0.002)
            finally:
                engine.close()
                store.close()
    print(json.dumps(results, ensure_ascii=True))


if __name__ == "__main__":
    if sys.argv[1:] == ["--benchmark"]:
        benchmark()
    else:
        unittest.main(verbosity=2)
