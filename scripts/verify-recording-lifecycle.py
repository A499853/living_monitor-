"""Local-only media, crash recovery, selection and disk-protection checks."""
from __future__ import annotations
import functools
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'app'))
from livewatch.engine import Engine
from livewatch.maintenance import Maintenance
from livewatch.recorder import Recorder
from livewatch.storage import Store
from livewatch.providers import ProviderRegistry
FFMPEG=PROJECT/'tools'/'ffmpeg.exe'
FLAGS=getattr(subprocess,'CREATE_NO_WINDOW',0)

def command(args):
    return subprocess.run([str(item) for item in args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS,check=True)

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
    def handle(self):
        try: super().handle()
        except (ConnectionResetError,BrokenPipeError): pass

class FakeRecorder:
    def __init__(self): self.value=None;self.starts=[];self.stops=0
    def status(self,rid): return self.value
    def available(self,settings): return True
    def start(self,room,url,headers,settings):
        self.starts.append(url)
        self.value=dict(running=True,has_media=False,duration_seconds=0,path='',task_id='new-task')
    def stop(self,rid):
        self.stops+=1;previous=self.value;self.value=None;return previous
    def close(self): return []

class RecordingLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='livewatch-recording-')
        self.root=Path(self.temp.name)/'应用 % 本地目录';self.store=Store(self.root)
        self.room=self.store.add_room(dict(platform='wechat',room_input='fixture',nickname='分段 % 测试',monitor_enabled=True,record_enabled=True,stream_url='http://127.0.0.1/fixture.ts'))
        self.store.update_settings(dict(min_free_disk_gib=0,ffmpeg_path=str(FFMPEG)))
    def tearDown(self): self.store.close();self.temp.cleanup()
    def test_real_segments_decodable_and_durable(self):
        fixture=self.root/'fixture.ts'
        command([FFMPEG,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','8','-c:v','libx264','-g','20','-keyint_min','20','-sc_threshold','0','-pix_fmt','yuv420p','-threads','1','-c:a','aac','-f','mpegts',fixture])
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(self.root)))
        server.daemon_threads=True;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        recorder=Recorder(self.root,Maintenance(self.store))
        try:
            settings=dict(self.store.get_settings(),record_segment_seconds=2)
            recorder.start(self.room,f'http://127.0.0.1:{server.server_port}/fixture.ts',{},settings)
            task=recorder.jobs[self.room['id']]
            task['process'].wait(timeout=20)
            result=recorder.stop(self.room['id'])
            self.assertTrue(result['has_media']);self.assertTrue(result['last_media_write_at'])
            self.assertGreaterEqual(result['segment_count'],4)
            files=sorted((self.root/'video').rglob('*.ts'))
            self.assertEqual(len(files),result['segment_count'])
            duration=0
            for path in files:
                output=command([FFMPEG,'-hide_banner','-loglevel','error','-nostats','-progress','pipe:1','-i',path,'-f','null','-'])
                values=re.findall(rb'out_time_us=(\d+)',output.stdout)
                self.assertTrue(values);duration+=int(values[-1])/1e6
            self.assertGreater(duration,7.8);self.assertLess(duration,8.8)
            durable=self.store.get_record_task(self.room['id'])
            self.assertEqual(durable['task_id'],result['task_id'])
            self.assertEqual(durable['last_media_write_at'],result['last_media_write_at'])
            self.assertTrue(Path(durable['path']).exists())
            self.assertEqual(result['bytes_written'],sum(path.stat().st_size for path in files))
            rows=list(self.store.db.execute("SELECT active FROM owned_files WHERE kind='recording' AND size>0"))
            self.assertEqual(len(rows),len(files));self.assertTrue(all(row[0]==0 for row in rows))
        finally:
            recorder.close();server.shutdown();server.server_close();thread.join(3)
    def test_media_clock_without_disk_growth_is_not_progress(self):
        recorder=Recorder(self.root)
        path=self.root/'no-media.ts'
        job=dict(path=str(path),duration_seconds=0,closed_bytes=0,bytes_written=0,validated_duration=0,validated_bytes=0,last_progress=0,last_media_write_at='',has_media=False)
        recorder._media_progress(job,10)
        self.assertFalse(job['has_media']);self.assertEqual(job['last_progress'],0)
        path.write_bytes(bytes(188*4));recorder._media_progress(job,11)
        self.assertTrue(job['has_media']);valid=job['last_progress']
        recorder._media_progress(job,12);self.assertEqual(job['last_progress'],valid)
        path.write_bytes(bytes(188*8));recorder._media_progress(job,11)
        self.assertEqual(job['last_progress'],valid)
        recorder._media_progress(job,13);self.assertGreaterEqual(job['last_progress'],valid)
        recorder.close()
    def test_stall_requires_fresh_source_before_reconnect(self):
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(confirmed_live=True,live_status='live',record_status='recording',resolved_stream_url='http://old/source',record_started=time.monotonic()-100)
            recorder.value=dict(running=True,has_media=True,duration_seconds=80,path='part.ts',last_progress=time.monotonic()-60,last_media_write_at='2026-11-22T12:00:00+08:00',task_id='old-task')
            engine._inspect_record(self.room,state)
            self.assertEqual(recorder.stops,1);self.assertTrue(state['record_needs_source'])
            self.assertEqual(state['record_status'],'recovering')
            state['retry_at']=0;engine._start_record(self.room,state)
            self.assertEqual(recorder.starts,[])
            self.assertEqual(self.store.get_record_task(self.room['id'])['state'],'recovering')
            self.assertEqual(state['last_media_at'],'2026-11-22T12:00:00+08:00')
        finally: engine.close()
    def test_requested_quality_and_missing_variant(self):
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            room=self.store.update_room(self.room['id'],dict(record_quality='high',record_protocol='flv'))
            state=engine._state(room)
            state.update(confirmed_live=True,live_status='live',resolved_stream_url='http://best/source',stream_variants=[dict(quality='high',protocol='flv',url='http://chosen/source')])
            engine._start_record(room,state);self.assertEqual(recorder.starts,['http://chosen/source'])
            recorder.value=None;state['stream_variants']=[]
            engine._start_record(room,state);self.assertEqual(len(recorder.starts),1)
            self.assertTrue(state['record_needs_source'])
        finally: engine.close()
    def test_rebuild_and_stop_keep_last_write(self):
        self.store.save_record_task(self.room['id'],dict(task_id='interrupted-task',state='recording',path='segment.ts',duration_seconds=12,last_media_write_at='2026-11-22T12:00:00+08:00',bytes_written=1234))
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            self.assertEqual(state['record_status'],'recovering');self.assertEqual(state['record_task_id'],'interrupted-task')
            self.assertEqual(state['resolved_stream_url'],'')
            self.assertEqual(state['record_path'],'segment.ts');self.assertEqual(state['duration_seconds'],12)
            self.assertTrue(any(event['kind']=='record_rebuilt' for event in self.store.list_events()))
            room=self.store.update_room(self.room['id'],dict(record_enabled=False))
            engine._stop_record(room['id'],room)
            self.assertEqual(state['record_status'],'stopped')
            self.assertEqual(state['last_media_at'],'2026-11-22T12:00:00+08:00')
        finally: engine.close()
    def test_detection_error_keeps_recording_and_monitor_off_cancels_recovery(self):
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(record_status='recording',record_task_id='active-task',confirmed_live=True,live_status='live')
            recorder.value=dict(running=True,has_media=True,duration_seconds=2,path='current.ts',last_progress=time.monotonic(),task_id='active-task')
            engine._handle_probe_error(self.room,state,'network timeout','network_error',0,'network=timeout')
            self.assertEqual(recorder.stops,0);self.assertEqual(state['record_status'],'recording');self.assertTrue(state['confirmed_live'])
            engine._record_failure(self.room,state,'record interrupted')
            self.store.update_room(self.room['id'],dict(monitor_enabled=False))
            engine.room_changed(self.room['id'])
            self.assertNotIn('record_needs_source',state);self.assertNotIn('record_recheck_at',state)
            self.assertNotIn(self.room['id'],engine.recoveries)
            self.assertEqual(state['record_status'],'stopped');self.assertEqual(recorder.stops,1)
        finally: engine.close()
    def test_old_stop_result_cannot_replace_current_task(self):
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(record_status='recording',record_task_id='new-task',record_path='new.ts',duration_seconds=3,last_media_at='new-write')
            engine._record_stopped(self.room['id'],self.room,dict(task_id='old-task',path='old.ts',duration_seconds=90,has_media=True,last_media_write_at='old-write'),False)
            self.assertEqual(state['record_task_id'],'new-task');self.assertEqual(state['record_path'],'new.ts')
            self.assertEqual(state['duration_seconds'],3);self.assertEqual(state['last_media_at'],'new-write')
            self.assertEqual(state['record_status'],'recording')
        finally: engine.close()
    def test_record_recovery_is_fast_but_access_restrictions_back_off(self):
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            for expected in (2,4,8,16,30,30,30):
                before=time.monotonic();engine._record_failure(self.room,state,'temporary connection loss')
                delay=state['retry_at']-before
                self.assertGreaterEqual(delay,expected);self.assertLess(delay,expected+.1)
            for code,minimum in ((403,60),(429,120),(444,120)):
                before=time.monotonic();engine._record_failure(self.room,state,f'HTTP error {code}')
                self.assertGreaterEqual(state['retry_at']-before,minimum)
        finally: engine.close()
    def test_disk_and_concurrency_block_before_process_creation(self):
        recorder=Recorder(self.root,Maintenance(self.store))
        try:
            settings=dict(self.store.get_settings(),min_free_disk_gib=1048576)
            with self.assertRaisesRegex(ValueError,'磁盘'): recorder.start(self.room,'http://127.0.0.1/source',{},settings)
            self.assertFalse(recorder.jobs)
            recorder.jobs['existing']=object()
            with self.assertRaisesRegex(ValueError,'并发'): recorder.start(self.room,'http://127.0.0.1/source',{},dict(self.store.get_settings(),max_recordings=1))
            recorder.jobs.clear()
        finally: recorder.close()
    def test_settings_reject_unbounded_values(self):
        for key,value in [('record_segment_seconds',0),('record_stall_seconds',float('nan')),('record_start_timeout_seconds',10000),('monitor_max_requests_per_second',0)]:
            with self.assertRaises(ValueError): self.store.update_settings({key:value})
    def test_task_database_failure_does_not_leak_finished_process(self):
        recorder=Recorder(self.root)
        path=self.root/'done.ts';path.write_bytes(bytes(188))
        class FinishedProcess:
            pid=123
            def poll(self): return 0
        job=dict(process=FinishedProcess(),path=str(path),duration_seconds=1,error='',has_media=True,stopping=False,last_progress=time.monotonic(),last_media_write_at='2026-11-22T12:00:00+08:00',bytes_written=188,segment_count=1,task_id='finished')
        recorder.jobs[self.room['id']]=job
        def unavailable(*args,**kwargs): raise sqlite3.OperationalError('disk full')
        recorder._save_task=unavailable
        try:
            result=recorder.stop(self.room['id'])
            self.assertIn('暂时无法保存',result['log_error'])
            self.assertIsNone(recorder.status(self.room['id']))
            self.assertTrue(path.exists())
        finally: recorder.close()
    def test_real_disconnect_rechecks_status_and_creates_new_segment(self):
        fixture=self.root/'fixture.ts'
        command([FFMPEG,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x90:rate=10','-t','2','-c:v','libx264','-g','10','-pix_fmt','yuv420p','-threads','1','-f','mpegts',fixture])
        media=fixture.read_bytes();counts={'status':0,'media':[]}
        class Handler(QuietHandler):
            def do_GET(self):
                if self.path=='/status':
                    counts['status']+=1
                    body=json.dumps(dict(live=True,stream_url=f'http://127.0.0.1:{self.server.server_port}/media/{counts["status"]}')).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                else:
                    counts['media'].append(self.path)
                    self.send_response(200);self.send_header('Content-Length',str(len(media)));self.end_headers();self.wfile.write(media)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.store.update_room(self.room['id'],dict(stream_url='',status_url=f'http://127.0.0.1:{server.server_port}/status'))
        self.store.update_settings(dict(status_poll_interval_seconds=60))
        engine=Engine(self.store,providers=ProviderRegistry())
        try:
            engine.start();deadline=time.monotonic()+15
            while time.monotonic()<deadline and len(counts['media'])<2: time.sleep(.05)
            self.assertGreaterEqual(len(counts['media']),2)
            self.assertNotEqual(counts['media'][0],counts['media'][1])
            self.assertGreaterEqual(counts['status'],2)
            self.assertGreaterEqual(engine.states[self.room['id']]['record_recovery_count'],1)
            self.assertTrue(engine.states[self.room['id']]['last_media_at'])
            self.assertFalse(any(event['kind']=='live_ended' for event in self.store.list_events()))
        finally:
            engine.close();server.shutdown();server.server_close();thread.join(3)
        self.assertGreaterEqual(len(list((self.root/'video').rglob('*.ts'))),2)
    @unittest.skipUnless(os.name=='nt','Windows process ownership')
    def test_backend_crash_terminates_owned_ffmpeg(self):
        fixture=self.root/'fixture.ts'
        command([FFMPEG,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x90:rate=10','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-threads','1','-f','mpegts',fixture])
        media=fixture.read_bytes();stop=threading.Event()
        class Handler(QuietHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers()
                try:
                    while not stop.is_set():
                        self.wfile.write(media);self.wfile.flush();stop.wait(.1)
                except OSError: pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        marker=self.root/'owned-process.json'
        script="""import json,sys,threading
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from livewatch.recorder import Recorder
root,binary,url,marker=sys.argv[2:]
rec=Recorder(root)
room=dict(id='crash-owned',platform='wechat',room_input='owned',nickname='Crash')
rec.start(room,url,{},dict(ffmpeg_path=binary))
Path(marker).write_text(json.dumps(dict(pid=rec.jobs[room['id']]['process'].pid)))
threading.Event().wait(60)
"""
        child=subprocess.Popen([sys.executable,'-c',script,str(PROJECT/'app'),str(self.root/'child'),str(FFMPEG),f'http://127.0.0.1:{server.server_port}/media',str(marker)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=FLAGS)
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and not marker.exists() and child.poll() is None: time.sleep(.05)
            self.assertTrue(marker.exists())
            pid=json.loads(marker.read_text())['pid'];self.assertTrue(Maintenance._process_alive(pid))
            child.kill();child.communicate(timeout=5)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and Maintenance._process_alive(pid): time.sleep(.05)
            self.assertFalse(Maintenance._process_alive(pid))
        finally:
            if child.poll() is None: child.kill()
            child.communicate(timeout=5);stop.set();server.shutdown();server.server_close();thread.join(3)

if __name__=='__main__': unittest.main(verbosity=2)
