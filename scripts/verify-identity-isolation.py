"""Offline identity-boundary checks for phone viewing and monitor traffic.

No real platform URL, account cookie, or external service is used.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import types
import sys
import unittest
import urllib.request
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "app"))

from livewatch.providers import (  # noqa: E402
    ProviderRegistry, ProbeResult, _IdentityGuard, _MonitorIdentityGuard,
    _anonymous_headers, _source_headers,
)
from livewatch.storage import Store  # noqa: E402


class IdentityIsolationChecks(unittest.TestCase):
    def test_anonymous_guard_drops_account_identity(self):
        request = urllib.request.Request(
            "https://live.douyin.com/123",
            headers={
                "Cookie": "sessionid=SECRET; sid_tt=SECOND; ttwid=PUBLIC",
                "Authorization": "Bearer SECRET",
                "User-Agent": "test",
            },
        )
        _IdentityGuard(anonymous=True, public_cookies=True).http_request(request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get("cookie"), "ttwid=PUBLIC")
        self.assertNotIn("authorization", headers)
        self.assertNotIn("SECRET", str(headers))
        self.assertEqual(_source_headers("https://live.douyin.com/123", {
            "Cookie": "sessionid=SECRET", "Authorization": "Bearer SECRET", "User-Agent": "test",
        }), {"User-Agent": "test"})
        self.assertEqual(_anonymous_headers({"Cookie": "sessionid=SECRET", "User-Agent": "test"}), {
            "User-Agent": "test",
        })

    def test_authorized_guard_scopes_cookie_to_room_endpoint(self):
        guard = _MonitorIdentityGuard("sessionid=SECRET", "123")
        allowed = urllib.request.Request("https://live.douyin.com/123", headers={"User-Agent": "test"})
        guard.http_request(allowed)
        self.assertEqual(dict(allowed.header_items()).get("Cookie"), "sessionid=SECRET")
        for url in (
            "https://live.douyin.com/456",
            "https://www.douyin.com/aweme/v1/web/user/profile/other/",
            "https://cdn.douyin.com/123.m3u8",
            "http://live.douyin.com/123",
        ):
            request = urllib.request.Request(url, headers={"Cookie": "old", "User-Agent": "test"})
            guard.http_request(request)
            self.assertNotIn("Cookie", dict(request.header_items()), url)
        api = urllib.request.Request("https://live.douyin.com/webcast/room/web/enter/", headers={"User-Agent": "test"})
        guard.http_request(api)
        self.assertEqual(dict(api.header_items()).get("Cookie"), "sessionid=SECRET")

    def test_isolation_forces_anonymous_room_probe_even_in_authorized_mode(self):
        registry = ProviderRegistry()
        captured = []
        def fake_probe(self, kind, identifier, expected, headers, opener, access="anonymous", **kwargs):
            captured.append((kind, identifier, access, opener))
            return ProbeResult(False)
        registry._douyin_anonymous = types.MethodType(fake_probe, registry)
        room = {"platform": "douyin", "room_input": "123", "input_type": "room", "request_headers": {}}
        settings = {
            "douyin_access_mode": "authorized", "douyin_cookie": "sessionid=SECRET",
            "douyin_user_agent": "account-agent", "monitor_identity_isolation": True,
        }
        registry._douyin(room, settings, {})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][2], "anonymous")
        self.assertEqual(captured[0][3].handlers[-1].__class__.__name__, "_IdentityGuard")
        captured.clear()
        settings["monitor_identity_isolation"] = False
        registry._douyin(room, settings, {})
        self.assertEqual(captured[0][2], "authorized")
        self.assertEqual(captured[0][3].handlers[-1].__class__.__name__, "_MonitorIdentityGuard")

    def test_isolated_authorized_mode_does_not_require_login(self):
        registry = ProviderRegistry()
        room = {"platform": "douyin", "room_input": "123", "input_type": "room"}
        with patch.object(registry, "_douyin_anonymous", return_value=ProbeResult(False)) as check:
            result = registry.check(room, {"douyin_access_mode": "authorized", "monitor_identity_isolation": True})
        self.assertIs(result.live, False)
        self.assertEqual(check.call_args.args[5], "anonymous")
        self.assertFalse(registry.requires_auth)

    def test_account_lookup_uses_no_login_identity_when_isolated(self):
        registry = ProviderRegistry()
        room = {"platform": "douyin", "room_input": "@account", "input_type": "account"}
        with patch.object(registry, "_resolve_account", return_value=("offline", "", {})) as lookup:
            result = registry.check(room, {"douyin_access_mode": "authorized",
                "douyin_cookie": "sessionid=SECRET", "monitor_identity_isolation": True})
        self.assertIs(result.live, False)
        headers, opener = lookup.call_args.args[2:4]
        self.assertNotIn("cookie", {key.lower() for key in headers})
        self.assertEqual(opener.handlers[-1].__class__.__name__, "_IdentityGuard")
        request = urllib.request.Request("https://www.douyin.com/aweme/v1/web/user/profile/other/",
            headers={"Cookie": "sessionid=SECRET", "Authorization": "Bearer SECRET"})
        opener.handlers[-1].http_request(request)
        self.assertNotIn("SECRET", str(request.header_items()))

    def test_high_frequency_account_does_not_contact_account_api(self):
        registry = ProviderRegistry()
        room = {"platform": "douyin", "room_input": "@account", "input_type": "account"}
        with patch("livewatch.providers._request") as request:
            result = registry.check(room, {"_status_only": True, "douyin_cookie": "sessionid=SECRET"})
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code, "status_source_required")
        request.assert_not_called()

    def test_isolation_switch_invalidates_worker_identity_state(self):
        registry = ProviderRegistry()
        settings = {"douyin_access_mode": "authorized", "douyin_cookie": "sessionid=SECRET",
                    "monitor_identity_isolation": False}
        generation = registry._auth_state(settings)
        registry._local.monitor_opener = object()
        replacement = registry._auth_state(settings | {"monitor_identity_isolation": True})
        self.assertGreater(replacement, generation)
        self.assertFalse(hasattr(registry._local, "monitor_opener"))

    def test_setting_defaults_and_validation(self):
        with tempfile.TemporaryDirectory(prefix="livewatch-identity-") as path:
            store = Store(path)
            try:
                settings = store.get_settings()
                self.assertTrue(settings["monitor_identity_isolation"])
                self.assertTrue(store.update_settings({"monitor_identity_isolation": False})["monitor_identity_isolation"] is False)
                with self.assertRaises(ValueError):
                    store.update_settings({"monitor_identity_isolation": "false"})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
