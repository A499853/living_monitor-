"""Offline behavior checks for delayed broadcast start-time enrichment.

Held futures represent slow platform responses without network access or sleeps.
The real Engine dispatch, completion callbacks and SQLite persistence are used.
"""
from __future__ import annotations

import concurrent.futures
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "app"))

from livewatch.engine import Engine
from livewatch.providers import ProbeResult
from livewatch.storage import Store


START = "2026-11-20T10:00:00+08:00"
SOURCE = "douyin.room.start_time"


class HeldPool:
    """A controllable external service boundary; unfinished requests stay held."""
    def __init__(self):
        self.calls = []

    def submit(self, function, room, settings):
        future = concurrent.futures.Future()
        self.calls.append(SimpleNamespace(room=room, settings=settings, future=future))
        return future

    def shutdown(self, wait=True, *, cancel_futures=True):
        for call in self.calls:
            if not call.future.done():
                call.future.cancel()


class Provider:
    requires_auth = False
    adaptive_polling = True
    offline_confirmations = 2

    def check(self, room, settings):
        raise AssertionError("Offline pool must hold requests instead of using a network")


class MetadataChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="livewatch-start-metadata-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.clock = SimpleNamespace(now=100.0)
        self.clock.monotonic = lambda: self.clock.now
        self.clock_patch = patch("livewatch.engine.time", self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.engine = None
        self.addCleanup(lambda: self.engine and self.engine.close())

    def configure(self, count=1, concurrency=32, rate=100):
        self.store.update_settings({
            "status_only_mode": True, "status_poll_interval_seconds": 8,
            "status_check_concurrency": concurrency, "status_douyin_concurrency": concurrency,
            "monitor_max_requests_per_second": rate, "monitor_process_isolation": False,
            "sound_enabled": False, "min_free_disk_gib": 0,
        })
        self.rooms = [self.store.add_room({
            "platform": "douyin", "room_input": str(123450 + index),
            "nickname": "room-" + str(index), "monitor_enabled": True,
            "record_enabled": False,
        }) for index in range(count)]
        self.store.reorder_rooms([room["id"] for room in self.rooms])
        self.engine = Engine(self.store, providers=Provider())
        self.engine.pool.shutdown()
        self.engine.metadata_pool.shutdown()
        self.status = HeldPool()
        self.metadata = HeldPool()
        self.engine.pool = self.status
        self.engine.metadata_pool = self.metadata

    def live(self, index=0, sid=None, actual="", source=""):
        return ProbeResult(True, live_session_id=sid or str(700000 + index),
                           live_started_at=actual, started_at_source=source)

    def first_wave(self, results=None):
        self.engine._tick()
        results = results or [self.live(i) for i in range(len(self.rooms))]
        for call, result in zip(self.status.calls, results):
            call.future.set_result(result)

    def collect(self):
        self.engine._tick()
        self.assertTrue(self.metadata.calls, "Expected a delayed start-time lookup")
        return self.metadata.calls[-1]

    def details(self, call, actual=START, source=SOURCE, **extra):
        return ProbeResult(True, live_session_id=call.settings["_metadata_session_id"],
                           live_started_at=actual, started_at_source=source, **extra)

    def state(self, index=0):
        return self.engine.states[self.rooms[index]["id"]]

    def test_slow_metadata_does_not_hold_status_workers_or_change_cadence(self):
        self.configure(count=3, concurrency=3)
        self.first_wave()
        first = self.collect()
        self.assertTrue(first.settings["_metadata_only"])
        self.assertFalse(first.settings["_status_only"])
        self.clock.now += 1
        self.engine._tick()
        self.assertEqual(len(self.metadata.calls), 2)
        self.clock.now += 7
        self.engine._tick()
        self.assertEqual(len(self.status.calls), 6)
        self.assertEqual(len(self.metadata.calls), 2)
        self.assertEqual(len(self.engine.metadata_pending), 2)
        self.assertTrue(all(not call.future.done() for call in self.metadata.calls))
        for call in self.status.calls[3:]:
            call.future.set_result(self.live(self.rooms.index(next(r for r in self.rooms if r["id"] == call.room["id"]))))
        self.assertEqual(self.engine.snapshot()["monitor"]["fresh_rooms"], 3)

    def test_success_updates_same_session_runtime_and_csv_without_status_side_effects(self):
        self.configure()
        self.first_wave()
        call = self.collect()
        state = self.state()
        state.update(record_status="recording", duration_seconds=123,
                     record_path="saved.flv", resolved_stream_url="https://media.invalid/current.flv")
        preserved = {key: state.get(key) for key in (
            "confirmed_live", "live_status", "last_checked", "last_success_at",
            "_last_success_monotonic", "last_attempt_at", "last_probe_latency_seconds",
            "record_status", "duration_seconds", "record_path", "resolved_stream_url",
            "record_headers", "stream_variants", "observed_started_at")}
        before = self.store.get_open_session(self.rooms[0]["id"])
        call.future.set_result(self.details(call, stream_url="https://media.invalid/other.flv",
                                            headers={"Authorization": "unused"},
                                            avatar_url="https://images.invalid/avatar.jpg"))
        expected = self.engine._actual_time(START)
        self.assertEqual(self.state()["live_started_at"], expected)
        self.assertEqual({key: self.state().get(key) for key in preserved}, preserved)
        after = self.store.get_open_session(self.rooms[0]["id"])
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after["actual_started_at"], expected)
        self.assertEqual(self.store.get_runtime(self.rooms[0]["id"])["live_started_at"], expected)
        self.assertTrue(any("metadata_updated" in path.read_text("utf-8-sig")
                            for path in Path(self.directory.name).rglob("events.csv")))
        self.assertEqual(sum(event["kind"] == "live_started" for event in self.store.list_events()), 1)
        self.assertFalse(any(event["kind"] == "live_ended" for event in self.store.list_events()))

    def test_empty_or_failed_lookup_retries_after_completion_without_damaging_status(self):
        for failure in (ProbeResult(None, error_code="metadata_pending"), OSError("temporary outage")):
            with self.subTest(failure=type(failure).__name__):
                if self.engine:
                    self.engine.close()
                # Each subcase owns a fresh database room to avoid cached state.
                self.store.db.execute("DELETE FROM rooms")
                self.store.db.commit()
                self.configure()
                self.first_wave()
                call = self.collect()
                self.clock.now += 4
                if isinstance(failure, Exception):
                    call.future.set_exception(failure)
                else:
                    call.future.set_result(failure)
                self.assertEqual(self.state()["live_status"], "live")
                self.assertEqual(self.state()["last_error"], "")
                self.assertEqual(self.state()["failure_count"], 0)
                self.clock.now += 59
                self.engine._tick()
                self.assertEqual(len(self.metadata.calls), 1)
                # Finish the due status check, proving its result stays independent.
                self.status.calls[-1].future.set_result(self.live())
                self.clock.now += 1
                self.engine._tick()
                self.assertEqual(len(self.metadata.calls), 2)

    def test_late_result_is_discarded_after_room_or_session_changes(self):
        changes = ("new_session", "offline", "deleted", "changed_source", "disabled", "reenabled", "auth_changed")
        for change in changes:
            with self.subTest(change=change):
                if self.engine:
                    self.engine.close()
                self.store.db.execute("DELETE FROM rooms")
                self.store.db.commit()
                self.configure()
                self.first_wave()
                call = self.collect()
                rid = self.rooms[0]["id"]
                if change in ("new_session", "offline"):
                    self.engine.refresh([rid])
                    self.engine._tick()
                    self.status.calls[-1].future.set_result(self.live(sid="800001") if change == "new_session" else ProbeResult(False))
                    if change == "offline":
                        self.clock.now += 1
                        self.engine._tick()
                        self.status.calls[-1].future.set_result(ProbeResult(False))
                elif change == "deleted":
                    self.store.delete_room(rid)
                    self.engine.room_changed(rid)
                elif change == "changed_source":
                    self.store.update_room(rid, {"room_input": "999999"})
                    self.engine.room_changed(rid)
                elif change in ("disabled", "reenabled"):
                    self.store.update_room(rid, {"monitor_enabled": False})
                    self.engine.room_changed(rid)
                    if change == "reenabled":
                        self.store.update_room(rid, {"monitor_enabled": True})
                        self.engine.room_changed(rid)
                else:
                    self.engine.auth_changed()
                call.future.set_result(self.details(call))
                self.assertNotIn(rid, self.engine.metadata_pending)
                if change == "deleted":
                    self.assertNotIn(rid, self.engine.states)
                    self.assertEqual(self.store.get_runtime(rid), {})
                else:
                    self.assertEqual(self.state()["live_started_at"], "")
                self.assertFalse(any(event["kind"] == "live_start_metadata"
                                     for event in self.store.list_events()))

    def test_exact_time_skips_lookup_and_new_session_is_eligible(self):
        self.configure()
        self.first_wave([self.live(actual=START, source=SOURCE)])
        self.engine._tick()
        self.assertEqual(self.metadata.calls, [])
        rid = self.rooms[0]["id"]
        old_session = self.store.get_open_session(rid)["id"]
        self.engine.refresh([rid])
        self.engine._tick()
        self.status.calls[-1].future.set_result(self.live(sid="800002"))
        call = self.collect()
        if call.settings.get('_metadata_ended'):
            call.future.set_result(ProbeResult(None,error_code='metadata_pending'))
            self.clock.now += 1
            self.engine._tick()
            call = self.metadata.calls[-1]
        self.assertEqual(call.settings["_metadata_session_id"], "800002")
        self.assertNotEqual(self.store.get_open_session(rid)["id"], old_session)

    def test_estimate_can_be_upgraded_but_creation_and_mismatched_time_are_rejected(self):
        self.configure(count=3)
        self.first_wave([self.live(i, actual=START, source="douyin.live_duration_seconds(推算)") for i in range(3)])
        call = self.collect()
        first_id = call.room["id"]
        call.future.set_result(self.details(call))
        self.assertEqual(self.engine.states[first_id]["started_at_source"], SOURCE)
        self.clock.now += 1
        self.engine._tick()
        call = self.metadata.calls[-1]
        call.future.set_result(self.details(call, source="douyin.room.create_time"))
        self.assertIn("推算", self.engine.states[call.room["id"]]["started_at_source"])
        self.clock.now += 1
        self.engine._tick()
        call = self.metadata.calls[-1]
        call.future.set_result(ProbeResult(True, live_session_id="99999999", live_started_at=START, started_at_source=SOURCE))
        self.assertIn("推算", self.engine.states[call.room["id"]]["started_at_source"])

    def test_metadata_endpoint_cooldown_does_not_pause_status_checks(self):
        self.configure(count=2)
        self.first_wave()
        call = self.collect()
        call.future.set_result(ProbeResult(None,error_code='rate_limited',retry_after=600))
        self.clock.now += 61
        self.engine._tick()
        self.assertEqual(len(self.status.calls),4)
        self.assertEqual(len(self.metadata.calls),1)
        self.assertGreater(self.engine.metadata_blocked_until,self.clock.now)
        self.assertEqual(self.engine.douyin_blocked_until,0)

    def test_metadata_shares_rate_limit_and_status_dispatches_first(self):
        self.configure(count=2, rate=2)
        self.first_wave()
        self.engine._tick()
        self.assertEqual(self.metadata.calls, [])
        self.clock.now += 1
        first = self.collect()
        self.assertEqual(len(self.engine.rate_window), 1)
        first.future.set_result(self.details(first))
        self.clock.now += 7
        self.engine._tick()
        # Both due status checks consumed this second's budget before enrichment.
        self.assertEqual(len(self.status.calls), 4)
        self.assertEqual(len(self.metadata.calls), 1)
        self.assertEqual(len(self.engine.rate_window), 2)
        self.clock.now += 1
        self.engine._tick()
        self.assertEqual(len(self.metadata.calls), 2)
        self.assertEqual(len(self.engine.rate_window), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
