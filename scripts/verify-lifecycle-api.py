"""Exercise new controls through the real local HTTP API in an isolated directory."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("project_checks", Path(__file__).with_name("verify-project.py"))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"live": True, "stream_url": f"http://127.0.0.1:{self.server.server_port}/live.ts"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class LifecycleApiChecks(unittest.TestCase):
    def test_settings_source_refresh_and_preferences_survive_restart(self):
        with tempfile.TemporaryDirectory(prefix="livewatch-lifecycle-api-") as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            backend = checks.Backend(Path(directory) / "中文 数据")
            try:
                backend.start()
                config = {"monitor_max_requests_per_second": 20, "record_segment_seconds": 60,
                          "record_stall_seconds": 10, "record_start_timeout_seconds": 20,
                          "status_poll_interval_seconds": 8, "sound_enabled": False}
                backend.json("PUT", "/api/settings", config)
                for key, value in [("monitor_max_requests_per_second", 0), ("record_segment_seconds", 59),
                                   ("record_stall_seconds", 9), ("record_start_timeout_seconds", 121)]:
                    self.assertEqual(backend.request("PUT", "/api/settings", {key: value})[0], 400)
                room = backend.json("POST", "/api/rooms", {
                    "platform": "wechat", "room_input": "source-selection", "record_enabled": False,
                    "status_url": f"http://127.0.0.1:{server.server_port}/status",
                }, expected=201)["room"]["id"]
                route = "/api/room-source/" + room
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = backend.json("GET", "/api/state")["rooms"][0]
                    if current.get("last_success_at"):
                        break
                    time.sleep(.05)
                self.assertEqual(current["room_status"], "live")
                self.assertFalse(backend.json("GET", route)["room"]["resolved_stream_url"])
                self.assertEqual(backend.request("POST", route + "/refresh", {},
                                                {"X-LiveWatch-Token": "wrong"})[0], 403)
                backend.json("POST", route + "/refresh", {})
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    source = backend.json("GET", route)
                    if not source["source_refresh_pending"]:
                        break
                    time.sleep(.05)
                self.assertFalse(source["source_refresh_pending"])
                self.assertTrue(source["room"]["resolved_stream_url"].endswith("/live.ts"))
                # This setting must be saved independently from enabling recording.
                backend.json("PUT", "/api/rooms/" + room, {"record_quality": "unknown", "record_protocol": "ts",
                                                            "monitor_enabled": False})
                self.assertEqual(backend.request("POST", route + "/refresh", {})[0], 400)
                self.assertEqual(backend.stop(), 0)
                backend.start()
                state = backend.json("GET", "/api/state")
                for key, value in config.items():
                    self.assertEqual(state["settings"][key], value)
                saved = state["rooms"][0]
                self.assertEqual(saved["record_quality"], "unknown")
                self.assertEqual(saved["record_protocol"], "ts")
                self.assertFalse(saved["record_enabled"])
                self.assertEqual(saved["room_status"], "disabled")
            finally:
                backend.stop()
                server.shutdown()
                server.server_close()
                thread.join(3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
