"""Local-only media, crash recovery, selection and disk-protection checks."""
from __future__ import annotations
import concurrent.futures
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
from unittest import mock
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

def video_pes_timestamps(media):
    """Read actual MPEG-TS video timestamps without requiring ffprobe."""
    def timestamp(value):
        return ((value[0]>>1&7)<<30)|(value[1]<<22)|((value[2]>>1)<<15)|(value[3]<<7)|(value[4]>>1)
    values=[]
    for offset in range(0,len(media)-187,188):
        packet=media[offset:offset+188]
        if packet[0]!=0x47 or not packet[1]&0x40: continue
        control=packet[3]>>4&3
        if control not in (1,3): continue
        start=4 if control==1 else 5+packet[4]
        payload=packet[start:]
        if len(payload)<14 or payload[:3]!=b'\x00\x00\x01' or not 0xe0<=payload[3]<=0xef: continue
        flags=payload[7]>>6
        if flags not in (2,3): continue
        pts=timestamp(payload[9:14])
        dts=timestamp(payload[14:19]) if flags==3 and len(payload)>=19 else pts
        values.append((pts,dts))
    return values

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
    def handle(self):
        try: super().handle()
        except (ConnectionResetError,BrokenPipeError): pass

class FakeRecorder:
    def __init__(self): self.value=None;self.starts=[];self.start_settings=[];self.stops=0
    def status(self,rid): return self.value
    def available(self,settings): return True
    def start(self,room,url,headers,settings):
        self.starts.append(url)
        self.start_settings.append(dict(settings))
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
            settings=dict(self.store.get_settings(),record_segment_enabled=True,record_segment_seconds=2)
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
    def test_continuous_recording_crosses_previous_half_hour_boundary(self):
        fixture=self.root/'half-hour-fixture.ts'
        # Media time advances past thirty minutes without spending thirty
        # minutes on a realtime input; all output still goes through FFmpeg.
        command([FFMPEG,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=32x32:rate=1','-t','1862','-c:v','libx264','-g','60','-pix_fmt','yuv420p','-threads','1','-f','mpegts',fixture])
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(self.root)))
        server.daemon_threads=True;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        recorder=Recorder(self.root,Maintenance(self.store))
        try:
            recorder.start(self.room,f'http://127.0.0.1:{server.server_port}/half-hour-fixture.ts',{},self.store.get_settings())
            task=recorder.jobs[self.room['id']]
            task['process'].wait(timeout=20)
            result=recorder.stop(self.room['id'])
            files=list((self.root/'video').rglob('*.ts'))
            self.assertEqual(len(files),1)
            self.assertEqual(result['segment_count'],1)
            self.assertGreater(result['duration_seconds'],1800)
            output=command([FFMPEG,'-hide_banner','-loglevel','error','-nostats','-progress','pipe:1','-i',files[0],'-f','null','-'])
            values=re.findall(rb'out_time_us=(\d+)',output.stdout)
            self.assertTrue(values)
            self.assertGreater(int(values[-1])/1e6,1860)
            self.assertEqual(result['bytes_written'],files[0].stat().st_size)
        finally:
            recorder.close();server.shutdown();server.server_close();thread.join(3)
    def test_real_continuous_recording_keeps_one_file_and_is_durable(self):
        fixture=self.root/'continuous-fixture.ts'
        command([FFMPEG,'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=160x90:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','8','-c:v','libx264','-g','20','-keyint_min','20','-sc_threshold','0','-pix_fmt','yuv420p','-threads','1','-c:a','aac','-f','mpegts',fixture])
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(self.root)))
        server.daemon_threads=True;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        recorder=Recorder(self.root,Maintenance(self.store))
        try:
            # Leave the old cut interval shorter than the input: disabling the
            # mode must bypass segmentation rather than merely extend a limit.
            settings=dict(self.store.get_settings(),record_segment_enabled=False,record_segment_seconds=2)
            recorder.start(self.room,f'http://127.0.0.1:{server.server_port}/continuous-fixture.ts',{},settings)
            task=recorder.jobs[self.room['id']]
            initial_path=task['path']
            task['process'].wait(timeout=20)
            result=recorder.stop(self.room['id'])
            files=list((self.root/'video').rglob('*.ts'))
            self.assertEqual(len(files),1)
            self.assertEqual(str(files[0]),initial_path)
            self.assertEqual(result['path'],initial_path)
            self.assertTrue(result['has_media']);self.assertTrue(result['last_media_write_at'])
            self.assertEqual(result['segment_count'],1)
            output=command([FFMPEG,'-hide_banner','-loglevel','error','-nostats','-progress','pipe:1','-i',files[0],'-f','null','-'])
            values=re.findall(rb'out_time_us=(\d+)',output.stdout)
            self.assertTrue(values)
            duration=int(values[-1])/1e6
            self.assertGreater(duration,7.8);self.assertLess(duration,8.8)
            self.assertGreater(result['duration_seconds'],7.8)
            self.assertEqual(result['bytes_written'],files[0].stat().st_size)
            durable=self.store.get_record_task(self.room['id'])
            for key in ('task_id','path','last_media_write_at','segment_count','bytes_written'):
                self.assertEqual(durable[key],result[key])
            row=self.store.db.execute("SELECT active,size FROM owned_files WHERE kind='recording'").fetchone()
            self.assertIsNotNone(row);self.assertEqual(row['active'],0)
            self.assertEqual(row['size'],files[0].stat().st_size)
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
    def test_record_until_offline_has_no_duration_deadline(self):
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(confirmed_live=True,live_status='live',resolved_stream_url='http://current/source')
            engine._start_record(self.room,state)
            self.assertFalse(state.get('record_deadline_at'))
            self.assertFalse(recorder.start_settings[-1]['record_duration_enabled'])
            self.assertFalse(recorder.start_settings[-1]['record_segment_enabled'])
            recorder.value.update(has_media=True,duration_seconds=1862,last_progress=time.monotonic())
            engine._inspect_record(self.room,state)
            self.assertEqual(recorder.stops,0)
            self.assertEqual(state['record_status'],'recording')
            self.assertTrue(self.store.get_room(self.room['id'])['record_enabled'])
        finally: engine.close()
    def test_timed_recording_keeps_deadline_across_recovery_and_stays_stopped(self):
        self.store.update_settings(dict(record_duration_enabled=True,record_duration_seconds=60))
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(confirmed_live=True,live_status='live',resolved_stream_url='http://first/source')
            with mock.patch('livewatch.engine.time.time',return_value=1000):
                engine._start_record(self.room,state)
            self.assertEqual(state['record_deadline_at'],1060)
            original=dict(state['record_options'])
            # Changing global settings must not restart a timer during a
            # connection recovery, or change an already active output mode.
            self.store.update_settings(dict(record_duration_enabled=False,record_duration_seconds=120,record_segment_enabled=True))
            engine._record_failure(self.room,state,'temporary connection loss')
            recorder.value=None
            state.pop('record_needs_source',None);state['retry_at']=0
            state['resolved_stream_url']='http://fresh/source'
            with mock.patch('livewatch.engine.time.time',return_value=1030):
                engine._start_record(self.room,state)
            self.assertEqual(state['record_deadline_at'],1060)
            self.assertEqual(state['record_options'],original)
            self.assertFalse(recorder.start_settings[-1]['record_segment_enabled'])
            self.assertTrue(recorder.start_settings[-1]['record_duration_enabled'])
            with mock.patch('livewatch.engine.time.time',return_value=1059):
                self.assertFalse(engine._expire_record_limit(self.room,state))
            with mock.patch('livewatch.engine.time.time',return_value=1061):
                engine._inspect_record(self.room,state)
            self.assertEqual(recorder.stops,1)
            self.assertFalse(self.store.get_room(self.room['id'])['record_enabled'])
            self.assertTrue(self.store.get_room(self.room['id'])['monitor_enabled'])
            self.assertFalse(state.get('record_deadline_at'))
            self.assertIsNone(state['record_options'])
            self.assertEqual(state['record_status'],'stopped')
            engine._start_record(dict(self.room,record_enabled=True),state)
            self.assertEqual(len(recorder.starts),2,'a late live result must not restart expired recording')
            events=[event for event in self.store.list_events() if event['kind']=='record_duration_reached']
            self.assertEqual(len(events),1)
            self.store.update_settings(dict(record_duration_enabled=True,record_duration_seconds=120))
            self.store.update_room(self.room['id'],dict(record_enabled=True))
            with mock.patch('livewatch.engine.time.time',return_value=1100):
                engine.room_changed(self.room['id'])
            self.assertEqual(len(recorder.starts),3)
            self.assertEqual(state['record_deadline_at'],1220)
            self.assertTrue(recorder.start_settings[-1]['record_segment_enabled'])
        finally: engine.close()
    def test_timed_recording_restart_preserves_deadline_and_expires_before_reconnect(self):
        self.store.update_settings(dict(record_duration_enabled=True,record_duration_seconds=60))
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(confirmed_live=True,live_status='live',resolved_stream_url='http://first/source')
            with mock.patch('livewatch.engine.time.time',return_value=1000):
                engine._start_record(self.room,state)
            options=dict(state['record_options'])
        finally: engine.close()
        self.store.close();self.store=Store(self.root)
        self.store.update_settings(dict(record_duration_enabled=False,record_segment_enabled=True))
        recorder=FakeRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            room=self.store.get_room(self.room['id']);state=engine._state(room)
            self.assertEqual(state['record_deadline_at'],1060)
            self.assertEqual(state['record_options'],options)
            state.update(confirmed_live=True,live_status='live',resolved_stream_url='http://fresh/source')
            with mock.patch('livewatch.engine.time.time',return_value=1061):
                engine._start_record(room,state)
            self.assertEqual(recorder.starts,[])
            self.assertFalse(self.store.get_room(room['id'])['record_enabled'])
            self.assertFalse(self.store.get_runtime(room['id'])['record_deadline_at'])
        finally: engine.close()
    def test_timed_recording_reenable_waits_for_async_save_before_new_deadline(self):
        class SavingRecorder(FakeRecorder):
            def __init__(self):
                super().__init__();self.stop_future=concurrent.futures.Future();self.stop_requests=0
            def request_stop(self,rid):
                self.stop_requests+=1
                return self.stop_future
        self.store.update_settings(dict(record_duration_enabled=True,record_duration_seconds=60))
        recorder=SavingRecorder();engine=Engine(self.store,providers=object(),recorder=recorder)
        try:
            state=engine._state(self.room)
            state.update(confirmed_live=True,live_status='live',resolved_stream_url='http://current/source')
            with mock.patch('livewatch.engine.time.time',return_value=1000):
                engine._start_record(self.room,state)
            recorder.value.update(has_media=True,duration_seconds=60,path='old.ts',last_progress=time.monotonic())
            state['record_status']='recording'
            with mock.patch('livewatch.engine.time.time',return_value=1061):
                engine._inspect_record(self.room,state)
            self.assertFalse(self.store.get_room(self.room['id'])['record_enabled'])
            self.assertEqual(state['record_status'],'stopping')
            self.assertIn(self.room['id'],engine.record_stop_tasks)
            self.assertFalse(recorder.stop_future.done())
            engine._start_record(dict(self.room,record_enabled=True),state)
            self.assertEqual(len(recorder.starts),1)
            # A deliberate enable is allowed, but the existing FFmpeg still
            # owns its output until the asynchronous save actually finishes.
            self.store.update_settings(dict(record_duration_seconds=120,record_segment_enabled=True))
            self.store.update_room(self.room['id'],dict(record_enabled=True))
            with mock.patch('livewatch.engine.time.time',return_value=1070):
                engine.room_changed(self.room['id'])
            self.assertEqual(len(recorder.starts),1)
            self.assertEqual(state['record_status'],'stopping')
            self.assertFalse(state.get('record_deadline_at'))
            self.assertIsNone(state['record_options'])
            self.assertEqual(recorder.stop_requests,1)
            finished=dict(recorder.value,running=False,stopping=True)
            recorder.value=None
            recorder.stop_future.set_result(finished)
            engine._drain_record_stops()
            self.assertNotIn(self.room['id'],engine.record_stop_tasks)
            with mock.patch('livewatch.engine.time.time',return_value=1080):
                engine._start_record(self.store.get_room(self.room['id']),state)
            self.assertEqual(len(recorder.starts),2)
            self.assertEqual(state['record_deadline_at'],1200)
            self.assertTrue(state['record_options']['record_segment_enabled'])
            self.assertEqual(state['record_options']['record_duration_seconds'],120)
        finally: engine.close()
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
    def test_real_disconnect_rechecks_source_and_resumes_same_file(self):
        self._assert_real_source_recovery_preserves_file(stalled=False)
    def test_real_stall_rechecks_source_and_resumes_same_file(self):
        self._assert_real_source_recovery_preserves_file(stalled=True)
    def _assert_real_source_recovery_preserves_file(self,stalled):
        media=[]
        for color,duration in [('red',12),('blue',14)]:
            fixture=self.root/(color+'.ts')
            command([FFMPEG,'-hide_banner','-loglevel','error','-f','lavfi','-i',f'color={color}:size=160x90:rate=10','-t',str(duration),'-c:v','libx264','-g','10','-bf','0','-pix_fmt','yuv420p','-threads','1','-f','mpegts',fixture])
            media.append(fixture.read_bytes())
        first_eof=threading.Event();finished=threading.Event()
        counts={'status':0,'media':[]}
        class Handler(QuietHandler):
            def do_GET(self):
                if self.path=='/status':
                    counts['status']+=1
                    body=json.dumps(dict(live=True,stream_url=f'http://127.0.0.1:{self.server.server_port}/media/{counts["status"]}')).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                else:
                    counts['media'].append(self.path)
                    attempt=int(self.path.rsplit('/',1)[-1])
                    body=media[min(attempt,2)-1]
                    self.send_response(200);self.send_header('Content-Type','video/mp2t');self.send_header('Content-Length',str(len(body)+188));self.send_header('Accept-Ranges','none');self.end_headers()
                    try:
                        self.wfile.write(body);self.wfile.flush()
                        # Keep the input open after delivering meaningful
                        # media. One withheld null packet makes release a
                        # normal EOF, rather than an incomplete HTTP response
                        # that FFmpeg correctly retries inside the connection.
                        (first_eof if attempt==1 else finished).wait(35)
                        self.wfile.write(b'\x47\x1f\xff\x10'+b'\xff'*184);self.wfile.flush()
                    except OSError: pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.store.update_room(self.room['id'],dict(stream_url='',status_url=f'http://127.0.0.1:{server.server_port}/status'))
        self.store.update_settings(dict(status_poll_interval_seconds=60))
        engine=Engine(self.store,providers=ProviderRegistry())
        protected_gap=[False]
        def wait_for(predicate,timeout,message):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if predicate(): return
                time.sleep(.05)
            status=engine.recorder.status(self.room['id']) or {}
            state=engine.states[self.room['id']]
            requests=dict(status=counts['status'],media=counts['media'][:8],media_requests=len(counts['media']))
            self.fail(f'{message}; requests={requests}, status={status}, record_state={state.get("record_status")}, recovery={state.get("record_recovery_count")}')
        try:
            engine.start()
            wait_for(lambda: (engine.recorder.status(self.room['id']) or {}).get('has_media'),10,'first source did not write media')
            initial=engine.recorder.status(self.room['id'])
            path=Path(initial['path']);task_id=initial['task_id'];prefix=path.read_bytes()
            self.assertGreater(len(prefix),188)
            if stalled:
                with engine.lock,engine.recorder.lock:
                    engine.recorder.jobs[self.room['id']]['last_progress']=time.monotonic()-60
                    engine._inspect_record(self.store.get_room(self.room['id']),engine.states[self.room['id']])
            else: first_eof.set()
            def reacquired_source():
                state=engine.states[self.room['id']]
                if state.get('record_recovery_count',0):
                    status=engine.recorder.status(self.room['id']) or {}
                    with self.store.lock:
                        row=self.store.db.execute("SELECT active FROM owned_files WHERE kind='recording'").fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row['active'],1,'recovery released the output file for cleanup')
                    if not status.get('running') or status.get('stopping'): protected_gap[0]=True
                return len(set(counts['media']))>=2
            wait_for(reacquired_source,20,'recording did not acquire a new source')
            self.assertTrue(protected_gap[0],'the test did not observe the gap between input connections')
            wait_for(lambda: (engine.recorder.status(self.room['id']) or {}).get('bytes_written',0)>len(prefix)+188,10,'recovered source did not append media')
            recovered=engine.recorder.status(self.room['id'])
            self.assertEqual(recovered['task_id'],task_id)
            self.assertEqual(recovered['path'],str(path))
            self.assertGreater(recovered['duration_seconds'],initial['duration_seconds'])
            self.assertEqual(path.read_bytes()[:len(prefix)],prefix,'recovery overwrote already recorded media')
            self.assertGreaterEqual(len(set(counts['media'])),2)
            self.assertGreaterEqual(counts['status'],2)
            self.assertGreaterEqual(engine.states[self.room['id']]['record_recovery_count'],1)
            self.assertTrue(engine.states[self.room['id']]['last_media_at'])
            self.assertFalse(any(event['kind']=='live_ended' for event in self.store.list_events()))
            finished.set()
        finally:
            first_eof.set();finished.set()
            engine.close();server.shutdown();server.server_close();thread.join(3)
        files=list((self.root/'video').rglob('*.ts'))
        self.assertEqual(files,[path])
        final=path.read_bytes()
        self.assertEqual(final[:len(prefix)],prefix)
        decoded=command([FFMPEG,'-hide_banner','-loglevel','error','-nostats','-progress','pipe:1','-i',path,'-f','null','-'])
        self.assertFalse(decoded.stderr,decoded.stderr.decode('utf-8','replace'))
        values=re.findall(rb'out_time_us=(\d+)',decoded.stdout)
        self.assertTrue(values);self.assertGreater(int(values[-1])/1e6,24)
        self.assertLess(int(values[-1])/1e6,27,f'HTTP requests={counts}, timestamps={video_pes_timestamps(final)[:2]}..{video_pes_timestamps(final)[-2:]}')
        pixels=command([FFMPEG,'-hide_banner','-loglevel','error','-i',path,'-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-']).stdout
        colors=[tuple(pixels[index:index+3]) for index in range(0,len(pixels)-2,3)]
        red=[index for index,(r,g,b) in enumerate(colors) if r>180 and g<60 and b<60]
        blue=[index for index,(r,g,b) in enumerate(colors) if b>180 and g<60 and r<60]
        self.assertGreater(len(red),100);self.assertGreater(len(blue),120)
        self.assertLess(max(red),min(blue),'the saved video lost the order of source media')
        timestamps=video_pes_timestamps(final)
        self.assertGreater(len(timestamps),240)
        for previous,current in zip(timestamps,timestamps[1:]):
            self.assertGreater(current[0],previous[0],'video PTS moved backwards across recovery')
            self.assertGreater(current[1],previous[1],'video DTS moved backwards across recovery')
        durable=self.store.get_record_task(self.room['id'])
        self.assertEqual(durable['task_id'],task_id)
        self.assertEqual(durable['path'],str(path))
        self.assertEqual(durable['bytes_written'],len(final))
        self.assertGreater(durable['duration_seconds'],24)
        row=self.store.db.execute("SELECT active,size FROM owned_files WHERE kind='recording'").fetchone()
        self.assertEqual(row['active'],0);self.assertEqual(row['size'],len(final))
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
