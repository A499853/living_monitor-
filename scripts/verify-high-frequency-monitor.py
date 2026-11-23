"""150-room bounded scheduling and deterministic state-machine checks.

This is an offline scheduling test.  The provider below behaves like a status
endpoint with a bounded response time; no platform, login, or network is used.
"""
from __future__ import annotations

import threading
import time
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "app"))

from livewatch.engine import Engine
from livewatch.providers import ProbeResult
from livewatch.storage import Store


class StatusProvider:
    requires_auth = False
    adaptive_polling = True
    offline_confirmations = 2

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.starts = {}
        self.active = 0
        self.peak = 0

    def check(self, room, settings):
        self.assert_status_profile(settings)
        with self.lock:
            self.calls.append(room["id"])
            self.starts.setdefault(room['id'],[]).append(time.monotonic())
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            # Simulate a small but non-zero weak-network response time.
            time.sleep(0.04)
            return ProbeResult(False, nickname=room["nickname"])
        finally:
            with self.lock:
                self.active -= 1

    @staticmethod
    def assert_status_profile(settings):
        if not settings.get("_status_only"):
            raise AssertionError("engine did not mark status-only probe")
        if settings.get("status_request_timeout") != 4:
            raise AssertionError("unexpected status timeout")


class HighFrequencyChecks(unittest.TestCase):
    def test_150_rooms_fixed_delay_with_request_budget(self):
        with tempfile.TemporaryDirectory(prefix="livewatch-150-") as directory:
            store = Store(Path(directory) / "状态压力测试")
            provider = StatusProvider()
            engine = None
            try:
                store.update_settings({
                    "status_only_mode": True,
                    "status_poll_interval_seconds": 8,
                    "monitor_max_requests_per_second": 20,
                    "status_check_concurrency": 32,
                    "status_request_timeout": 4,
                    "monitor_batch_size": 3,
                    # The high-frequency profile must not inherit these
                    # legacy waits even when a saved installation has them.
                    "room_check_delay_seconds": 60,
                    "monitor_schedule_mode": "batch",
                    "monitor_process_isolation": False,
                    "monitor_interval_minutes": 0.5,
                    "min_free_disk_gib": 0,
                })
                rooms = []
                for index in range(150):
                    rooms.append(store.add_room({
                        "platform": "douyin",
                        "room_input": "status-room-" + str(index),
                        "status_url": "http://127.0.0.1/status/" + str(index),
                        "nickname": "status-" + str(index),
                        "monitor_enabled": True,
                        "record_enabled": False,
                    }))
                store.reorder_rooms([room["id"] for room in rooms])
                engine = Engine(store, providers=provider)
                started = time.monotonic()
                engine.start()
                deadline = started + 8.5
                while time.monotonic() < deadline:
                    with provider.lock:
                        if len(provider.calls) >= 150 and provider.active == 0:
                            break
                    time.sleep(0.01)
                elapsed = time.monotonic() - started
                with provider.lock:
                    self.assertEqual(len(provider.calls), 150)
                    self.assertEqual(len(set(provider.calls)), 150)
                    self.assertLessEqual(provider.peak, 32)
                self.assertLess(elapsed, 9.0)
                # Future callbacks update Engine metrics just after the probe
                # function releases its last slot.
                metrics_deadline = time.monotonic() + 1
                while engine.probe_successes < 150 and time.monotonic() < metrics_deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(engine.probe_successes, 150)
                self.assertEqual(engine.probe_failures, 0)
                self.assertEqual(engine.snapshot()["monitor"]["effective_concurrency"], 32)
                # Exercise the second cycle too: a fast first wave alone does
                # not prove every room is scheduled every five seconds.
                deadline=started+18
                while time.monotonic()<deadline:
                    with provider.lock:
                        if all(len(starts)>=2 for starts in provider.starts.values()): break
                    time.sleep(.01)
                with provider.lock:
                    self.assertEqual(len(provider.starts),150)
                    self.assertTrue(all(len(starts)>=2 for starts in provider.starts.values()))
                    gaps=[starts[1]-starts[0] for starts in provider.starts.values()]
                self.assertGreaterEqual(min(gaps),8.0)
                self.assertLess(max(gaps),10.0)
                # This setting limits dispatches, including both waves. Worker
                # thread entry can be delayed by the OS across a second boundary
                # and is not the instant when the scheduler reserves its slot.
                with engine.lock: dispatches=list(engine.probe_started_times)
                self.assertGreaterEqual(len(dispatches),300)
                self.assertLessEqual(max(sum(at<=other<at+1 for other in dispatches) for at in dispatches),20)
            finally:
                if engine is not None:
                    engine.close()
                store.close()


