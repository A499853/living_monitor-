r"""Offline restoration checks; writes only to a temporary test directory.

Run from any directory with: tools\python\python.exe scripts\verify-project.py
No account, real livestream, existing database, or external server is used.
"""
from __future__ import annotations

import ast
import functools
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


PROJECT = Path(__file__).resolve().parents[1]
APP = PROJECT / "app"
FFMPEG = PROJECT / "tools" / "ffmpeg.exe"
sys.dont_write_bytecode = True
sys.path.insert(0, str(APP))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def command(arguments, timeout=20):
    result = subprocess.run(
        [str(value) for value in arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise AssertionError(
            f"Command exited {result.returncode}: {arguments[0]}\n"
            + result.stderr.decode("utf-8", "replace")[-4000:]
        )
    return result


class Backend:
    def __init__(self, data):
        self.data = Path(data)
        self.process = None
        self.port = None
        self.token = None
        self.launch = [sys.executable, "-B", str(PROJECT / "run.py"), "--no-browser",
                       "--data-dir", str(data), "--owner-pid", str(os.getpid())]

    def start(self):
        self.process = subprocess.Popen(
            self.launch, cwd=self.data.parent, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
        )
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    stdout, stderr = self.process.communicate(timeout=1)
                    raise AssertionError("Backend exited during startup: " +
                                         (stdout + stderr).decode("utf-8", "replace")[-4000:])
                try:
                    info = json.loads((self.data / "data" / "instance.json").read_text("utf-8"))
                    if info["pid"] == self.process.pid:
                        self.port = int(info["port"])
                        status, _, body = self.request("GET", "/api/health")
                        if status == 200 and json.loads(body).get("app") == "LiveWatch":
                            status, _, html = self.request("GET", "/")
                            match = re.search(rb'<meta name="api-token" content="([^"]+)">', html)
                            if status != 200 or not match:
                                raise AssertionError("Frontend session token is missing")
                            self.token = match[1].decode("ascii")
                            return self
                except (OSError, ValueError, KeyError, http.client.HTTPException):
                    pass
                time.sleep(0.05)
            raise AssertionError("Backend did not become healthy within 15 seconds")
        except BaseException:
            self.stop()
            raise

    def request(self, method, path, body=None, headers=None):
        supplied = dict(headers or {})
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            supplied.setdefault("Content-Type", "application/json")
        if method != "GET" and "X-LiveWatch-Token" not in supplied and self.token:
            supplied["X-LiveWatch-Token"] = self.token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body, supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json(self, method, path, body=None, expected=200, headers=None):
        status, _, result = self.request(method, path, body, headers)
        if status != expected:
            raise AssertionError(f"{method} {path}: expected {expected}, got {status}: " +
                                 result.decode("utf-8", "replace")[:1000])
        return json.loads(result)

    def stop(self):
        if self.process is None:
            return None
        process, self.process = self.process, None
        if process.poll() is None and self.token:
            try:
                self.request("POST", "/api/shutdown", {})
            except (OSError, http.client.HTTPException):
                pass
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
            raise AssertionError("Backend required forced termination")
        return process.returncode


class QuietMediaHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class RestorationChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="livewatch-restore-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文 and spaces"
        self.root.mkdir()

    def test_layout_source_syntax_and_version(self):
        required = ["run.py", "app/run.py", "app/livewatch/web/index.html",
                    "app/livewatch/web/app.js", "app/livewatch/web/style.css",
                    "tools/python/python.exe", "tools/ffmpeg.exe", "直播守望.exe",
                    "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll",
                    "WebView2Loader.dll", "tools/webview2/msedgewebview2.exe"]
        for name in required:
            self.assertTrue((PROJECT / name).is_file(), name)
        source_files = [PROJECT / "run.py", *APP.rglob("*.py")]
        for source in source_files:
            ast.parse(source.read_text("utf-8-sig"), filename=str(source))
        from livewatch import __version__
        result = command([sys.executable, "-B", PROJECT / "run.py", "--version"])
        self.assertEqual(result.stdout.decode().strip(), __version__)
        self.assertEqual(__version__, "2.8.0")

    def test_backend_api_guards_single_instance_and_persistence(self):
        backend = Backend(self.root / "state")
        self.addCleanup(backend.stop)
        backend.start()
        self.assertEqual(backend.json("GET", "/api/health")["version"], "2.8.0")
        for path, mime in [("/", "text/html"), ("/app.js", "text/javascript"),
                           ("/style.css", "text/css")]:
            status, headers, body = backend.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(mime, headers["Content-Type"])
            self.assertGreater(len(body), 100)
            self.assertNotIn(b"__API_TOKEN__", body)
        self.assertEqual(backend.request("GET", "/api/health", headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(backend.request("GET", "/api/state", headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(backend.request("PUT", "/api/settings", {"monitor_batch_size": 2},
                                         {"X-LiveWatch-Token": "invalid"})[0], 403)
        self.assertEqual(backend.json("GET", "/api/state")["rooms"], [])
        settings = {"monitor_batch_size": 16, "monitor_interval_minutes": 1.5,
                    "monitor_schedule_mode": "batch", "monitor_douyin_concurrency": 2,
                    "monitor_process_isolation": True,
                    "status_only_mode": True, "status_check_concurrency": 48,
                    "status_douyin_concurrency": 24, "status_poll_interval_seconds": 3,
                    "status_request_timeout": 2, "monitor_identity_isolation": True,
                    "room_check_delay_seconds": 2, "ffmpeg_path": str(FFMPEG)}
        backend.json("PUT", "/api/settings", settings)
        for invalid in ({"monitor_batch_size": 17}, {"monitor_batch_size": True},
                        {"monitor_douyin_concurrency": 6}, {"monitor_douyin_concurrency": 0},
                        {"monitor_process_isolation": 1}, {"monitor_schedule_mode": "invalid"},
                        {"status_check_concurrency": 65}, {"status_douyin_concurrency": True},
                        {"status_douyin_concurrency": 0}, {"status_only_mode": 1},
                        {"status_poll_interval_seconds": 0}, {"status_request_timeout": 16}):
            self.assertEqual(backend.request("PUT", "/api/settings", invalid)[0], 400)
        room = {"platform": "douyin", "room_input": "restore_smoke_20261121",
                "nickname": "离线恢复测试", "monitor_enabled": False,
                "record_enabled": False, "remark": "created"}
        created = backend.json("POST", "/api/rooms", room, expected=201)
        room_id = created["room"]["id"]
        backend.json("PUT", "/api/rooms/" + room_id, {"remark": "persisted"})
        state = backend.json("GET", "/api/state")
        self.assertEqual(state["stats"]["monitoring"], 0)
        self.assertEqual(state["rooms"][0]["live_status"], "disabled")
        self.assertEqual(state["monitor"]["active_checks"], 0)
        self.assertTrue(state["ffmpeg_available"])
        # The duplicate launch must exit without taking ownership or creating a second backend.
        command(backend.launch, timeout=5)
        saved = json.loads((backend.data / "data" / "instance.json").read_text("utf-8"))
        self.assertEqual(saved["pid"], backend.process.pid)
        self.assertEqual(backend.stop(), 0)
        self.assertFalse((backend.data / "data" / "instance.json").exists())
        backend.start()
        state = backend.json("GET", "/api/state")
        self.assertEqual(len(state["rooms"]), 1)
        self.assertEqual(state["rooms"][0]["id"], room_id)
        self.assertEqual(state["rooms"][0]["remark"], "persisted")
        self.assertFalse(state["rooms"][0]["monitor_enabled"])
        for key, value in settings.items():
            self.assertEqual(state["settings"][key], value)
        backend.json("DELETE", "/api/rooms/" + room_id, {})
        self.assertEqual(backend.json("GET", "/api/state")["rooms"], [])
        self.assertEqual(backend.stop(), 0)

    def test_backend_process_probes_and_live_settings(self):
        # Exercise the actual API -> Engine -> child ProviderRegistry path,
        # including switching off isolation without restarting the backend.
        fixture = self.root / "status.json"
        fixture.write_text(json.dumps({"live": False, "nickname": "子进程监控"}), "utf-8")
        handler = functools.partial(QuietMediaHandler, directory=str(self.root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        backend = Backend(self.root / "isolated-state")
        try:
            backend.start()
            backend.json("PUT", "/api/settings", {
                "monitor_schedule_mode": "independent", "monitor_batch_size": 3,
                "monitor_process_isolation": True, "room_check_delay_seconds": 0,
                "sound_enabled": False,
            })
            for number in range(3):
                backend.json("POST", "/api/rooms", {
                    "platform": "wechat", "room_input": f"local-process-{number}",
                    "record_enabled": False,
                    "status_url": f"http://127.0.0.1:{server.server_port}/status.json",
                }, expected=201)

            def await_state(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = backend.json("GET", "/api/state")
                    if predicate(state):
                        return state
                    time.sleep(.02)
                self.fail("Backend probes did not reach the expected state")

            state = await_state(lambda state: all(room["live_status"] == "waiting"
                                                  for room in state["rooms"]))
            self.assertEqual(len(state["rooms"]), 3)
            self.assertTrue(all(room["nickname"] == "子进程监控" for room in state["rooms"]))
            self.assertTrue(1 <= state["monitor"]["active_processes"] <= 3)
            backend.json("PUT", "/api/settings", {"monitor_process_isolation": False})
            await_state(lambda state: state["monitor"]["active_processes"] == 0)
            fixture.write_text(json.dumps({"live": True, "nickname": "线程监控"}), "utf-8")
            backend.json("POST", "/api/refresh", {})
            state = await_state(lambda state: all(room["live_status"] == "live" and
                                                  room["nickname"] == "线程监控"
                                                  for room in state["rooms"]))
            self.assertEqual(state["monitor"]["active_processes"], 0)
            self.assertFalse(state["monitor"]["process_isolation"])
            self.assertEqual(backend.stop(), 0)
        finally:
            backend.stop()
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_local_media_recording_and_playback(self):
        from livewatch.playback import Playback
        from livewatch.recorder import Recorder

        fixture = self.root / "fixture.ts"
        command([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                 "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-threads", "1", "-c:a", "aac", "-f", "mpegts", fixture])
        handler = functools.partial(QuietMediaHandler, directory=str(self.root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        recorder = Recorder(self.root)
        playback = Playback(self.root)
        try:
            settings = {"ffmpeg_path": str(FFMPEG)}
            room = {"id": "offline-media-test", "platform": "douyin",
                    "room_input": "offline-media-test", "nickname": "恢复测试"}
            url = f"http://127.0.0.1:{server.server_port}/fixture.ts"
            recorder.start(room, url, {}, settings)
            recorder.jobs[room["id"]]["process"].wait(timeout=15)
            recorded = recorder.stop(room["id"])
            self.assertEqual(recorded["returncode"], 0)
            self.assertTrue(recorded["has_media"])
            self.assertGreater(recorded["duration_seconds"], 0)
            output = Path(recorded["path"])
            self.assertGreater(output.stat().st_size, 0)
            command([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-i", output, "-f", "null", "-"])
            info = playback.create(room, {"resolved_stream_url": url}, settings)
            session = playback.start(info["id"])
            movie, _ = session["process"].communicate(timeout=15)
            self.assertEqual(session["process"].returncode, 0)
            self.assertIn(b"ftyp", movie[:64])
            converted = self.root / "playback.mp4"
            converted.write_bytes(movie)
            command([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-i", converted, "-f", "null", "-"])
        finally:
            playback.close()
            recorder.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
