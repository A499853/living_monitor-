"""Verify durable local authorization using disposable backend data only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / 'app'))
spec = importlib.util.spec_from_file_location('project_verification', PROJECT / 'scripts' / 'verify-project.py')
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)

from livewatch.auth import authorization_status, authorized_monitor_cookie


class AuthorizationPersistenceChecks(unittest.TestCase):
    def test_saved_authorization_survives_settings_and_backend_restart_until_cleared(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-auth-') as directory:
            backend = verification.Backend(Path(directory) / 'state')
            self.addCleanup(backend.stop)
            backend.start()
            self.assertFalse(backend.json('GET', '/api/state')['auth']['douyin_authorized'])
            # Deliberately fictitious credentials; no rooms or external calls.
            cookie = 'sessionid=LOCAL-TEST-ONLY; ttwid=LOCAL-TEST-ONLY'
            result = backend.json('POST', '/api/auth/douyin', {'cookie': cookie})
            self.assertTrue(result['douyin_authorized'])
            backend.json('PUT', '/api/settings', {'sound_enabled': False})
            for restart in (False, True):
                if restart:
                    backend.stop()
                    backend.start()
                state = backend.json('GET', '/api/state')
                self.assertTrue(state['auth']['douyin_authorized'])
                self.assertTrue(state['auth']['local_authorization_permanent'])
                self.assertIsNone(state['auth']['expires_at'])
                self.assertTrue(state['settings']['douyin_cookie_configured'])
                self.assertNotIn('LOCAL-TEST-ONLY', json.dumps(state))
            backend.json('PUT', '/api/settings', {'douyin_cookie': ''})
            backend.stop()
            backend.start()
            state = backend.json('GET', '/api/state')
            self.assertFalse(state['auth']['douyin_authorized'])
            self.assertFalse(state['auth']['local_authorization_permanent'])
            backend.stop()

    def test_anonymous_and_invalid_credentials_do_not_grant_local_authorization(self):
        for value in ('', 'ttwid=anonymous', 'sessionid=', 'sessionid=x\r\nInjected: x', None):
            with self.subTest(value=value):
                self.assertFalse(authorization_status(value)['douyin_authorized'])
                with self.assertRaises(ValueError):
                    authorized_monitor_cookie(value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