class Clock:
    def __init__(self): self.now=100.0
    def monotonic(self): return self.now


class HeldProvider:
    requires_auth=False
    adaptive_polling=True
    offline_confirmations=2

    def __init__(self):
        self.condition=threading.Condition()
        self.calls=[]
        self.active={}
        self.peak=0
        self.peak_per_room=0
        self.closed=False

    def check(self,room,settings):
        call={'room':room,'settings':settings,'release':threading.Event(),'result':ProbeResult(False)}
        with self.condition:
            self.calls.append(call)
            rid=room['id']
            self.active[rid]=self.active.get(rid,0)+1
            self.peak=max(self.peak,sum(self.active.values()))
            self.peak_per_room=max(self.peak_per_room,self.active[rid])
            if self.closed: call['release'].set()
            self.condition.notify_all()
        try:
            if not call['release'].wait(5): raise AssertionError('Held probe was not released')
            return call['result']
        finally:
            with self.condition:
                self.active[rid]-=1

    def wait_call(self,name,number=1):
        end=time.monotonic()+2
        with self.condition:
            while True:
                found=[call for call in self.calls if call['room']['nickname']==name]
                if len(found)>=number: return found[number-1]
                remaining=end-time.monotonic()
                if remaining<=0: raise AssertionError('Expected probe '+name+' #'+str(number))
                self.condition.wait(remaining)

    def close(self):
        with self.condition:
            self.closed=True
            for call in self.calls: call['release'].set()


