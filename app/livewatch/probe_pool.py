"""Bounded, persistent probe processes supervised by executor threads.

Only the parent owns application state. A child receives one room and a settings
snapshot over anonymous pipes, and returns a ProbeResult without touching disk.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import fields
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .providers import ProbeResult


MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_WORKER_PATH = Path(__file__).with_name('probe_worker.py').resolve()
_RESULT_FIELDS = {field.name for field in fields(ProbeResult)}


def _failure(deadline=False, stopped=False):
    message = ('监控任务已停止。' if stopped else
               '检查超时，尚不能确认直播状态。' if deadline else
               '检查进程暂时不可用，稍后自动重试。')
    return ProbeResult(None, message=message, error_code='network_error',
                       diagnostic='network=' + ('deadline' if deadline else 'transport') + ' stage=source_request')


def _encode_message(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8') + b'\n'
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError('Probe message exceeds the protocol limit')
    return data


class _WorkerProcess:
    def __init__(self):
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._requests = queue.Queue(maxsize=1)
        self._responses = queue.Queue(maxsize=1)
        self._sequence = 0
        self.process = subprocess.Popen(
            [sys.executable, '-B', str(_WORKER_PATH), '--owner-pid', str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=64 * 1024, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self._io_thread = threading.Thread(target=self._exchange_loop,
                                           name='probe-pipe-' + str(self.process.pid), daemon=True)
        try:
            self._io_thread.start()
        except BaseException:
            self.close()
            raise

    def _exchange_loop(self):
        # Pipe writes can block too. Keeping both operations here lets the
        # supervisor enforce a deadline even when a child stops reading stdin.
        try:
            while not self._closed.is_set():
                request = self._requests.get()
                if request is None or self._closed.is_set():
                    return
                view = memoryview(request)
                while view:
                    count = self.process.stdin.write(view)
                    if not count:
                        raise OSError('Probe pipe closed')
                    view = view[count:]
                self.process.stdin.flush()
                response = self.process.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not response or len(response) > MAX_MESSAGE_BYTES or not response.endswith(b'\n'):
                    raise ValueError('Invalid probe response')
                self._responses.put_nowait(response)
        except (OSError, ValueError, queue.Full):
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                pass

    def exchange(self, room, settings, deadline):
        self._sequence += 1
        sequence = self._sequence
        request = _encode_message(dict(version=1, id=sequence, room=room, settings=settings))
        if self._closed.is_set():
            raise OSError('Probe worker stopped')
        self._requests.put_nowait(request)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        try:
            response = self._responses.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError() from None
        if response is None:
            raise OSError('Probe worker exited')
        value = json.loads(response)
        if (not isinstance(value, dict) or value.get('version') != 1
                or value.get('id') != sequence or value.get('ok') is not True):
            raise ValueError('Invalid probe response envelope')
        result = value.get('result')
        if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
            raise ValueError('Invalid probe result')
        if result.get('live') is not None and type(result['live']) is not bool:
            raise ValueError('Invalid probe live status')
        return ProbeResult(**result)

    def close(self):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            try:
                self._requests.put_nowait(None)
            except queue.Full:
                pass
            # Closing a buffered pipe from another thread can itself block on
            # its I/O lock. End the process first, which releases pending I/O.
            if self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                    except OSError:
                        pass
                    self.process.wait(timeout=2)
            if self._io_thread.ident is not None:
                self._io_thread.join(timeout=2)
            for pipe in (self.process.stdin, self.process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
            # Wake the supervisor when shutdown races an in-flight request.
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                pass


class ProbePool:
    """ThreadPoolExecutor-compatible probe submission with process isolation."""

    def __init__(self, provider, max_workers=16):
        self.provider = provider
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='probe')
        self._local = threading.local()
        self._lock = threading.RLock()
        self._workers = set()
        self._busy_workers = set()
        self._retiring_workers = set()
        self._retire_threads = set()
        self._isolation_enabled = True
        self._closed = threading.Event()
        self._process_restarts = 0
        self._process_timeouts = 0

    def submit(self, function, room, settings):
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError('cannot schedule new futures after shutdown')
            return self._executor.submit(self._run, function, dict(room), dict(settings))

    def _run(self, function, room, settings):
        if self._closed.is_set():
            return _failure(stopped=True)
        with self._lock:
            isolated = self._isolation_enabled and settings.get('monitor_process_isolation', True)
        if not isolated:
            previous = getattr(self._local, 'worker', None)
            self._local.worker = None
            if previous is not None:
                self._close_worker(previous)
            return function(room, settings)
        deadline = settings.get('_probe_deadline')
        if (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                or not math.isfinite(deadline)):
            timeout = settings.get('request_timeout', 12)
            if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                    or not math.isfinite(timeout) or timeout <= 0):
                timeout = 12
            deadline = time.monotonic() + timeout
            settings['_probe_deadline'] = deadline
        if time.monotonic() >= deadline:
            return _failure(deadline=True)
        worker = getattr(self._local, 'worker', None)
        try:
            if worker is not None:
                with self._lock:
                    unusable = worker._closed.is_set() or worker in self._retiring_workers or worker.process.poll() is not None
                    isolated = self._isolation_enabled
                    if not unusable and isolated:
                        self._busy_workers.add(worker)
                if unusable:
                    # A background retirement may still hold a live process.
                    # Wait here before replacing it so the process count never
                    # temporarily exceeds the executor's worker limit.
                    self._close_worker(worker)
                    self._local.worker = None
                    worker = None
                if not isolated:
                    return function(room, settings)
            if worker is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                worker = _WorkerProcess()
                with self._lock:
                    accepted = not self._closed.is_set() and self._isolation_enabled
                    if accepted:
                        if getattr(self._local, 'started', False):
                            self._process_restarts += 1
                        self._local.started = True
                        self._local.worker = worker
                        self._workers.add(worker)
                        self._busy_workers.add(worker)
                if not accepted:
                    worker.close()
                    return _failure(stopped=True) if self._closed.is_set() else function(room, settings)
            return worker.exchange(room, settings, deadline)
        except (TimeoutError, OSError, ValueError, TypeError, RecursionError, queue.Full) as exc:
            timed_out = isinstance(exc, TimeoutError)
            with self._lock:
                if timed_out and not self._closed.is_set():
                    self._process_timeouts += 1
                self._local.worker = None
            if worker is not None:
                self._close_worker(worker)
            return _failure(deadline=timed_out, stopped=self._closed.is_set())
        finally:
            with self._lock:
                self._busy_workers.discard(worker)
                retire = worker in self._workers and not self._isolation_enabled
                if retire:
                    self._retiring_workers.add(worker)
            if retire:
                self._close_worker(worker)

    def _close_worker(self, worker):
        try:
            worker.close()
        finally:
            with self._lock:
                self._workers.discard(worker)
                self._busy_workers.discard(worker)
                self._retiring_workers.discard(worker)

    def _retire_idle(self, workers):
        for worker in workers:
            self._close_worker(worker)

    def configure(self, settings):
        """Apply mode changes without waiting for pipe or process shutdown.

        Idle children are retired in the background. In-flight probes finish
        under their existing deadline, then release their process. Old queued
        settings cannot start a new process after isolation has been disabled.
        """
        with self._lock:
            self._isolation_enabled = bool(settings.get('monitor_process_isolation', True))
            if self._closed.is_set() or self._isolation_enabled:
                return
            idle = self._workers - self._busy_workers - self._retiring_workers
            if not idle:
                return
            self._retiring_workers.update(idle)
            self._retire_threads = {thread for thread in self._retire_threads if thread.is_alive()}
            thread = threading.Thread(target=self._retire_idle, args=(list(idle),),
                                      name='probe-retire', daemon=True)
            self._retire_threads.add(thread)
            try:
                thread.start()
            except RuntimeError:
                self._retire_threads.discard(thread)
                self._retiring_workers.difference_update(idle)
                raise

    def stats(self):
        with self._lock:
            return dict(active_processes=sum(worker.process.poll() is None for worker in self._workers),
                        process_restarts=self._process_restarts,
                        process_timeouts=self._process_timeouts)

    def shutdown(self, wait=True, *, cancel_futures=True):
        with self._lock:
            self._closed.set()
            workers = list(self._workers)
            retire_threads = list(self._retire_threads)
        # Terminate before executor.shutdown: active requests may otherwise
        # retain their full request budget during application shutdown.
        for worker in workers:
            self._close_worker(worker)
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if wait:
            for thread in retire_threads:
                if thread is not threading.current_thread():
                    thread.join()

