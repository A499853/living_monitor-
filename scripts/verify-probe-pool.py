"""Local fault-injection tests for bounded, persistent probe processes.

Uses temporary worker scripts and loopback HTTP only; no user data or accounts.
Run: tools/python/python.exe -B scripts/verify-probe-pool.py
"""
from __future__ import annotations

import concurrent.futures
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'app'))
sys.dont_write_bytecode = True

from livewatch import probe_pool
from livewatch.probe_pool import ProbePool
from livewatch.providers import ProbeResult, ProviderRegistry


class StatusHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == '/slow':
            self.server.slow_entered.set()
            self.server.release_slow.wait(5)
        result = dict(live=False, nickname='隔离检查')
        if self.path == '/large':
            result.update(live=True, stream_url='http://127.0.0.1/media',
                          headers={'X-Large-' + str(index): 'x' * 16384 for index in range(16)})
        body = json.dumps(result, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('Condition did not become true within the test budget')


class ProbePoolChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), StatusHandler)
        cls.server.slow_entered = threading.Event()
        cls.server.release_slow = threading.Event()
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.release_slow.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(3)

    def setUp(self):
        self.provider = ProviderRegistry()
        self.pool = ProbePool(self.provider, max_workers=1)
        self.addCleanup(self.pool.shutdown)
        self.room = dict(platform='wechat', room_input='local-only',
                         status_url='http://127.0.0.1:' + str(self.server.server_port) + '/status',
                         stream_url='', request_headers={})

    def submit(self, seconds=3, room=None, **settings):
        return self.pool.submit(self.provider.check, room or self.room,
                                dict(settings, _probe_deadline=time.monotonic() + seconds))

    def workers(self):
        with self.pool._lock:
            return list(self.pool._workers)

    def test_persistent_process_and_real_probe_result(self):
        pids = []
        for _ in range(3):
            result = self.submit().result(timeout=5)
            self.assertIs(result.live, False)
            self.assertEqual(result.nickname, '隔离检查')
            pids.append(self.workers()[0].process.pid)
        self.assertEqual(len(set(pids)), 1)
        self.assertEqual(self.pool.stats(), dict(active_processes=1, process_restarts=0, process_timeouts=0))
        self.assertNotEqual(pids[0], os.getpid())

    def test_large_valid_response_round_trips_buffered_protocol(self):
        room = dict(self.room, status_url=self.room['status_url'].replace('/status', '/large'))
        result = self.submit(room=room).result(timeout=5)
        self.assertIs(result.live, True)
        self.assertEqual(len(result.headers), 16)
        self.assertTrue(all(value == 'x' * 16384 for value in result.headers.values()))
        self.assertEqual(result.nickname, '隔离检查')

    def test_rebuild_replaces_busy_transport_only_after_request_finishes(self):
        self.assertIs(self.submit().result(timeout=5).live,False)
        old=self.workers()[0]
        self.server.slow_entered.clear(); self.server.release_slow.clear()
        slow_room=dict(self.room,status_url=self.room['status_url'].replace('/status','/slow'))
        future=self.submit(room=slow_room)
        self.assertTrue(self.server.slow_entered.wait(2))
        self.pool.rebuild_sessions()
        self.assertIsNone(old.process.poll())
        self.server.release_slow.set()
        self.assertIs(future.result(timeout=5).live,False)
        self.assertIs(self.submit().result(timeout=5).live,False)
        self.assertNotEqual(self.workers()[0].process.pid,old.process.pid)
        self.assertIsNotNone(old.process.poll())

    def test_relative_probe_budget_starts_after_executor_queue(self):
        self.assertIs(self.submit().result(timeout=5).live,False)
        entered=threading.Event(); release=threading.Event()
        self.addCleanup(release.set)
        blocker=self.pool._executor.submit(lambda:(entered.set(),release.wait(3)))
        self.assertTrue(entered.wait(1))
        future=self.submit(seconds=.1,_probe_budget_seconds=1)
        time.sleep(.2)
        self.assertIsNone(future.probe_timing['started_at'])
        self.assertFalse(future.done())
        release.set(); blocker.result(timeout=2)
        self.assertIs(future.result(timeout=3).live,False)
        self.assertIsNotNone(future.probe_timing['started_at'])

    def test_long_relative_budgets_replace_stale_queued_deadline(self):
        for budget in (20,60):
            with self.subTest(budget=budget):
                future=self.submit(seconds=-1,_probe_budget_seconds=budget)
                self.assertIs(future.result(timeout=5).live,False)
                self.assertIsNotNone(future.probe_timing['started_at'])

    def test_abort_busy_process_is_nonblocking_and_reaps_before_replacement(self):
        self.assertIs(self.submit().result(timeout=5).live,False)
        worker=self.workers()[0]
        self.server.slow_entered.clear(); self.server.release_slow.clear()
        slow=dict(self.room,status_url=self.room['status_url'].replace('/status','/slow'))
        future=self.submit(seconds=30,room=slow,_probe_budget_seconds=30)
        self.assertTrue(self.server.slow_entered.wait(2))
        with worker._close_lock:
            began=time.monotonic()
            self.assertTrue(self.pool.abort(future))
            self.assertLess(time.monotonic()-began,.2)
            self.assertIn(worker,self.pool._retiring_workers)
        self.assertIsNone(future.result(timeout=3).live)
        self.assertIsNotNone(worker.process.poll())
        self.assertFalse(worker._io_thread.is_alive())
        self.assertIs(self.submit().result(timeout=5).live,False)
        self.assertNotEqual(self.workers()[0].process.pid,worker.process.pid)
        self.server.release_slow.set()

    def test_abort_queued_task_never_calls_provider(self):
        entered=threading.Event(); release=threading.Event(); called=threading.Event()
        self.addCleanup(release.set)
        blocker=self.pool._executor.submit(lambda:(entered.set(),release.wait(3)))
        self.assertTrue(entered.wait(1))
        future=self.pool.submit(lambda *_:called.set(),self.room,{'monitor_process_isolation':False})
        self.pool.abort(future)
        release.set(); blocker.result(timeout=2)
        self.assertTrue(future.cancelled())
        self.assertFalse(called.is_set())

    def test_slow_loopback_endpoint_respects_total_deadline(self):
        # Measure the network deadline independently of cold process startup,
        # which can consume the short budget under parallel suite load.
        self.assertIs(self.submit().result(timeout=5).live, False)
        self.server.slow_entered.clear()
        self.server.release_slow.clear()
        slow_room = dict(self.room, status_url=self.room['status_url'].replace('/status', '/slow'))
        began = time.monotonic()
        result = self.submit(.5, room=slow_room).result(timeout=3)
        self.assertTrue(self.server.slow_entered.is_set())
        self.assertLess(time.monotonic() - began, 2)
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code, 'network_error')
        self.server.release_slow.set()
        self.assertIs(self.submit().result(timeout=5).live, False)

    def test_hung_worker_is_killed_and_next_task_restarts(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-hung-probe-') as directory:
            worker_file = Path(directory) / 'hung.py'
            worker_file.write_text('import sys,time\nfor line in sys.stdin.buffer:\n time.sleep(60)\n', encoding='utf-8')
            with patch.object(probe_pool, '_WORKER_PATH', worker_file):
                began = time.monotonic()
                future = self.submit(.4)
                worker = wait_for(lambda: next(iter(self.workers()), None))
                result = future.result(timeout=3)
            self.assertLess(time.monotonic() - began, 2)
            self.assertIsNone(result.live)
            self.assertEqual(result.error_code, 'network_error')
            self.assertIn('network=deadline', result.diagnostic)
            self.assertIsNotNone(worker.process.poll())
            self.assertFalse(worker._io_thread.is_alive())
            self.assertEqual(self.pool.stats()['process_timeouts'], 1)
            self.assertEqual(self.pool.stats()['active_processes'], 0)
            self.assertIs(self.submit().result(timeout=5).live, False)
            self.assertEqual(self.pool.stats()['process_restarts'], 1)

    def test_dead_idle_process_replaced_before_next_request(self):
        self.assertIs(self.submit().result(timeout=5).live, False)
        old = self.workers()[0]
        old.process.kill()
        old.process.wait(timeout=3)
        result = self.submit().result(timeout=5)
        self.assertIs(result.live, False)
        self.assertNotEqual(self.workers()[0].process.pid, old.process.pid)
        self.assertFalse(old._io_thread.is_alive())
        self.assertEqual(self.pool.stats()['process_restarts'], 1)

    def test_crash_during_probe_returns_unknown_and_next_task_recovers(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-crash-probe-') as directory:
            worker_file = Path(directory) / 'crash.py'
            worker_file.write_text('import os,sys\nsys.stdin.buffer.readline()\nos._exit(23)\n', encoding='utf-8')
            with patch.object(probe_pool, '_WORKER_PATH', worker_file):
                result = self.submit().result(timeout=5)
            self.assertIsNone(result.live)
            self.assertEqual(result.error_code, 'network_error')
            self.assertEqual(self.pool.stats()['active_processes'], 0)
            self.assertIs(self.submit().result(timeout=5).live, False)
            self.assertEqual(self.pool.stats()['process_restarts'], 1)

    def test_blocked_pipe_write_obeys_deadline_without_leaking_io_thread(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-pipe-probe-') as directory:
            worker_file = Path(directory) / 'no_read.py'
            worker_file.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
            room = dict(self.room, request_headers={'X-Test-Large': 'x' * (512 * 1024)})
            with patch.object(probe_pool, '_WORKER_PATH', worker_file):
                began = time.monotonic()
                future = self.submit(.4, room=room)
                worker = wait_for(lambda: next(iter(self.workers()), None))
                result = future.result(timeout=3)
            self.assertLess(time.monotonic() - began, 2)
            self.assertIsNone(result.live)
            self.assertIn('network=deadline', result.diagnostic)
            self.assertFalse(worker._io_thread.is_alive())
            self.assertIsNotNone(worker.process.poll())

    def test_oversized_protocol_response_is_rejected_and_process_reaped(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-large-probe-') as directory:
            worker_file = Path(directory) / 'oversized.py'
            worker_file.write_text(
                'import sys,time\nsys.stdin.buffer.readline()\n'
                'sys.stdout.buffer.write(b"x"*' + str(probe_pool.MAX_MESSAGE_BYTES + 1) + ')\n'
                'sys.stdout.buffer.flush()\ntime.sleep(60)\n', encoding='utf-8')
            with patch.object(probe_pool, '_WORKER_PATH', worker_file):
                future = self.submit()
                worker = wait_for(lambda: next(iter(self.workers()), None))
                result = future.result(timeout=5)
            self.assertIsNone(result.live)
            self.assertEqual(result.error_code, 'network_error')
            self.assertEqual(self.pool.stats()['active_processes'], 0)
            self.assertFalse(worker._io_thread.is_alive())
            self.assertIsNotNone(worker.process.poll())

    def test_shutdown_interrupts_running_worker_and_cancels_queue(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-stop-probe-') as directory:
            worker_file = Path(directory) / 'hung.py'
            worker_file.write_text('import sys,time\nfor line in sys.stdin.buffer:\n time.sleep(60)\n', encoding='utf-8')
            with patch.object(probe_pool, '_WORKER_PATH', worker_file):
                running = self.submit(30)
                worker = wait_for(lambda: next(iter(self.workers()), None))
                queued = self.submit(30)
                began = time.monotonic()
                self.pool.shutdown()
            self.assertLess(time.monotonic() - began, 2)
            self.assertTrue(running.done())
            self.assertIsNone(running.result().live)
            self.assertTrue(queued.done())
            self.assertTrue(queued.cancelled() or queued.result().live is None)
            self.assertIsNotNone(worker.process.poll())
            self.assertFalse(worker._io_thread.is_alive())
            self.assertEqual(self.pool.stats()['active_processes'], 0)
            with self.assertRaises(RuntimeError):
                self.submit()

    def test_disabled_isolation_calls_supplied_function_without_process(self):
        called = []
        def direct(room, settings):
            called.append((room, settings, os.getpid()))
            return ProbeResult(True, nickname='直接检查')
        result = self.pool.submit(direct, self.room, dict(monitor_process_isolation=False)).result(timeout=2)
        self.assertTrue(result.live)
        self.assertEqual(result.nickname, '直接检查')
        self.assertEqual(called[0][2], os.getpid())
        self.assertEqual(self.pool.stats()['active_processes'], 0)

    def test_configure_retires_idle_process_and_allows_reenable(self):
        self.assertIs(self.submit().result(timeout=5).live, False)
        old = self.workers()[0]
        began = time.monotonic()
        self.pool.configure(dict(monitor_process_isolation=False))
        self.assertLess(time.monotonic() - began, .2)
        wait_for(lambda: self.pool.stats()['active_processes'] == 0)
        self.assertFalse(old._io_thread.is_alive())
        self.assertIsNotNone(old.process.poll())
        direct = self.pool.submit(lambda *_args: ProbeResult(True, nickname='直接'), self.room, {}).result(timeout=3)
        self.assertTrue(direct.live)
        self.assertEqual(self.pool.stats()['active_processes'], 0)
        self.pool.configure(dict(monitor_process_isolation=True))
        self.assertIs(self.submit().result(timeout=5).live, False)
        self.assertEqual(self.pool.stats()['active_processes'], 1)
        self.assertEqual(self.pool.stats()['process_restarts'], 1)
        self.pool.shutdown()
        self.assertTrue(all(not thread.is_alive() for thread in self.pool._retire_threads))

    def test_configure_lets_running_probe_finish_then_retires_process(self):
        self.server.slow_entered.clear()
        self.server.release_slow.clear()
        slow_room = dict(self.room, status_url=self.room['status_url'].replace('/status', '/slow'))
        future = self.submit(room=slow_room)
        self.assertTrue(self.server.slow_entered.wait(3))
        worker = self.workers()[0]
        began = time.monotonic()
        self.pool.configure(dict(monitor_process_isolation=False))
        self.assertLess(time.monotonic() - began, .2)
        self.assertFalse(future.done())
        self.assertIsNone(worker.process.poll())
        self.server.release_slow.set()
        self.assertIs(future.result(timeout=5).live, False)
        self.assertIsNotNone(worker.process.poll())
        self.assertFalse(worker._io_thread.is_alive())
        self.assertEqual(self.pool.stats()['active_processes'], 0)
        # A queued snapshot with isolation=True also observes configure(False).
        result = self.pool.submit(lambda *_args: ProbeResult(True), self.room,
                                  dict(monitor_process_isolation=True)).result(timeout=3)
        self.assertTrue(result.live)
        self.assertEqual(self.pool.stats()['active_processes'], 0)

    def test_reenable_during_probe_preserves_its_reusable_process(self):
        self.server.slow_entered.clear()
        self.server.release_slow.clear()
        slow_room = dict(self.room, status_url=self.room['status_url'].replace('/status', '/slow'))
        future = self.submit(room=slow_room)
        self.assertTrue(self.server.slow_entered.wait(3))
        worker = self.workers()[0]
        self.pool.configure(dict(monitor_process_isolation=False))
        self.pool.configure(dict(monitor_process_isolation=True))
        self.server.release_slow.set()
        self.assertIs(future.result(timeout=5).live, False)
        self.assertIsNone(worker.process.poll())
        self.assertIs(self.submit().result(timeout=5).live, False)
        self.assertEqual(self.workers()[0].process.pid, worker.process.pid)

    def test_configure_and_shutdown_overlap_without_orphan_process(self):
        self.assertIs(self.submit().result(timeout=5).live, False)
        worker = self.workers()[0]
        start = threading.Barrier(2)
        errors = []
        def disable():
            try:
                start.wait(timeout=3)
                self.pool.configure(dict(monitor_process_isolation=False))
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=disable)
        thread.start()
        start.wait(timeout=3)
        self.pool.shutdown()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNotNone(worker.process.poll())
        self.assertFalse(worker._io_thread.is_alive())
        self.assertTrue(all(not retired.is_alive() for retired in self.pool._retire_threads))

    def test_reenable_waits_for_retirement_before_spawning_replacement(self):
        self.assertIs(self.submit().result(timeout=5).live, False)
        old = self.workers()[0]
        replacement_started = threading.Event()
        original = probe_pool._WorkerProcess
        def create_worker():
            replacement_started.set()
            return original()
        # Hold the close lock to simulate a slow OS process retirement. The
        # next executor task must wait for it without blocking configure().
        with patch.object(probe_pool, '_WorkerProcess', side_effect=create_worker):
            with old._close_lock:
                self.pool.configure(dict(monitor_process_isolation=False))
                self.pool.configure(dict(monitor_process_isolation=True))
                future = self.submit()
                wait_for(future.running)
                self.assertFalse(replacement_started.wait(.15))
                self.assertEqual(self.pool.stats()['active_processes'], 1)
                self.assertIsNone(old.process.poll())
            self.assertIs(future.result(timeout=5).live, False)
        self.assertTrue(replacement_started.is_set())
        self.assertIsNotNone(old.process.poll())
        self.assertEqual(self.pool.stats()['active_processes'], 1)

    def wider_pool(self, width, limit):
        self.pool.shutdown()
        self.pool = ProbePool(self.provider, max_workers=width)
        self.addCleanup(self.pool.shutdown)
        self.pool.configure(dict(status_only_mode=True, status_check_concurrency=limit))
        self.server.slow_entered.clear()
        self.server.release_slow.clear()
        self.addCleanup(self.server.release_slow.set)
        return dict(self.room, status_url=self.room['status_url'].replace('/status', '/slow'))

    def test_idle_children_are_shared_when_executor_threads_change(self):
        slow = self.wider_pool(8, 2)
        warm = [self.submit(5, room=slow) for _ in range(2)]
        wait_for(lambda: len(self.pool._busy_workers) == 2)
        self.server.release_slow.set()
        self.assertTrue(all(future.result(timeout=5).live is False for future in warm))
        original_pids = {worker.process.pid for worker in self.workers()}
        self.assertEqual(len(original_pids), 2)
        # Occupy all original executor threads with direct tasks. New isolated
        # probes must run on other threads and still lease the same children.
        barrier = threading.Barrier(len(self.pool._executor._threads) + 1)
        release = threading.Event()
        def direct(*_args):
            barrier.wait(timeout=3)
            release.wait(5)
            return ProbeResult(False)
        blockers = [self.pool.submit(direct, self.room, dict(monitor_process_isolation=False))
                    for _ in range(barrier.parties - 1)]
        try:
            barrier.wait(timeout=3)
            fresh = [self.submit(3) for _ in range(2)]
            self.assertTrue(all(future.result(timeout=5).live is False for future in fresh))
            self.assertGreater(len(self.pool._executor._threads), 2)
            self.assertEqual({worker.process.pid for worker in self.workers()}, original_pids)
            self.assertEqual(self.pool.stats()['active_processes'], 2)
        finally:
            release.set()
            for future in blockers:
                future.result(timeout=5)

    def test_lowering_width_drains_running_children_before_new_leases(self):
        slow = self.wider_pool(8, 3)
        running = [self.submit(5, room=slow) for _ in range(3)]
        wait_for(lambda: len(self.pool._busy_workers) == 3)
        original_pids = {worker.process.pid for worker in self.workers()}
        began = time.monotonic()
        self.pool.configure(dict(status_only_mode=True, status_check_concurrency=1))
        self.assertLess(time.monotonic() - began, .2)
        queued = self.submit(4, status_check_concurrency=64)
        wait_for(queued.running)
        time.sleep(.1)
        self.assertFalse(queued.done())
        self.assertEqual({worker.process.pid for worker in self.workers()}, original_pids)
        self.server.release_slow.set()
        self.assertTrue(all(future.result(timeout=5).live is False for future in running))
        self.assertIs(queued.result(timeout=5).live, False)
        wait_for(lambda: self.pool.stats()['active_processes'] == 1)
        self.assertTrue({worker.process.pid for worker in self.workers()} <= original_pids)

    def test_hot_growth_then_idle_shrink_keeps_real_process_count_bounded(self):
        slow = self.wider_pool(8, 2)
        for limit in (2, 4):
            self.server.release_slow.clear()
            self.pool.configure(dict(status_only_mode=True, status_check_concurrency=limit))
            futures = [self.submit(5, room=slow) for _ in range(limit)]
            wait_for(lambda: len(self.pool._busy_workers) == limit)
            self.assertEqual(self.pool.stats()['active_processes'], limit)
            self.server.release_slow.set()
            self.assertTrue(all(future.result(timeout=5).live is False for future in futures))
        old_workers = self.workers()
        began = time.monotonic()
        self.pool.configure(dict(status_only_mode=False, monitor_batch_size=1))
        self.assertLess(time.monotonic() - began, .2)
        wait_for(lambda: self.pool.stats()['active_processes'] == 1)
        self.assertEqual(sum(worker.process.poll() is None for worker in old_workers), 1)
        self.assertIs(self.submit().result(timeout=5).live, False)
        self.assertEqual(self.pool.stats()['active_processes'], 1)

    def test_waiting_for_process_capacity_obeys_request_deadline(self):
        slow = self.wider_pool(4, 1)
        running = self.submit(5, room=slow)
        self.assertTrue(self.server.slow_entered.wait(3))
        began = time.monotonic()
        waiting = self.submit(.2)
        result = waiting.result(timeout=2)
        self.assertLess(time.monotonic() - began, 1)
        self.assertIsNone(result.live)
        self.assertIn('network=deadline', result.diagnostic)
        self.assertEqual(self.pool.stats()['active_processes'], 1)
        self.assertFalse(running.done())
        self.server.release_slow.set()
        self.assertIs(running.result(timeout=5).live, False)

    def test_orphan_worker_exits_even_while_probe_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-parent-probe-') as directory:
            launcher = Path(directory) / 'owner.py'
            launcher.write_text(
                'import json,sys,time\n'
                'sys.path.insert(0,' + repr(str(PROJECT / 'app')) + ')\n'
                'from livewatch.probe_pool import ProbePool\n'
                'from livewatch.providers import ProviderRegistry\n'
                'p=ProviderRegistry(); pool=ProbePool(p,max_workers=1)\n'
                'pool.submit(p.check,' + repr(dict(self.room, status_url=self.room['status_url'].replace('/status', '/slow'))) + ',{"_probe_deadline":time.monotonic()+30})\n'
                'while not pool.stats()["active_processes"]: time.sleep(.01)\n'
                'print(next(iter(pool._workers)).process.pid,flush=True)\n'
                'time.sleep(60)\n', encoding='utf-8')
            self.server.slow_entered.clear()
            self.server.release_slow.clear()
            owner = subprocess.Popen([sys.executable, '-B', str(launcher)], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            child_pid = None
            try:
                # Read through a bounded future so failed startup cannot hang the test.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as reader:
                    line_future = reader.submit(owner.stdout.readline)
                    try:
                        line = line_future.result(timeout=5)
                    except concurrent.futures.TimeoutError:
                        owner.kill()
                        owner.wait(timeout=3)
                        raise
                child_pid = int(line)
                self.assertTrue(self.server.slow_entered.wait(3))
                owner.kill()
                owner.wait(timeout=3)
                self.assertTrue(wait_for(lambda: not process_alive(child_pid), timeout=4))
            finally:
                self.server.release_slow.set()
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=3)
                owner.stdout.close()
                if child_pid and process_alive(child_pid):
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(child_pid), '/F'], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    else:
                        os.kill(child_pid, 9)


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


if __name__ == '__main__':
    unittest.main(verbosity=2)
