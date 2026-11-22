"""Real local HTTP deadline -> recovery -> successful status update."""
from __future__ import annotations
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

spec=importlib.util.spec_from_file_location('backend_fixture',Path(__file__).with_name('verify-project.py'))
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class SlowOnce(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.lock:
            self.server.requests+=1
            number=self.server.requests
        if number==1:
            self.server.entered.set()
            time.sleep(5.5)
        body=json.dumps({'live':True}).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (OSError,ConnectionError): pass
    def log_message(self,*_): pass


class RecoveryApi(unittest.TestCase):
    def test_real_slow_response_recovers_without_false_live_transition(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),SlowOnce)
        server.daemon_threads=True
        server.lock=threading.Lock()
        server.requests=0
        server.entered=threading.Event()
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory(prefix='livewatch-recovery-api-') as directory:
            backend=fixture.Backend(Path(directory)/'isolated state')
            try:
                backend.start()
                backend.json('PUT','/api/settings',{'sound_enabled':False,'status_request_timeout':4,'status_poll_interval_seconds':8})
                backend.json('POST','/api/rooms',{'platform':'wechat','room_input':'slow-once','record_enabled':False,
                    'status_url':f'http://127.0.0.1:{server.server_port}/status'},expected=201)
                self.assertTrue(server.entered.wait(3))
                began=time.monotonic()
                states=[]
                deadline=began+12
                recovery_at=None
                while time.monotonic()<deadline:
                    state=backend.json('GET','/api/state')
                    room=state['rooms'][0]
                    states.append(room['check_state'])
                    if room['check_state']=='recovering' and recovery_at is None:
                        recovery_at=time.monotonic()-began
                        self.assertEqual(room['last_confirmed_status'],'unknown')
                        self.assertGreaterEqual(state['monitor']['probe_timeouts'],1)
                    if recovery_at is not None and room['check_state']=='idle' and room['room_status']=='live':
                        break
                    time.sleep(.08)
                self.assertIn('checking',states)
                self.assertIsNotNone(recovery_at)
                self.assertGreater(recovery_at,3.5)
                self.assertLess(recovery_at,5.0)
                self.assertEqual(room['check_state'],'idle')
                self.assertEqual(room['room_status'],'live')
                self.assertFalse(room['monitor_recovering'])
                self.assertEqual(sum(item['kind']=='live_started' for item in state['events']),1)
            finally:
                backend.stop()
                server.shutdown()
                server.server_close()
                thread.join(3)


if __name__=='__main__': unittest.main(verbosity=2)
