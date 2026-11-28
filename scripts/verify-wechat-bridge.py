"""Offline WeChat bridge checks using real loopback HTTP and temporary data.

No WeChat account, remote bridge, system proxy, or existing database is used.
"""
from __future__ import annotations

import concurrent.futures
import copy
import csv
from datetime import datetime
import http.client
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import re
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / 'app'))
from livewatch.engine import Engine
from livewatch.providers import ProviderRegistry
from livewatch.storage import Store, room_directory
from livewatch.wechat_capture import WechatCapture

spec = importlib.util.spec_from_file_location('project_checks', Path(__file__).with_name('verify-project.py'))
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)

START = 1700000000
END = 1700000600
SESSION = '18446744073709551610'
OBJECT = '18446744073709551611'
USERNAME = 'v2_test@finder'


class Bridge:
    def __init__(self):
        owner = self
        self.lock = threading.Lock()
        self.calls = []
        self.body = {}
        self.status = 200
        self.location = ''

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with owner.lock:
                    owner.calls.append((self.path, dict(self.headers)))
                    value, status, location = copy.deepcopy(owner.body), owner.status, owner.location
                body = value if isinstance(value, bytes) else json.dumps(value).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if location:
                    self.send_header('Location', location)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.set_live()

    def set_live(self, live=True):
        self.set_body({'code': 0, 'data': {'errCode': 0, 'data': {
            'baseResponse': {'Ret': 0},
            'liveInfo': {'liveId': SESSION, 'liveStatus': 1 if live else 2,
                         'streamUrl': self.origin + '/live.flv', 'startTime': START,
                         'endTime': 0 if live else END},
            'liveSdkInfo': {'liveId': SESSION, 'liveCdnUrl': self.origin + '/live.flv'},
        }}})

    def set_body(self, value, status=200, location=''):
        with self.lock:
            self.body, self.status, self.location = value, status, location

    def room(self, **values):
        return {'platform': 'wechat', 'room_input': 'wx:' + USERNAME, 'nickname': '视频号测试',
                'monitor_enabled': True, 'record_enabled': False,
                'transfer_options': {'wechat_bridge': {
                    'api_base': self.origin, 'object_id': OBJECT, 'nonce_id': 'nonce',
                    'live_id': SESSION, 'username': USERNAME}}, **values}

    def snapshot_calls(self):
        with self.lock:
            return list(self.calls)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class ProviderBridgeChecks(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge()
        self.addCleanup(self.bridge.close)
        self.provider = ProviderRegistry()

    def probe(self, room=None):
        return self.provider.check(room or self.bridge.room(), {
            '_status_only': True, '_probe_deadline': time.monotonic() + 3,
            'monitor_identity_isolation': True})

    def test_each_check_reads_active_profile_and_keeps_large_identifiers(self):
        for _ in range(2):
            result = self.probe()
            self.assertIs(result.live, True, result.message)
            self.assertEqual(result.live_session_id, SESSION)
            self.assertEqual(result.live_started_at, '2023-11-14T22:13:20+00:00')
        calls = self.bridge.snapshot_calls()
        self.assertEqual(len(calls), 2)
        for path, _headers in calls:
            parsed = urlsplit(path)
            self.assertEqual(parsed.path, '/api/channels/live/profile')
            self.assertEqual(parse_qs(parsed.query), {'oid': [OBJECT], 'nid': ['nonce'], 'id': [SESSION]})
        self.assertFalse(any('/live.flv' in path for path, _ in calls), 'status checks must not download media')

    def test_explicit_ended_response_provides_platform_end_time(self):
        self.bridge.set_live(False)
        result = self.probe()
        self.assertIs(result.live, False, result.message)
        self.assertEqual(result.live_session_id, SESSION)
        self.assertEqual(result.live_ended_at, '2023-11-14T22:23:20+00:00')

    def test_errors_and_incomplete_status_are_unknown(self):
        original = copy.deepcopy(self.bridge.body)
        bad = []
        for key, value in [('code', 1), ('code', '0')]:
            item = copy.deepcopy(original); item[key] = value; bad.append(item)
        item = copy.deepcopy(original); item['data']['errCode'] = 1; bad.append(item)
        item = copy.deepcopy(original); item['data']['data']['baseResponse']['Ret'] = 1; bad.append(item)
        item = copy.deepcopy(original); item['data']['data']['liveInfo']['liveStatus'] = 2; bad.append(item)
        item = copy.deepcopy(original); item['data']['data']['liveInfo']['liveStatus'] = '1'; bad.append(item)
        bad.extend([{}, {'code': 0, 'data': {}}, b'<html>please login</html>'])
        for index, body in enumerate(bad):
            with self.subTest(index=index):
                self.bridge.set_body(body)
                result = self.probe()
                self.assertIsNone(result.live, result.message)
        self.bridge.set_body(original, status=403)
        self.assertIsNone(self.probe().live)

    def test_other_session_cannot_replace_selected_broadcast(self):
        item = copy.deepcopy(self.bridge.body)
        item['data']['data']['liveInfo']['liveId'] = '9999999999999999999'
        item['data']['data']['liveSdkInfo']['liveId'] = '9999999999999999999'
        self.bridge.set_body(item)
        self.assertIsNone(self.probe().live)

    def test_stale_server_time_and_contradictory_end_are_unknown(self):
        original = copy.deepcopy(self.bridge.body)
        item = copy.deepcopy(original)
        item['data']['data']['serverTime'] = int(time.time()) - 600
        self.bridge.set_body(item)
        self.assertIsNone(self.probe().live)
        item = copy.deepcopy(original)
        item['data']['data']['liveInfo']['endTime'] = END
        self.bridge.set_body(item)
        self.assertIsNone(self.probe().live, 'live status with an end timestamp is inconsistent')
        item = copy.deepcopy(original)
        item['data']['data']['liveInfo'].update(liveStatus=2, endTime=START - 1)
        self.bridge.set_body(item)
        self.assertIsNone(self.probe().live, 'end before start cannot close a session')

    def test_bridge_redirect_is_rejected_without_visiting_target(self):
        target = Bridge()
        self.addCleanup(target.close)
        self.bridge.set_body({}, status=302, location=target.origin + '/redirected')
        self.assertIsNone(self.probe().live)
        self.assertEqual(target.snapshot_calls(), [])

    def test_non_loopback_bridge_and_credentials_are_rejected(self):
        for base in ('http://192.0.2.1:2022', 'http://example.invalid:2022',
                     'http://127.0.0.1.example.invalid:2022', 'http://user:pass@127.0.0.1:2022'):
            with self.subTest(base=base):
                room = self.bridge.room()
                room['transfer_options']['wechat_bridge']['api_base'] = base
                self.assertIsNone(self.probe(room).live)
        self.assertEqual(self.bridge.snapshot_calls(), [])

    def test_recording_gets_current_source_without_media_fetch(self):
        room = self.bridge.room(record_enabled=True)
        result = self.probe(room)
        self.assertIs(result.live, True, result.message)
        self.assertEqual(result.stream_url, self.bridge.origin + '/live.flv')
        self.assertTrue(result.stream_variants)
        self.assertEqual(len(self.bridge.snapshot_calls()), 1)
        updated = copy.deepcopy(self.bridge.body)
        for obj, key in [('liveInfo', 'streamUrl'), ('liveSdkInfo', 'liveCdnUrl')]:
            updated['data']['data'][obj][key] = self.bridge.origin + '/live.flv?signature=new'
        self.bridge.set_body(updated)
        result = self.probe(room)
        self.assertEqual(result.stream_url, self.bridge.origin + '/live.flv?signature=new')


class FakeRecorder:
    def __init__(self):
        self.value = None
        self.starts = []
        self.stops = 0

    def status(self, _rid):
        return self.value

    def available(self, _settings):
        return True

    def start(self, room, url, headers, settings):
        self.starts.append(url)
        self.value = dict(running=True, has_media=True, duration_seconds=3,
                          path='', task_id='bridge-test-task', last_progress=time.monotonic())

    def stop(self, _rid):
        self.stops += 1
        value, self.value = self.value, None
        return value

    def close(self):
        return []


class EngineBridgeChecks(unittest.TestCase):
    def test_rebinding_rejects_an_inflight_result_from_previous_session(self):
        bridge = Bridge()
        self.addCleanup(bridge.close)
        with tempfile.TemporaryDirectory(prefix='livewatch-wechat-rebind-') as directory:
            store = Store(directory)
            engine = None
            try:
                store.update_settings({'sound_enabled': False})
                room = store.add_room(bridge.room())
                provider = ProviderRegistry()
                engine = Engine(store, providers=provider)
                engine._state(room)
                result = provider.check(room, {'_probe_deadline': time.monotonic() + 3})
                self.assertIs(result.live, True)
                future = concurrent.futures.Future()
                future.set_result(result)
                submission = (future, engine._identity(room), engine.auth_generation)
                options = copy.deepcopy(room['transfer_options'])
                options['wechat_bridge']['live_id'] = '777'
                updated = store.update_room(room['id'], {'transfer_options': options})
                self.assertNotEqual(engine._identity(room), engine._identity(updated))
                engine.room_changed(room['id'])
                engine._accept_finished(room['id'], future, submission)
                self.assertIsNone(engine.states[room['id']]['confirmed_live'])
                self.assertIsNone(store.get_open_session(room['id']))
                self.assertFalse(any(event['kind'] == 'live_started' for event in store.list_events()))
            finally:
                if engine:
                    engine.close()
                store.close()

    def test_active_confirmations_recording_errors_and_csv(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-wechat-engine-') as directory:
            bridge = Bridge()
            store = Store(directory)
            engine = None
            try:
                store.update_settings({'sound_enabled': False, 'min_free_disk_gib': 0})
                room = store.add_room(bridge.room(record_enabled=True))
                recorder = FakeRecorder()
                provider = ProviderRegistry()
                engine = Engine(store, providers=provider, recorder=recorder)

                def finish():
                    result = provider.check(store.get_room(room['id']), {'_status_only': True,
                        '_probe_deadline': time.monotonic() + 3})
                    future = concurrent.futures.Future()
                    future.set_result(result)
                    current = store.get_room(room['id'])
                    engine._accept_finished(room['id'], future,
                        (future, engine._identity(current), engine.auth_generation))
                    return result

                self.assertIs(finish().live, True)
                self.assertEqual(len(recorder.starts), 1)
                session = store.get_open_session(room['id'])
                self.assertIsNotNone(session)
                bridge.set_live(False)
                self.assertIs(finish().live, False)
                self.assertEqual(engine.states[room['id']]['offline_checks'], 1)
                self.assertEqual(recorder.stops, 0)
                bridge.set_body({}, status=503)
                self.assertIsNone(finish().live)
                self.assertEqual(engine.states[room['id']]['offline_checks'], 0)
                self.assertEqual(recorder.stops, 0)
                self.assertEqual(store.get_open_session(room['id'])['id'], session['id'])
                bridge.set_live(False)
                finish()
                self.assertEqual(recorder.stops, 0)
                finish()
                self.assertEqual(recorder.stops, 1)
                self.assertIsNone(store.get_open_session(room['id']))
                kinds = [event['kind'] for event in store.list_events()]
                self.assertEqual(kinds.count('live_started'), 1)
                self.assertEqual(kinds.count('live_ended'), 1)
                path = Path(directory) / 'time_begin_over' / room_directory(room) / 'events.csv'
                with path.open(encoding='utf-8-sig', newline='') as handle:
                    events = list(csv.DictReader(handle))
                self.assertEqual(sum(event['event'] == 'start' for event in events), 1)
                self.assertEqual(sum(event['event'] == 'end' for event in events), 1)
                saved_session = dict(store.db.execute('SELECT * FROM sessions WHERE id=?',
                    (session['id'],)).fetchone())
                self.assertEqual(datetime.fromisoformat(saved_session['actual_started_at']).timestamp(), START)
                self.assertEqual(datetime.fromisoformat(saved_session['actual_ended_at']).timestamp(), END)
                self.assertEqual(saved_session['ended_at_source'], 'wechat.joinLive.endTime')
                self.assertEqual(len(bridge.snapshot_calls()), 5)
            finally:
                if engine:
                    engine.close()
                store.close()
                bridge.close()


class BackendBridgeChecks(unittest.TestCase):
    def test_capture_receives_only_identities_and_requires_explicit_add(self):
        bridge = Bridge()
        self.addCleanup(bridge.close)
        bridge.set_body({'code': 1})
        with tempfile.TemporaryDirectory(prefix='livewatch-wechat-capture-') as directory:
            backend = checks.Backend(Path(directory) / 'capture isolated')
            try:
                backend.start()
                backend.json('PUT', '/api/settings', {'sound_enabled': False})
                body = {'bridge_api': bridge.origin}
                self.assertEqual(backend.request('POST', '/api/wechat/capture/start', body,
                    {'X-LiveWatch-Token': 'wrong'})[0], 403)
                state = backend.json('POST', '/api/wechat/capture/start', body)
                self.assertTrue(state['running'])
                self.assertEqual(state['candidates'], [])
                script = Path(state['script_path']).read_text('utf-8')
                match = re.search(r'http://127\.0\.0\.1:[0-9]+/capture/[A-Za-z0-9_-]+', script)
                self.assertIsNotNone(match)
                endpoint = urlsplit(match.group())
                payload = {'object_id': OBJECT, 'nonce_id': 'nonce', 'live_id': SESSION,
                           'username': USERNAME, 'nickname': '捕获测试'}

                def capture(value, headers=None, path=None):
                    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
                    try:
                        request_headers = {'Content-Type': 'application/json', **(headers or {})}
                        connection.request('POST', path or endpoint.path,
                            json.dumps(value).encode('utf-8'), request_headers)
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        connection.close()

                self.assertEqual(capture(payload, {'Origin': 'https://example.invalid'})[0], 403)
                self.assertEqual(capture(payload, {'Host': 'localhost'})[0], 403)
                self.assertEqual(capture(payload, path='/capture/wrong')[0], 403)
                self.assertEqual(capture(payload | {'request_headers': {'Cookie': 'private'}})[0], 400)
                status, result = capture(payload, {'Origin': 'https://channels.weixin.qq.com'})
                self.assertEqual(status, 200)
                self.assertEqual(result['code'], 0)
                candidate_id = result['data']['id']
                state = backend.json('GET', '/api/wechat/capture')
                self.assertEqual(len(state['candidates']), 1)
                self.assertNotIn('nonce_id', state['candidates'][0])
                self.assertNotIn('object_id', state['candidates'][0])
                self.assertEqual(backend.json('GET', '/api/state')['rooms'], [])
                self.assertEqual(bridge.snapshot_calls(), [], 'capture must not count as a live probe')
                self.assertEqual(capture(payload)[1]['data']['id'], candidate_id)
                added = backend.json('POST', '/api/wechat/capture/add', {'candidate_id': candidate_id})
                rid = added['room']['id']
                self.assertFalse(added['updated'])
                deadline = time.monotonic() + 5
                while not bridge.snapshot_calls() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(bridge.snapshot_calls())
                snapshot = backend.json('GET', '/api/state')
                room = snapshot['rooms'][0]
                self.assertEqual(room['room_input'], 'wx:' + USERNAME)
                self.assertNotEqual(room['room_status'], 'live')
                self.assertFalse(any(item['kind'] == 'live_started' for item in snapshot['events']))
                backend.json('PUT', '/api/rooms/' + rid, {'monitor_enabled': False})
                updated = backend.json('POST', '/api/wechat/capture/add', {'candidate_id': candidate_id})
                self.assertTrue(updated['updated'])
                self.assertEqual(updated['room']['id'], rid)
                self.assertEqual(len(backend.json('GET', '/api/state')['rooms']), 1)
                next_payload = payload | {'object_id': '777', 'nonce_id': 'next', 'live_id': '888'}
                _, next_capture = capture(next_payload)
                next_id = next_capture['data']['id']
                self.assertNotEqual(next_id, candidate_id)
                candidates = backend.json('GET', '/api/wechat/capture')['candidates']
                self.assertEqual([item['id'] for item in candidates], [next_id])
                self.assertEqual(capture(next_payload)[1]['data']['id'], next_id)
                rebound = backend.json('POST', '/api/wechat/capture/add', {'candidate_id': next_id})
                self.assertTrue(rebound['updated'])
                self.assertEqual(rebound['room']['id'], rid)
                self.assertEqual(backend.request('POST', '/api/wechat/capture/add',
                    {'candidate_id': candidate_id})[0], 400, 'stale UI must not restore the older session')
                current_binding = backend.json('GET', '/api/room-source/' + rid)['room']['transfer_options']['wechat_bridge']
                self.assertEqual(current_binding['live_id'], '888')
                stopped = backend.json('POST', '/api/wechat/capture/stop', {})
                self.assertFalse(stopped['running'])
                self.assertEqual(stopped['candidates'], [])
                self.assertEqual(backend.request('POST', '/api/wechat/capture/add',
                    {'candidate_id': candidate_id})[0], 400)
            finally:
                backend.stop()

    def test_real_backend_process_isolation_live_offline_and_saved_binding(self):
        bridge = Bridge()
        self.addCleanup(bridge.close)
        with tempfile.TemporaryDirectory(prefix='livewatch-wechat-api-') as directory:
            root = Path(directory) / '隔离 数据'
            backend = checks.Backend(root)
            try:
                backend.start()
                backend.json('PUT', '/api/settings', {'sound_enabled': False, 'status_poll_interval_seconds': 1})
                rid = backend.json('POST', '/api/rooms', bridge.room(), expected=201)['room']['id']

                def wait_status(expected, timeout=10):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        state = backend.json('GET', '/api/state')
                        room = next(item for item in state['rooms'] if item['id'] == rid)
                        if room['room_status'] == expected and not room.get('pending_offline_confirmation'):
                            return state, room
                        time.sleep(.05)
                    self.fail(f'Backend did not reach {expected}: {room.get("last_error", "")}')

                state, room = wait_status('live')
                self.assertEqual(room['live_session_id'], SESSION)
                self.assertTrue(room['last_success_at'])
                count_before = len(bridge.snapshot_calls())
                bridge.set_live(False)
                backend.json('POST', '/api/refresh', {'ids': [rid]})
                state, room = wait_status('waiting')
                self.assertGreaterEqual(len(bridge.snapshot_calls()) - count_before, 2)
                self.assertEqual(sum(item['kind'] == 'live_started' for item in state['events']), 1)
                self.assertEqual(sum(item['kind'] == 'live_ended' for item in state['events']), 1)
                binding = backend.json('GET', '/api/room-source/' + rid)['room']['transfer_options']['wechat_bridge']
                self.assertEqual(binding['object_id'], OBJECT)
                self.assertEqual(binding['live_id'], SESSION)
                self.assertEqual(backend.stop(), 0)
                self.assertTrue(list((root / 'time_begin_over').rglob('*.csv')))
                backend.start()
                restored = backend.json('GET', '/api/room-source/' + rid)['room']['transfer_options']['wechat_bridge']
                self.assertEqual(restored, binding)
            finally:
                backend.stop()


class CaptureLifecycleChecks(unittest.TestCase):
    def test_stop_serializes_with_start_and_close_prevents_restart(self):
        with tempfile.TemporaryDirectory(prefix='livewatch-wechat-lifecycle-') as directory:
            store = Store(directory)
            engine = Engine(store, providers=object())
            capture = WechatCapture(directory, store, engine)
            entered, proceed, started = threading.Event(), threading.Event(), threading.Event()
            threads = []
            outcomes, errors = {}, []
            try:
                capture.start('http://127.0.0.1:2022')
                old_server = capture.server
                original_shutdown = old_server.shutdown

                def blocked_shutdown():
                    entered.set()
                    if not proceed.wait(3):
                        raise AssertionError('test did not release shutdown')
                    original_shutdown()

                old_server.shutdown = blocked_shutdown

                def stop():
                    try:
                        outcomes['stopped'] = capture.stop()
                    except Exception as exc:
                        errors.append(exc)

                def start():
                    try:
                        outcomes['restarted'] = capture.start('http://127.0.0.1:2022')
                        started.set()
                    except Exception as exc:
                        errors.append(exc)

                threads.append(threading.Thread(target=stop))
                threads[-1].start()
                self.assertTrue(entered.wait(2))
                threads.append(threading.Thread(target=start))
                threads[-1].start()
                self.assertFalse(started.wait(.1), 'start must wait for the previous listener to close')
                proceed.set()
                for thread in threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertFalse(outcomes['stopped']['running'])
                self.assertTrue(outcomes['restarted']['running'])
                self.assertIsNot(capture.server, old_server)
                capture.close()
                self.assertFalse(capture.snapshot()['running'])
                with self.assertRaises(ValueError):
                    capture.start('http://127.0.0.1:2022')
                capture.stop()
                with self.assertRaises(ValueError):
                    capture.start('http://127.0.0.1:2022')
            finally:
                proceed.set()
                for thread in threads:
                    thread.join(3)
                capture.close()
                engine.close()
                store.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
