"""Offline contract checks: bounded status requests and no inferred offline.

Uses response fixtures and a loopback trickle server. Never contacts a platform
or reads a user account. Run with tools/python/python.exe -B this-file.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "app"))

from livewatch.providers import ProviderRegistry, ProbeError, _request, _retry_after


class ProviderStatusChecks(unittest.TestCase):
    def setUp(self):
        self.provider = ProviderRegistry()
        self.settings = {"_status_only": True, "monitor_identity_isolation": True,
                         "_probe_deadline": time.monotonic() + 4}
        self.room = {"platform": "douyin", "room_input": "123", "input_type": "room",
                     "record_enabled": False, "request_headers": {}}
        # These checks exercise the compact warm path; visitor initialization
        # and bounded page recovery have their own cold-worker contract tests.
        self.provider._auth_state(self.settings)
        self.provider._local.status_bootstrapped=True
        self.provider._page_attempts['123']=time.monotonic()+30

    @staticmethod
    def response(value):
        return json.dumps(value).encode(), {"Content-Type": "application/json"}, "https://live.douyin.com/123"

    def test_room_status_success_uses_one_endpoint_without_enrichment(self):
        response = self.response({"data": {"data": [{"status": 2, "id_str": "456",
            "owner": {"nickname": "anchor"},
            "stream_url": {"hls_pull_url": "https://media.invalid/live.m3u8"}}]}})
        with patch("livewatch.providers._request", return_value=response) as request, \
                patch.object(self.provider, "_enrich_douyin", side_effect=AssertionError("metadata fetched")):
            result = self.provider.check(self.room, self.settings)
        self.assertIs(result.live, True)
        self.assertEqual(request.call_count, 1)
        self.assertIn("/webcast/room/web/enter/?", request.call_args.args[0])
        self.assertNotIn("Cookie", request.call_args.args[1])
        # Returning an already supplied media URL is harmless and preserves
        # recording compatibility; it is never fetched by the status probe.
        self.assertEqual(result.stream_url, "https://media.invalid/live.m3u8")

    def test_failed_status_respects_recent_page_cooldown_without_media_requests(self):
        responses = [self.response({"data": {"data": []}}),
                     (b"<html><title>verification</title></html>", {}, "https://live.douyin.com/123"),
                     ProbeError("offline network", "network_error", 30),
                     ProbeError("limited", "rate_limited", 120)]
        for response in responses:
            with self.subTest(response=type(response).__name__):
                options = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch("livewatch.providers._request", **options) as request:
                    result = self.provider.check(self.room, self.settings)
                self.assertIsNone(result.live)
                self.assertEqual(request.call_count, 1)

    def test_unsupported_high_frequency_sources_issue_no_requests(self):
        sources = [{"room_input": "@account", "input_type": "account"},
                   {"room_input": "https://www.douyin.com/user/MS4wLjABAAAA12345678"},
                   {"room_input": "https://v.douyin.com/abc/"},
                   {"room_input": "https://webcast.amemv.com/webcast/reflow/123/?sec_uid=MS4wLjABAAAA12345678"},
                   {"stream_url": "https://media.invalid/live.m3u8"}]
        for source in sources:
            with self.subTest(source=source), patch("livewatch.providers._request") as request:
                result = self.provider.check(self.room | source, self.settings)
                self.assertIsNone(result.live)
                self.assertEqual(result.error_code, "status_source_required")
                request.assert_not_called()

    def test_custom_status_requires_json_boolean(self):
        room = self.room | {"status_url": "https://status.invalid/room"}
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value), patch("livewatch.providers._request", return_value=self.response({"live": value})):
                self.assertIsNone(self.provider.check(room, self.settings).live)
        for value in (True, False):
            with self.subTest(value=value), patch("livewatch.providers._request", return_value=self.response({"live": value})):
                self.assertIs(self.provider.check(room, self.settings).live, value)

    def test_state_only_custom_source_ignores_optional_media_fields(self):
        room = self.room | {"status_url": "https://status.invalid/room"}
        with patch("livewatch.providers._request", return_value=self.response({
                "live": True, "stream_url": "invalid media url", "headers": ["invalid"]})) as request:
            result = self.provider.check(room, self.settings)
        self.assertIs(result.live, True)
        self.assertEqual(result.stream_url, "")
        self.assertEqual(request.call_count, 1)

    def test_recording_custom_source_preserves_supplied_media(self):
        room = self.room | {"status_url": "https://status.invalid/room", "record_enabled": True}
        with patch("livewatch.providers._request", return_value=self.response({
                "live": True, "stream_url": "https://media.invalid/live.m3u8"})) as request:
            result = self.provider.check(room, self.settings)
        self.assertIs(result.live, True)
        self.assertEqual(result.stream_url, "https://media.invalid/live.m3u8")
        self.assertEqual(result.stream_variants[0]['quality'],'unknown')
        self.assertEqual(result.stream_variants[0]['protocol'],'hls')
        self.assertEqual(result.stream_variants[0]['url'],result.stream_url)
        self.assertEqual(request.call_count, 1)

    def test_invalid_recording_source_does_not_erase_explicit_live_state(self):
        room=self.room | {'status_url':'https://status.invalid/room','record_enabled':True}
        for media in ({'stream_url':'invalid'}, {'stream_url':'https://media.invalid/live.flv','headers':['invalid']}):
            with self.subTest(media=media), patch('livewatch.providers._request',return_value=self.response({'live':True,**media})):
                result=self.provider.check(room,self.settings)
            self.assertIs(result.live,True)
            self.assertEqual(result.stream_url,'')
            self.assertTrue(result.message)

    def test_recording_manual_source_keeps_legacy_media_probe(self):
        room = self.room | {"stream_url": "https://media.invalid/live.m3u8", "record_enabled": True}
        response = (b"#EXTM3U\n#EXTINF:1\nsegment.ts", {}, room["stream_url"])
        with patch("livewatch.providers._request", return_value=response) as request:
            result = self.provider.check(room, self.settings)
        self.assertIs(result.live, True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[2], 4096)

    def test_expired_deadline_does_not_start_network_operation(self):
        room = self.room | {"status_url": "http://127.0.0.1:1/status"}
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS after expiry")):
            result = self.provider.check(room, self.settings | {"_probe_deadline": time.monotonic() - 1})
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code, "network_error")
        self.assertIn("network=deadline", result.diagnostic)

    def test_retry_after_parses_seconds_and_http_date(self):
        self.assertEqual(_retry_after({"Retry-After": "120"}), 120)
        self.assertEqual(_retry_after({"Retry-After": "999999"}), 86400)
        self.assertEqual(_retry_after({"Retry-After": "garbage"}), 0)
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
        self.assertGreaterEqual(_retry_after({"retry-after": future}), 89)
        self.assertLessEqual(_retry_after({"retry-after": future}), 90)

    def test_http_429_preserves_retry_after_and_unknown(self):
        error = urllib.error.HTTPError("https://status.invalid/room", 429, "limited",
                                       {"Retry-After": "180"}, io.BytesIO(b""))
        with patch("urllib.request.OpenerDirector.open", side_effect=error):
            result = self.provider.check(self.room | {"status_url": "https://status.invalid/room"}, self.settings)
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code, "rate_limited")
        self.assertEqual(result.retry_after, 180)

    def test_slow_body_has_total_deadline_not_per_byte_timeout(self):
        class Trickle(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "200")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(.04)
                except (OSError, BrokenPipeError):
                    pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            result = self.provider.check(self.room | {"status_url": f"http://127.0.0.1:{server.server_port}/status"},
                self.settings | {"_probe_deadline": started + .2})
            self.assertLess(time.monotonic() - started, .8)
            self.assertIsNone(result.live)
            self.assertEqual(result.error_code, "network_error")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
