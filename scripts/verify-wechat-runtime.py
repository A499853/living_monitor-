"""Offline runtime lifecycle checks. All OS/process/HTTP operations are fakes.

Never runs the bundled bridge, certutil, icacls or registry writes. Temporary
directories contain only fake bridge files, a journal and a dummy PEM fixture.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from livewatch.wechat_runtime import WechatRuntime, RuntimeErrorSafe, PROXY_VALUES, WindowsIntegration, guardian_main


class FakeLock:
    def close(self): pass


class FakeProcess:
    pid = 4411

    def __init__(self, system):
        self.system = system
        self.alive = True

    def poll(self): return None if self.alive else 0

    def terminate(self):
        self.system.events.append('terminate')
        self.alive = False
        self.system.ports = {}
        self.system.record = None

    def wait(self, timeout=None): return 0


class FakeWindows:
    def __init__(self):
        self.registry = {'ProxyEnable': [1, 4], 'ProxyServer': ['127.0.0.1:7890', 1],
            'ProxyOverride': ['localhost;company.internal', 1], 'AutoConfigURL': ['http://config.invalid/proxy.pac', 1],
            'AutoDetect': None}
        self.original = copy.deepcopy(self.registry)
        self.events = []
        self.certificates = set()
        self.ports = {}
        self.record = None
        self.fail_add = False
        self.fail_remove = False
        self.fail_write = None
        self.fail_notify = False
        self.process = None

    def lock(self, path): return FakeLock()
    def secure_directory(self, path): self.events.append('acl')
    def registry_read(self): return copy.deepcopy(self.registry)
    def listener_pid(self, port): return self.ports.get(port)
    def create_job(self): return FakeLock()
    def assign_job(self, job, process): pass

    def registry_write(self, key, entry):
        self.events.append('registry:' + key)
        if self.fail_write == key:
            self.fail_write = None
            raise RuntimeErrorSafe('fake write failure')
        self.registry[key] = copy.deepcopy(entry)

    def notify_proxy(self):
        self.events.append('notify')
        if self.fail_notify:
            self.fail_notify = False
            raise RuntimeErrorSafe('fake notify failure')

    def process_key(self, pid):
        if pid == os.getpid():
            return {'pid': pid, 'image': sys.executable, 'created': '1111'}
        return copy.deepcopy(self.record)

    def terminate_record(self, record):
        if self.record == record:
            self.process.terminate()

    def certificate_present(self, fingerprint): return fingerprint in self.certificates

    def certificate_add(self, path):
        import ssl
        fingerprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(path.read_text())).hexdigest().upper()
        self.certificates.add(fingerprint)
        self.events.append('cert:add')
        if self.fail_add:
            raise RuntimeErrorSafe('fake add response failure after write')

    def certificate_remove(self, fingerprint):
        self.events.append('cert:remove')
        if self.fail_remove:
            raise RuntimeErrorSafe('fake cert remove failure')
        self.certificates.remove(fingerprint)


class FakeRuntime(WechatRuntime):
    fail_api = ''

    def _launch_guard(self):
        self.system.events.append('guard')
        return None

    def _spawn(self):
        self.system.events.append('spawn')
        self.system.process = process = FakeProcess(self.system)
        self.system.record = {'pid': process.pid, 'image': str(self.executable.resolve()), 'created': '123456789'}
        self.system.ports[2022] = process.pid
        return process

    def _api(self, path, data=None, timeout=5):
        assert self._owns_api(), 'API request sent to unrelated listener'
        self.system.events.append('api:' + path)
        if path == self.fail_api:
            raise RuntimeErrorSafe('fake API failure')
        if path.endswith('/certificate/generate'):
            assert data['install'] is False and data['restart'] is False
            directory = self.workdir / 'certs'
            cert, key = directory / 'test.pem', directory / 'test.key'
            # A dummy PEM is enough to verify fingerprint ownership plumbing;
            # tests never put this fixture in a real certificate store.
            cert.write_text('-----BEGIN CERTIFICATE-----\n' + base64.b64encode(b'fake local certificate').decode() + '\n-----END CERTIFICATE-----\n')
            key.write_text('fake key')
            return {'config': {'cert': {'file': str(cert), 'key': str(key)}}}
        if path == '/api/proxy/config':
            assert data['values']['proxy.system'] is False
            assert data['values']['proxy.skipInstallRootCert'] is True
            assert data['restart'] is False
        if path == '/api/service/start':
            assert data == {'name': 'interceptor'}
            self.system.ports[2023] = self.process.pid
        if path == '/api/service/stop':
            self.system.ports.pop(2023, None)
        return {}


class RuntimeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.system = FakeWindows()
        self.runtime = FakeRuntime(self.root, bundle_root=self.root, system=self.system)
        self.runtime.executable.parent.mkdir(parents=True)
        self.runtime.executable.write_bytes(b'not an executable')
        self.script = self.root / 'global.js'
        self.script.write_text('/* fixture */')

    def tearDown(self):
        self.runtime._watchdog_stop.set()
        self.runtime._release()
        self.temp.cleanup()

    def test_full_lifecycle_and_cleanup_order(self):
        state = self.runtime.start(self.script)
        self.assertTrue(state['running'])
        self.assertEqual(self.system.registry, PROXY_VALUES)
        self.assertEqual(len(self.system.certificates), 1)
        config = json.loads(self.runtime.config_path.read_text())
        self.assertFalse(config['proxy']['enabled'])
        self.assertFalse(config['proxy']['system'])
        self.assertTrue(config['proxy']['skipInstallRootCert'])
        before = len(self.system.events)
        self.assertFalse(self.runtime.stop()['running'])
        self.assertEqual(self.system.registry, self.system.original)
        self.assertEqual(self.system.certificates, set())
        self.assertFalse(self.runtime.journal_path.exists())
        events = self.system.events[before:]
        self.assertLess(events.index('notify'), events.index('api:/api/service/stop'))
        self.assertLess(events.index('api:/api/service/stop'), events.index('terminate'))
        self.assertLess(events.index('terminate'), events.index('cert:remove'))

    def test_duplicate_start_is_idempotent(self):
        self.runtime.start(self.script)
        self.runtime.start(self.script)
        self.assertEqual(self.system.events.count('spawn'), 1)
        self.runtime.stop()

    def test_unrelated_port_is_not_adopted(self):
        self.system.ports[2022] = 9000
        with self.assertRaises(RuntimeErrorSafe): self.runtime.start(self.script)
        self.assertNotIn('spawn', self.system.events)
        self.assertEqual(self.system.registry, self.system.original)

    def test_missing_bundle_is_safe_error(self):
        self.runtime.executable.unlink()
        with self.assertRaisesRegex(RuntimeErrorSafe, '缺少内置'): self.runtime.start(self.script)
        self.assertEqual(self.system.events, [])

    def test_changed_proxy_server_is_preserved(self):
        self.runtime.start(self.script)
        self.system.registry['ProxyServer'] = ['127.0.0.1:8888', 1]
        current = copy.deepcopy(self.system.registry)
        self.runtime.stop()
        self.assertEqual(self.system.registry, current)

    def test_changed_individual_value_is_preserved(self):
        self.runtime.start(self.script)
        self.system.registry['AutoConfigURL'] = ['http://new.invalid/config.pac', 1]
        self.runtime.stop()
        expected = self.system.original | {'AutoConfigURL': ['http://new.invalid/config.pac', 1]}
        self.assertEqual(self.system.registry, expected)

    def test_partial_registry_failure_rolls_back(self):
        self.system.fail_write = 'AutoConfigURL'
        with self.assertRaises(RuntimeErrorSafe): self.runtime.start(self.script)
        self.assertEqual(self.system.registry, self.system.original)
        self.assertEqual(self.system.certificates, set())
        self.assertFalse(self.system.process.alive)
        self.assertFalse(self.runtime.journal_path.exists())

    def test_notify_failure_rolls_back(self):
        self.system.fail_notify = True
        with self.assertRaises(RuntimeErrorSafe): self.runtime.start(self.script)
        self.assertEqual(self.system.registry, self.system.original)
        self.assertEqual(self.system.certificates, set())

    def test_uncertain_certificate_install_is_undone(self):
        self.system.fail_add = True
        with self.assertRaises(RuntimeErrorSafe): self.runtime.start(self.script)
        self.assertEqual(self.system.certificates, set())
        self.assertNotIn('registry:ProxyServer', self.system.events)

    def test_api_failure_rolls_back(self):
        self.runtime.fail_api = '/api/proxy/config'
        with self.assertRaises(RuntimeErrorSafe): self.runtime.start(self.script)
        self.assertEqual(self.system.certificates, set())
        self.assertEqual(self.system.registry, self.system.original)
        self.assertFalse(self.system.process.alive)

    def test_failed_cleanup_keeps_recovery_record(self):
        self.runtime.start(self.script)
        self.system.fail_remove = True
        with self.assertRaises(RuntimeErrorSafe): self.runtime.stop()
        self.assertTrue(self.runtime.journal_path.exists())
        self.assertEqual(self.system.registry, self.system.original)
        self.system.fail_remove = False
        recovered = FakeRuntime(self.root, bundle_root=self.root, system=self.system)
        recovered.recover()
        self.assertFalse(recovered.journal_path.exists())
        self.assertEqual(self.system.certificates, set())

    def test_crash_recovery_uses_process_identity(self):
        self.runtime.start(self.script)
        self.runtime._watchdog_stop.set()
        self.runtime._release()
        recovered = FakeRuntime(self.root, bundle_root=self.root, system=self.system)
        recovered.recover()
        self.assertEqual(self.system.registry, self.system.original)
        self.assertFalse(self.system.process.alive)
        self.assertFalse(recovered.journal_path.exists())

    def test_pid_reuse_never_terminates_unrelated_process(self):
        self.runtime.start(self.script)
        self.runtime._watchdog_stop.set()
        self.runtime._release()
        self.system.record['created'] = '987654321'
        recovered = FakeRuntime(self.root, bundle_root=self.root, system=self.system)
        recovered.recover()
        self.assertTrue(self.system.process.alive)
        self.assertEqual(self.system.registry, self.system.original)

    def test_bridge_crash_recovers_proxy_without_restart(self):
        self.runtime.start(self.script)
        self.system.process.terminate()
        self.assertTrue(self.runtime.snapshot()['recovery_pending'])
        until = time.monotonic() + 2
        while self.runtime.journal_path.exists() and time.monotonic() < until:
            time.sleep(.05)
        self.assertFalse(self.runtime.snapshot()['running'])
        self.assertFalse(self.runtime.journal_path.exists())
        self.assertEqual(self.system.registry, self.system.original)
        self.assertEqual(self.system.certificates, set())
        self.assertIn('意外退出', self.runtime.last_error)

    def test_invalid_recovery_record_causes_no_system_writes(self):
        self.runtime.workdir.mkdir(parents=True)
        self.runtime.journal_path.write_text('{"version": 1, "certificate":"not a fingerprint"}')
        with self.assertRaises(RuntimeErrorSafe): self.runtime.recover()
        self.assertEqual(self.system.events, [])

    def test_guard_recovers_after_exact_owner_exits(self):
        self.runtime.start(self.script)
        token = self.runtime.state['guard_token']
        owner_pid = self.runtime.state['owner']['pid']
        self.runtime._watchdog_stop.set()
        self.runtime._release()
        recovered = FakeRuntime(self.root, bundle_root=self.root, system=self.system)
        real_key = self.system.process_key
        with patch('livewatch.wechat_runtime.WechatRuntime', return_value=recovered), \
                patch.object(self.system, 'process_key', side_effect=lambda pid: None if pid == owner_pid else real_key(pid)):
            guardian_main(str(self.root), str(self.root), token)
        self.assertEqual(self.system.registry, self.system.original)
        self.assertFalse(self.runtime.journal_path.exists())

    def test_guard_ignores_another_activation_token(self):
        self.runtime.start(self.script)
        recovered = FakeRuntime(self.root, bundle_root=self.root, system=self.system)
        before = list(self.system.events)
        with patch('livewatch.wechat_runtime.WechatRuntime', return_value=recovered):
            guardian_main(str(self.root), str(self.root), '0' * 32)
        self.assertEqual(self.system.events, before)
        self.runtime.stop()

    def test_reparse_certificate_directory_rejected_before_acl(self):
        self.runtime.workdir.mkdir(parents=True)
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(RuntimeErrorSafe): self.runtime.start(self.script)
        self.assertEqual(self.system.events, [])

    def test_termination_access_denied_or_timeout_is_error(self):
        class Function:
            def __init__(self, value): self.value = value
            def __call__(self, *args): return self.value
        class Kernel:
            def __init__(self, opened, waited):
                self.OpenProcess = Function(opened)
                self.CloseHandle = Function(1)
                self.TerminateProcess = Function(1)
                self.WaitForSingleObject = Function(waited)
        record = {'pid': 4411, 'created': '1234', 'image': 'fixture'}
        for opened, waited in ((0, 0), (1, 258)):
            with self.subTest(opened=opened, waited=waited), \
                    patch('livewatch.wechat_runtime.ctypes.WinDLL', return_value=Kernel(opened, waited)), \
                    patch.object(WindowsIntegration, 'process_key', return_value=record):
                with self.assertRaises(RuntimeErrorSafe): WindowsIntegration.terminate_record(record)


if __name__ == '__main__':
    unittest.main(verbosity=2)
