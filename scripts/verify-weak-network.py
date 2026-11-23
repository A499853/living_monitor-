"""Real HTTP -> ProviderRegistry -> supervised processes -> Engine acceptance.

All traffic stays on loopback. --duration 86400 runs the same fixture for a day;
that exercises resource lifetime, not a real platform's availability or quota.
"""
from __future__ import annotations

import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "app"))
from livewatch.engine import Engine
from livewatch.storage import Store
from livewatch.diagnostics import system_health


class Fixture(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def __init__(self, request_timeout=4):
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.live = False
        self.faults = False
        self.counts = Counter()
        self.active = 0
        self.peak_active = 0
        self.stop = threading.Event()
        self.hang_seconds=request_timeout+1

    def handle_error(self, request, client_address):
        # Killing a deadline worker may reset the connection before the HTTP
        # handler has read its first byte; that is intentional fault traffic.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        number = int(self.path.rsplit("/", 1)[1])
        with self.server.lock:
            self.server.counts[number] += 1
            self.server.active += 1
            self.server.peak_active = max(self.server.peak_active, self.server.active)
            faults, live = self.server.faults, self.server.live
        try:
            if faults and number < 3:
                # More than the real process deadline, including socket read.
                self.server.stop.wait(self.server.hang_seconds)
            elif faults and number < 6:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            elif faults and number == 6:
                self.send_response(429)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.server.stop.wait(0.03 + number % 5 * 0.01)
            body = json.dumps({"live": live}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ConnectionError):
            pass
        finally:
            with self.server.lock:
                self.server.active -= 1


def await_condition(engine, predicate, seconds, label):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = engine.snapshot()
        if predicate(state):
            return state
        engine.wake.wait(0.03)
        time.sleep(0.01)
    latest = engine.snapshot()
    raise AssertionError(label + ": " + json.dumps({
        "monitor": latest["monitor"],
        "room_statuses": dict(Counter(room["live_status"] for room in latest["rooms"])),
        "room_errors": dict(Counter(room.get("error_code", "") for room in latest["rooms"])),
    }, ensure_ascii=True))


def run(duration, report_path, request_timeout=4):
    logging.disable(logging.CRITICAL)
    server = Fixture(request_timeout)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {"fixture": "loopback HTTP with hangs, disconnects, 429 and recovery",
              "rooms": 150, "real_platform_test": False,
              "configured_interval_seconds": 8, "configured_timeout_seconds": request_timeout,
              "configured_request_budget_per_second": 20,
              "fault_hang_seconds":server.hang_seconds,
              "periodic_fault_start_seconds": 60, "periodic_fault_cycle_seconds": 60,
              "periodic_fault_duration_seconds": 8, "periodic_fault_windows": 0,
              "resource_scope": "test parent process; worker process count separately bounded"}
    began = time.monotonic()
    engine = store = None
    with tempfile.TemporaryDirectory(prefix="livewatch-weak-net-") as directory:
        try:
            store = Store(directory)
            store.update_settings({"status_only_mode": True, "status_poll_interval_seconds": 8,
                                   "monitor_max_requests_per_second":20,
                                   "status_request_timeout": request_timeout, "sound_enabled": False,
                                   "min_free_disk_gib": 0})
            added = store.add_rooms([{"platform": "wechat", "room_input": f"fixture-{i}",
                                      "record_enabled": False,
                                      "status_url": f"http://127.0.0.1:{server.server_port}/status/{i}"}
                                     for i in range(150)])
            assert added["added"] == 150
            by_number = {int(r["room_input"].split("-")[1]): r["id"] for r in store.list_rooms()}
            engine = Engine(store)
            engine.start()
            await_condition(engine, lambda s: all(r["room_status"] == "waiting" for r in s["rooms"]),
                            15, "150 real process probes did not warm up")
            report["cold_start_seconds"] = round(time.monotonic() - began, 3)
            switched = time.monotonic()
            with server.lock:
                server.live = True
            await_condition(engine, lambda s: all(r["room_status"] == "live" for r in s["rooms"]),
                            20, "150 healthy status transitions exceeded 20 seconds")
            report["healthy_transition_seconds"] = round(time.monotonic() - switched, 3)
            with server.lock:
                server.faults = True
                server.live = False
            healthy_ids = {rid for number, rid in by_number.items() if number >= 7}
            switched = time.monotonic()
            # Measure the confirmed transition itself. A room already proven
            # offline can become stale while other rooms spend the global
            # budget on confirmation; this must not undo that observation.
            state = await_condition(engine, lambda s: all(r["room_status"] == "waiting"
                                    for r in s["rooms"] if r["id"] in healthy_ids),
                                    20, "faulty rooms blocked confirmed offline transitions beyond 20 seconds")
            report["mixed_fault_transition_seconds"] = round(time.monotonic() - switched, 3)
            state = await_condition(engine, lambda s: s["monitor"].get("probe_process_timeouts", 0) > 0
                                    or s["monitor"].get("process_timeouts", 0) > 0
                                    or s["monitor"].get("probe_timeouts", 0) > 0,
                                    6, "deadline fixture did not produce a timeout")
            affected = {by_number[n] for n in range(7)}
            assert not any(event["kind"] == "live_ended" and event["room_id"] in affected
                           for event in store.list_events(1000)), "network error became a false offline event"
            with server.lock:
                server.faults = False
                server.live = True
            # Retry-After and platform backoff are deliberately not cleared.
            recovered_ids = {by_number[n] for n in range(6)}
            await_condition(engine, lambda s: all(r["live_status"] == "live" and not r.get("status_stale", False)
                                    for r in s["rooms"] if r["id"] in recovered_ids),
                            20, "timed-out processes did not recover")
            report["recovery_verified"] = True
            report["resource_start"] = system_health()
            next_sample = time.monotonic()
            max_processes = 0
            max_handles = 0
            max_private_bytes = 0
            max_threads = 0
            previous_fault_cycle = None
            # A constant status avoids creating millions of fake sessions in a
            # 24-hour soak; every probe still traverses the real HTTP stack.
            while time.monotonic() - began < duration:
                elapsed = time.monotonic() - began
                fault_cycle = int(elapsed // 60) if elapsed >= 60 and elapsed % 60 < 8 else None
                # A long soak repeats hangs, disconnects and HTTP 429 without
                # inventing broadcast transitions or clearing Retry-After.
                # The first minute retains the focused startup/recovery test.
                with server.lock:
                    server.faults = fault_cycle is not None
                if fault_cycle is not None and fault_cycle != previous_fault_cycle:
                    report["periodic_fault_windows"] += 1
                previous_fault_cycle = fault_cycle
                state = engine.snapshot()
                monitor = state["monitor"]
                max_processes = max(max_processes, monitor.get("active_processes", 0))
                assert monitor["active_checks"] <= 32
                assert monitor.get("active_processes", 0) <= 32
                if time.monotonic() >= next_sample:
                    health = system_health()
                    max_handles = max(max_handles, health.get("handle_count") or 0)
                    max_private_bytes = max(max_private_bytes, health.get("private_bytes") or 0)
                    max_threads = max(max_threads, health.get("thread_count") or 0)
                    report.update(elapsed_seconds=round(time.monotonic()-began, 3),
                                  monitor=monitor, resource_latest=health,
                                  max_processes=max_processes, max_parent_handles=max_handles,
                                  max_parent_private_bytes=max_private_bytes, max_parent_threads=max_threads)
                    if report_path:
                        report_path.parent.mkdir(parents=True, exist_ok=True)
                        report_path.write_text(json.dumps(report, indent=2), "utf-8")
                    next_sample = time.monotonic() + 10
                time.sleep(0.2)
            report["monitor"] = engine.snapshot()["monitor"]
            report["requests"] = sum(server.counts.values())
            report["resource_end"] = system_health()
        except Exception as exc:
            report["passed"] = False
            report["failure"] = str(exc) or type(exc).__name__
            if engine:
                report["monitor"] = engine.snapshot()["monitor"]
            raise
        finally:
            if engine:
                engine.close()
                report["remaining_processes_after_close"] = engine.pool.stats().get("active_processes")
                assert report["remaining_processes_after_close"] == 0
            if store:
                store.close()
            server.stop.set()
            server.shutdown()
            server.server_close()
            thread.join(3)
            if report_path and report.get("passed") is False:
                report["elapsed_seconds"] = round(time.monotonic() - began, 3)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, indent=2), "utf-8")
    report["elapsed_seconds"] = round(time.monotonic() - began, 3)
    report["passed"] = True
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=30, help="minimum total fixture runtime in seconds")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--request-timeout", type=float, default=4,
                        help="status request deadline in seconds (release default: 4; optional tuned profile: 2)")
    args = parser.parse_args()
    if not 0 <= args.duration <= 7 * 86400:
        parser.error("duration must be between 0 and 604800 seconds")
    if not 2 <= args.request_timeout <= 60:
        parser.error("request-timeout must be between 2 and 60 seconds")
    run(args.duration, args.report, args.request_timeout)