class IndependentStatusChecks(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='livewatch-status-schedule-')
        self.addCleanup(self.temporary.cleanup)
        self.store=Store(self.temporary.name)
        self.addCleanup(self.store.close)
        self.clock=Clock()
        self.clock_patch=patch('livewatch.engine.time',self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.provider=HeldProvider()
        self.previous_logging_level=logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable,self.previous_logging_level)
        self.engine=None
        self.addCleanup(self.close_engine)

    def close_engine(self):
        self.provider.close()
        if self.engine: self.engine.close()

    def configure(self,names,concurrency=2,official=True):
        self.store.update_settings({'status_only_mode':True,'status_poll_interval_seconds':5,
                                   'status_check_concurrency':concurrency,'status_douyin_concurrency':concurrency,
                                   'monitor_max_requests_per_second':100,
                                   'monitor_schedule_mode':'batch','room_check_delay_seconds':60,
                                   'monitor_interval_minutes':1440,'sound_enabled':False,'min_free_disk_gib':0})
        rooms=[]
        for name in names:
            payload={'platform':'douyin','room_input':'test_'+name,'nickname':name}
            if not official: payload['status_url']='http://127.0.0.1/status/'+name
            rooms.append(self.store.add_room(payload))
        self.store.reorder_rooms([room['id'] for room in rooms])
        self.rooms={room['nickname']:room for room in rooms}
        self.engine=Engine(self.store,providers=self.provider)

    def finish(self,name,result=None,number=1):
        call=self.provider.wait_call(name,number)
        if result is not None: call['result']=result
        call['release'].set()
        end=time.monotonic()+2
        while time.monotonic()<end:
            with self.engine.lock:
                if call['room']['id'] not in self.engine.pending: return
            time.sleep(.005)
        self.fail('Probe callback did not finish')

    def test_healthy_timer_ignores_slow_room_batch_and_legacy_waits(self):
        self.configure(['slow','fast','next'])
        self.engine._tick()
        self.provider.wait_call('slow')
        self.finish('fast')
        self.engine._tick()
        self.finish('next')
        self.clock.now+=5
        self.engine._tick()
        self.finish('fast',number=2)
        self.engine._tick()
        self.finish('next',number=2)
        self.assertEqual(len([c for c in self.provider.calls if c['room']['nickname']=='slow']),1)
        self.assertEqual(self.provider.peak_per_room,1)

    def test_long_gap_dispatches_once_without_backlog_or_overlap(self):
        self.configure(['one'])
        self.engine._tick(); self.finish('one')
        self.clock.now+=3600
        for _ in range(20): self.engine._tick()
        self.provider.wait_call('one',2)
        self.assertEqual(len(self.provider.calls),2)
        self.finish('one',number=2)
        for _ in range(20): self.engine._tick()
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(self.provider.peak_per_room,1)

    def test_official_concurrency_can_increase_live_past_initial_executor_size(self):
        self.configure(['r'+str(i) for i in range(40)],concurrency=1)
        self.engine._tick(); self.provider.wait_call('r0')
        self.store.update_settings({'status_check_concurrency':40,'status_douyin_concurrency':40})
        self.engine.settings_changed(); self.engine._tick()
        self.provider.wait_call('r39')
        self.assertEqual(self.provider.peak,40)
        self.assertEqual(self.engine.snapshot()['monitor']['effective_concurrency'],40)

    def test_stale_snapshot_is_unknown_and_does_not_invent_offline(self):
        self.configure(['one'])
        self.engine._tick(); self.finish('one',ProbeResult(True))
        self.assertEqual(self.engine.snapshot()['rooms'][0]['live_status'],'live')
        self.clock.now+=10
        room=self.engine.snapshot()['rooms'][0]
        self.assertEqual(room['live_status'],'unknown')
        self.assertEqual(room['last_confirmed_status'],'live')
        self.assertTrue(room['status_stale'])
        self.assertFalse(any(event['kind']=='live_ended' for event in self.store.list_events()))
        self.engine._tick(); self.finish('one',ProbeResult(False),number=2)
        room=self.engine.snapshot()['rooms'][0]
        self.assertEqual(room['room_status'],'live')
        self.assertTrue(room['pending_offline_confirmation'])
        self.clock.now+=1
        self.engine._tick(); self.finish('one',ProbeResult(False),number=3)
        room=self.engine.snapshot()['rooms'][0]
        self.assertEqual(room['live_status'],'waiting')
        self.assertFalse(room['status_stale'])
        self.assertEqual(self.engine.snapshot()['monitor']['coverage_ratio'],1)

    def test_network_outage_does_not_globally_pause_other_rooms(self):
        self.configure(['a','b','c','healthy'],concurrency=3)
        self.engine._tick()
        for name in ['a','b','c']:
            self.finish(name,ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
        self.clock.now+=11
        self.engine._tick()
        self.finish('healthy',ProbeResult(True))
        self.assertEqual(self.engine.douyin_blocked_until,0)
        self.assertEqual(self.engine.snapshot()['monitor']['probe_timeouts'],3)
        self.assertEqual(self.engine.snapshot()['monitor']['stale_rooms'],3)

    def test_retry_after_still_blocks_official_requests(self):
        self.configure(['a','b','c','queued'],concurrency=3)
        self.engine._tick()
        for name in ['a','b','c']:
            self.finish(name,ProbeResult(None,error_code='rate_limited',retry_after=60,diagnostic='http=429 retry_after=60'))
        self.clock.now+=5
        self.engine._tick()
        self.assertGreaterEqual(self.engine.douyin_blocked_until,self.clock.now+55)
        self.assertEqual(len(self.provider.calls),3)

    def test_custom_source_honors_retry_after_without_diagnostic_text(self):
        self.configure(['one'],official=False)
        self.engine._tick()
        self.finish('one',ProbeResult(None,error_code='rate_limited',retry_after=60))
        self.clock.now+=5
        self.engine._tick()
        self.assertEqual(len(self.provider.calls),1)
        self.assertGreaterEqual(self.engine.due[self.rooms['one']['id']],self.clock.now+55)

    def test_restart_keeps_last_confirmation_time_but_requires_fresh_probe(self):
        self.configure(['one'])
        self.engine._tick(); self.finish('one',ProbeResult(True))
        previous=self.engine.snapshot()['rooms'][0]
        self.engine.close()
        self.engine=Engine(self.store,providers=self.provider)
        restored=self.engine.snapshot()['rooms'][0]
        self.assertEqual(restored['last_success_at'],previous['last_success_at'])
        self.assertTrue(restored['last_success_at'])
        self.assertEqual(restored['last_confirmed_status'],'live')
        self.assertEqual(restored['live_status'],'unknown')
        self.assertTrue(restored['status_stale'])

    def test_high_frequency_refresh_forces_timer_without_overlapping_room(self):
        self.configure(['one'])
        self.engine._tick(); self.finish('one')
        self.engine.refresh()
        self.engine._tick(); self.provider.wait_call('one',2)
        for _ in range(5):
            self.engine.refresh(); self.engine._tick()
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(self.provider.peak_per_room,1)

    def test_first_network_failure_marks_unknown_and_recovery_restores_freshness(self):
        self.configure(['one'])
        self.engine._tick(); self.finish('one',ProbeResult(True))
        self.engine.refresh(); self.engine._tick()
        self.finish('one',ProbeResult(None,error_code='network_error'),number=2)
        uncertain=self.engine.snapshot()['rooms'][0]
        self.assertEqual(uncertain['live_status'],'unknown')
        self.assertEqual(uncertain['last_confirmed_status'],'live')
        self.assertEqual(uncertain['status_age_seconds'],0)
        self.assertTrue(uncertain['recovering'])
        self.assertFalse(any(event['kind']=='live_ended' for event in self.store.list_events()))
        self.clock.now+=1
        self.engine._tick(); self.finish('one',ProbeResult(True),number=3)
        self.assertEqual(self.engine.snapshot()['rooms'][0]['live_status'],'live')
        self.assertFalse(self.engine.snapshot()['rooms'][0]['status_stale'])

    def test_period_begins_after_slow_request_finishes_and_reports_actual_interval(self):
        self.configure(['one'])
        self.engine._tick(); self.provider.wait_call('one')
        self.clock.now+=4
        self.finish('one',ProbeResult(True))
        self.clock.now+=4.9
        self.engine._tick()
        self.assertEqual(len(self.provider.calls),1)
        self.clock.now+=.1
        self.engine._tick(); self.finish('one',ProbeResult(True),number=2)
        room=self.engine.snapshot()['rooms'][0]
        self.assertEqual(room['last_actual_check_interval_seconds'],9)
        self.assertEqual(self.engine.snapshot()['monitor']['actual_check_interval_p95_seconds'],9)
        self.assertTrue(room['last_check_finished_at'])

    def test_error_interrupts_offline_confirmation_without_ending_session(self):
        self.configure(['one'],official=False)
        self.engine._tick(); self.finish('one',ProbeResult(True))
        self.engine.refresh(); self.engine._tick(); self.finish('one',ProbeResult(False),number=2)
        self.clock.now+=1
        self.engine._tick(); self.finish('one',ProbeResult(None,error_code='network_error'),number=3)
        room=self.engine.snapshot()['rooms'][0]
        self.assertEqual(room['room_status'],'live')
        self.assertEqual(room['live_status'],'unknown')
        self.assertFalse(room['pending_offline_confirmation'])
        self.clock.now+=1
        self.engine._tick(); self.finish('one',ProbeResult(False),number=4)
        self.assertEqual(self.engine.snapshot()['rooms'][0]['room_status'],'live')
        self.assertFalse(any(e['kind']=='live_ended' for e in self.store.list_events()))
        self.clock.now+=1
        self.engine._tick(); self.finish('one',ProbeResult(False),number=5)
        self.assertEqual(self.engine.snapshot()['rooms'][0]['room_status'],'waiting')
        self.assertEqual(sum(e['kind']=='live_ended' for e in self.store.list_events()),1)

    def test_global_request_budget_counts_fast_failures_and_retries(self):
        self.configure(['a','b','c','d'],concurrency=4)
        self.store.update_settings({'monitor_max_requests_per_second':2})
        self.engine._tick()
        self.finish('a',ProbeResult(None,error_code='network_error'))
        self.finish('b',ProbeResult(None,error_code='network_error'))
        for _ in range(10): self.engine._tick()
        self.assertEqual(len(self.provider.calls),2)
        self.clock.now+=1
        self.engine._tick()
        self.assertEqual(len(self.engine.pending),2)
        self.assertEqual(len(self.engine.rate_window),2)
        self.assertLessEqual(self.provider.peak,2)

    def test_parse_failure_preserves_confirmed_room_and_retries_in_five_seconds(self):
        self.configure(['one'])
        self.engine._tick(); self.finish('one',ProbeResult(True))
        self.engine.refresh(); self.engine._tick()
        self.finish('one',ProbeResult(None,error_code='parse_error'),number=2)
        room=self.engine.snapshot()['rooms'][0]
        self.assertEqual(room['room_status'],'live')
        self.assertEqual(room['live_status'],'unknown')
        self.assertTrue(room['last_success_at'])
        self.clock.now+=5
        self.engine._tick(); self.finish('one',ProbeResult(True),number=3)
        self.assertEqual(self.engine.snapshot()['rooms'][0]['live_status'],'live')

    def test_source_refresh_is_queued_behind_status_request_without_overlap(self):
        self.configure(['one'],official=False)
        self.engine._tick(); self.provider.wait_call('one')
        self.assertTrue(self.engine.request_sources(self.rooms['one']['id'])['queued'])
        self.assertTrue(self.engine.snapshot()['rooms'][0]['source_refresh_pending'])
        self.engine._tick()
        self.assertEqual(len(self.provider.calls),1)
        self.finish('one',ProbeResult(True))
        self.engine._tick()
        refresh=self.provider.wait_call('one',2)
        self.assertFalse(refresh['settings']['_status_only'])
        self.assertEqual(refresh['settings']['_probe_deadline']-self.clock.now,self.store.get_settings()['request_timeout'])
        self.engine.request_sources(self.rooms['one']['id'])
        self.finish('one',ProbeResult(True,stream_url='https://media.invalid/stream.m3u8'),number=2)
        self.assertFalse(self.engine.snapshot()['rooms'][0]['source_refresh_pending'])
        self.engine._tick()
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(self.provider.peak_per_room,1)

    def test_exhausted_network_recovery_ignores_legacy_advisory_retry(self):
        self.configure(['one'],official=False)
        self.engine._tick()
        self.finish('one',ProbeResult(None,error_code='network_error',retry_after=30))
        self.clock.now+=11
        self.engine._tick()
        self.clock.now+=5
        self.engine._tick(); self.finish('one',ProbeResult(True),number=2)
        self.assertEqual(self.engine.snapshot()['rooms'][0]['room_status'],'live')

    def test_disabling_then_reenabling_discards_previous_inflight_result(self):
        self.configure(['one'])
        rid=self.rooms['one']['id']
        self.engine._tick(); self.provider.wait_call('one')
        self.store.update_room(rid,{'monitor_enabled':False}); self.engine.room_changed(rid)
        self.store.update_room(rid,{'monitor_enabled':True}); self.engine.room_changed(rid)
        self.finish('one',ProbeResult(True))
        self.assertEqual(self.engine.snapshot()['rooms'][0]['room_status'],'unknown')
        self.engine._tick(); self.finish('one',ProbeResult(False),number=2)
        self.assertEqual(self.engine.snapshot()['rooms'][0]['room_status'],'waiting')
        self.assertFalse(any(e['kind']=='live_started' for e in self.store.list_events()))

    def test_empty_api_advisory_does_not_pause_fleet_after_rescue_exhaustion(self):
        self.configure(['empty','missing','temporary','healthy'],concurrency=3)
        self.engine._tick()
        for name,diagnostic in [('empty','body=empty'),('missing','body=missing_room'),('temporary','platform_status=30003')]:
            self.finish(name,ProbeResult(None,error_code='anonymous_restricted',retry_after=300,diagnostic=diagnostic+' stage=room_web_enter'))
        self.clock.now+=11
        self.engine._tick();self.finish('healthy',ProbeResult(True))
        self.assertEqual(self.engine.douyin_blocked_until,0)
        self.assertEqual(self.engine.douyin_rejections,{})
        for name in ('empty','missing','temporary'):
            self.assertLessEqual(self.engine._status_due_at(self.rooms[name]['id'],self.store.get_settings()),self.clock.now+5)
        self.clock.now+=5
        self.engine._tick()
        for name in ('empty','missing','temporary'): self.finish(name,ProbeResult(True),number=2)
        self.assertTrue(all(room['live_status']=='live' for room in self.engine.snapshot()['rooms']))
        self.assertFalse(any(event['kind']=='live_ended' for event in self.store.list_events()))

    def test_real_restrictions_and_server_retry_after_keep_cooldowns(self):
        self.configure(['a','b','c'],concurrency=3)
        for code,advice,diagnostic,minimum in [
            ('anonymous_restricted',300,'http=401 body=empty',300),
            ('anonymous_restricted',300,'http=403 body=empty',300),
            ('rate_limited',60,'http=429 body=empty',60),
            ('rate_limited',60,'http=444 body=empty',60),
            ('anonymous_restricted',120,'body=verification',120),
            ('anonymous_restricted',45,'body=empty retry_after=45',45),
            ('anonymous_restricted',45,'body=empty page_retry_after=45',45),
        ]:
            with self.subTest(diagnostic=diagnostic):
                self.engine.douyin_blocked_until=0;self.engine.douyin_rejections.clear()
                for room in self.rooms.values():
                    state=self.engine._state(room);state['failure_count']=0
                    self.engine._handle_probe_error(room,state,'restricted',code,advice,diagnostic)
                    self.assertGreaterEqual(self.engine.due[room['id']],self.clock.now+minimum)
                self.assertGreaterEqual(self.engine.douyin_blocked_until,self.clock.now+minimum)
                self.assertFalse(self.engine.recoveries)

    def test_successful_page_refresh_keeps_eight_second_checks_and_fast_offline_confirmation(self):
        self.configure(['one']);self.store.update_settings({'status_poll_interval_seconds':8})
        rid=self.rooms['one']['id']
        self.engine._tick();self.finish('one',ProbeResult(True,retry_after=30,diagnostic='stage=room_page'))
        page_deadline=self.clock.now+30
        self.assertEqual(self.engine.states[rid]['_status_page_not_before'],page_deadline)
        self.assertEqual(self.engine._status_due_at(rid,self.store.get_settings()),self.clock.now+8)
        self.clock.now+=8;self.engine._tick()
        self.assertEqual(self.provider.wait_call('one',2)['settings']['_status_page_not_before'],page_deadline)
        self.finish('one',ProbeResult(False,retry_after=30,diagnostic='stage=room_web_enter'),number=2)
        self.assertEqual(self.engine._status_due_at(rid,self.store.get_settings()),self.clock.now+1)
        self.assertTrue(self.engine.states[rid]['confirmed_live'])
        self.clock.now+=1;self.engine._tick();self.finish('one',ProbeResult(False),number=3)
        self.assertFalse(self.engine.states[rid]['confirmed_live'])

    def test_page_cooldown_diagnostic_does_not_slide_deadline(self):
        self.configure(['one']);rid=self.rooms['one']['id']
        self.engine._tick();self.finish('one',ProbeResult(True,diagnostic='stage=room_page'))
        page_deadline=self.engine.states[rid]['_status_page_not_before']
        self.clock.now+=5;self.engine._tick()
        self.finish('one',ProbeResult(None,error_code='anonymous_restricted',retry_after=300,
                                    diagnostic='stage=room_web_enter body=empty page_stage=room_page page_result=cooldown'),number=2)
        self.assertEqual(self.engine.states[rid]['_status_page_not_before'],page_deadline)
        self.clock.now+=1;self.engine._tick()
        self.assertEqual(self.provider.wait_call('one',3)['settings']['_status_page_not_before'],page_deadline)
        self.finish('one',ProbeResult(True),number=3)
        self.assertEqual(self.engine.states[rid]['_status_page_not_before'],page_deadline)

    def test_successful_status_still_respects_real_retry_after(self):
        self.configure(['one']);rid=self.rooms['one']['id']
        self.engine._tick();self.finish('one',ProbeResult(True,retry_after=30,diagnostic='stage=room_page retry_after=45'))
        self.assertEqual(self.engine.due[rid],self.clock.now+45)
        self.clock.now+=8;self.engine._tick()
        self.assertEqual(len(self.provider.calls),1)

    def test_full_profile_keeps_success_retry_hint(self):
        self.configure(['one']);self.store.update_settings({'status_only_mode':False,'room_check_delay_seconds':0})
        self.engine._tick();self.finish('one',ProbeResult(True,retry_after=30,diagnostic='stage=room_page'))
        self.assertEqual(self.engine.due[self.rooms['one']['id']],self.clock.now+30)

    def test_two_successful_trials_release_global_restriction(self):
        self.configure(['a','b','c','fresh'],concurrency=3)
        self.engine._tick()
        for name in ('a','b','c'): self.finish(name,ProbeResult(None,error_code='rate_limited',retry_after=60,diagnostic='http=444'))
        self.clock.now=self.engine.douyin_blocked_until;self.engine._tick()
        self.finish('fresh',ProbeResult(True))
        self.assertEqual(self.engine.douyin_recovery_successes,1)
        self.assertEqual(self.engine.douyin_blocked_until,self.clock.now+5)
        self.clock.now+=5;self.engine._tick()
        names=[name for name,room in self.rooms.items() if room['id'] in self.engine.pending]
        self.assertEqual(len(names),1)
        self.finish(names[0],ProbeResult(True),number=2)
        self.assertEqual(self.engine.douyin_blocked_until,0)
        self.assertEqual(self.engine.douyin_rejections,{})


if __name__ == "__main__":
    unittest.main(verbosity=2)
