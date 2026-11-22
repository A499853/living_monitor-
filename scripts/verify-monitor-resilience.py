"""Offline checks for pacing, correlated failures and transport rebuilding."""
from __future__ import annotations
import concurrent.futures
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from livewatch.engine import Engine
from livewatch.providers import ProviderRegistry,ProbeResult,ProbeError
from livewatch.storage import Store


class FakeProvider:
    adaptive_polling=True
    paced_requests=True
    offline_confirmations=2
    def __init__(self): self.resets=0
    def check(self,*args): raise AssertionError('No network calls in this suite')
    def reset_session(self,settings): self.resets+=1


class QueuePool:
    def __init__(self): self.calls=[]; self.rebuilds=0
    def submit(self,function,room,settings):
        future=concurrent.futures.Future()
        self.calls.append((room,settings,future))
        return future
    def shutdown(self,**kwargs): pass
    def rebuild_sessions(self): self.rebuilds+=1


class ResilienceChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='livewatch-pacing-')
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name); self.addCleanup(self.store.close)
        self.store.update_settings({'sound_enabled':False,'min_free_disk_gib':0})
        self.now=100.0
        self.clock=patch('livewatch.engine.time',types.SimpleNamespace(monotonic=lambda:self.now))
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.provider=FakeProvider()
        self.engine=Engine(self.store,providers=self.provider)
        self.engine.pool.shutdown(); self.engine.metadata_pool.shutdown()
        self.engine.pool=QueuePool(); self.engine.metadata_pool=QueuePool()
        self.addCleanup(self.engine.close)
        logging.disable(logging.CRITICAL); self.addCleanup(logging.disable,logging.NOTSET)

    def rooms(self,count=4,custom=False):
        result=[]
        for index in range(count):
            payload=dict(platform='douyin',room_input='fixture'+str(index),nickname='room'+str(index))
            if custom: payload['status_url']='http://127.0.0.1/'+str(index)
            result.append(self.store.add_room(payload))
        self.store.reorder_rooms([r['id'] for r in result])
        return result

    def finish(self,result):
        room,settings,future=self.engine.pool.calls[-1]
        future.set_result(result)
        return room,settings

    @staticmethod
    def empty():
        return ProbeResult(None,error_code='anonymous_restricted',retry_after=300,
                           diagnostic='http=200 body=empty stage=room_web_enter')

    def test_official_requests_are_spread_out_and_do_not_block_custom_sources(self):
        rooms=self.rooms(3)
        custom=self.store.add_room(dict(platform='wechat',room_input='local',status_url='http://127.0.0.1/local'))
        self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),2)
        self.assertEqual({r['id'] for r,_,_ in self.engine.pool.calls},{rooms[0]['id'],custom['id']})
        self.now+=.49; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),2)
        self.now+=.01; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),3)
        self.assertTrue(self.engine.pool.calls[-1][1]['_status_single_request'])
        self.now+=3600; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),4)  # no burst after a pause

    def test_one_empty_room_backs_off_without_declaring_a_fleet_limit(self):
        room=self.rooms(1)[0]
        for attempt in range(4):
            self.engine._tick(); self.finish(self.empty())
            state=self.engine.states[room['id']]
            delay=state['_error_recheck_at']-self.now
            self.assertGreaterEqual(delay,min(60,5*2**attempt))
            self.assertLess(delay,min(60,5*2**attempt)+1)
            self.assertFalse(self.engine.recoveries)
            self.assertEqual(self.engine.douyin_blocked_until,0)
            self.now=state['_error_recheck_at']

    def test_shared_empty_responses_reduce_rate_then_rebuild_with_guard(self):
        rooms=self.rooms()
        for room in rooms:
            self.engine._state(room)['confirmed_live']=True
        for index in range(3):
            self.engine._tick(); self.finish(self.empty()); self.now+=.5
        self.assertEqual(self.engine.douyin_backoff_level,1)
        self.assertGreater(self.engine.douyin_blocked_until,self.now)
        self.assertFalse(self.engine.recoveries)
        self.now=self.engine.douyin_blocked_until
        self.engine._tick(); self.finish(self.empty())
        self.assertEqual(self.engine.douyin_backoff_level,2)
        self.assertEqual(self.provider.resets,1)
        self.assertEqual(self.engine.pool.rebuilds,1)
        self.assertEqual(self.engine.metadata_pool.rebuilds,1)
        self.assertTrue(all(s['confirmed_live'] for s in self.engine.states.values()))
        self.now=self.engine.douyin_blocked_until
        self.engine._tick(); self.finish(self.empty())
        self.assertEqual(self.provider.resets,1)  # at most once per ten minutes
        self.assertFalse(any(e['kind']=='live_ended' for e in self.store.list_events()))

    def test_manual_refresh_cannot_bypass_server_retry_after(self):
        self.rooms(3)
        for _ in range(3):
            self.engine._tick()
            self.finish(ProbeResult(None,error_code='rate_limited',retry_after=90,diagnostic='http=429 retry_after=90'))
            self.now+=.5
        until=self.engine.douyin_blocked_until
        self.assertGreater(until,self.now+89)
        count=len(self.engine.pool.calls)
        self.engine.refresh(); self.now+=5; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),count)

    def test_two_successful_trials_keep_reduced_rate(self):
        room=self.rooms(1)[0]
        self.engine._degrade_douyin(room,self.now,15,'fixture')
        self.now+=15; self.engine._tick(); self.finish(ProbeResult(False))
        self.now+=8; self.engine._tick(); self.finish(ProbeResult(False))
        self.assertEqual(self.engine.douyin_blocked_until,0)
        self.assertEqual(self.engine.douyin_backoff_level,1)
        self.assertEqual(self.engine._douyin_request_rate(self.store.get_settings()),1)
        for _ in range(10):
            self.now+=8; self.engine._tick(); self.finish(ProbeResult(False))
        self.assertEqual(self.engine.douyin_backoff_level,0)

    def test_single_room_refresh_and_source_refresh_preserve_retry_after(self):
        room=self.rooms(1)[0]
        self.engine._tick()
        self.finish(ProbeResult(None,error_code='rate_limited',retry_after=90,diagnostic='http=429 retry_after=90'))
        self.assertEqual(self.engine.douyin_blocked_until,0)
        for _ in range(5):
            self.now+=5
            self.engine.refresh([room['id']])
            self.engine.request_sources(room['id'])
            self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),1)
        self.now=self.engine.due[room['id']]
        self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),2)
        self.assertFalse(self.engine.pool.calls[-1][1]['_status_only'])

    def test_metadata_shares_platform_slot_and_receives_fair_turn(self):
        rooms=self.rooms(8)
        state=self.engine._state(rooms[-1])
        state.update(confirmed_live=True,live_session_id='123456',detection_uncertain=False,live_status='live')
        self.engine._tick()
        self.assertEqual(len(self.engine.metadata_pool.calls),1)
        self.assertEqual(len(self.engine.pool.calls),0)
        self.now+=.5; self.engine._tick()
        self.assertEqual(len(self.engine.metadata_pool.calls),1)
        self.assertEqual(len(self.engine.pool.calls),1)

    def test_bulk_auto_recording_enables_monitoring(self):
        room=self.rooms(1)[0]
        self.store.update_room(room['id'],{'monitor_enabled':False})
        self.engine.bulk('record_on',[room['id']])
        updated=self.store.get_room(room['id'])
        self.assertTrue(updated['record_enabled'])
        self.assertTrue(updated['monitor_enabled'])


class SingleRequestChecks(unittest.TestCase):
    def setUp(self):
        self.provider=ProviderRegistry()
        self.room=dict(platform='douyin',room_input='123',input_type='room')
        self.settings=dict(_status_only=True,_status_single_request=True)
        self.provider._auth_state(self.settings)
        self.provider._local.status_bootstrapped=True

    def test_empty_api_defers_page_to_another_scheduler_slot(self):
        empty=(b'',{'_livewatch_http_status':200},'https://live.douyin.com/webcast/room/web/enter/')
        with patch('livewatch.providers._request',return_value=empty) as request:
            result=self.provider.check(self.room,self.settings)
        self.assertIsNone(result.live)
        self.assertEqual(request.call_count,1)
        self.assertIn('page_result=deferred',result.diagnostic)
        with patch('livewatch.providers._request',return_value=(b'<html></html>',{},'https://live.douyin.com/123')) as request:
            self.provider.check(self.room,self.settings|{'_status_page_requested':True})
        self.assertEqual(request.call_count,1)
        self.assertEqual(request.call_args.args[0],'https://live.douyin.com/123')

    def test_444_never_triggers_immediate_page_fallback(self):
        with patch('livewatch.providers._request',side_effect=ProbeError('limited','rate_limited',60,'http=444')) as request:
            result=self.provider.check(self.room,self.settings)
        self.assertEqual(request.call_count,1)
        self.assertIsNone(result.live)
        self.assertEqual(result.retry_after,60)

    def test_full_source_rejection_does_not_trigger_html_retry(self):
        self.provider._local.bootstrapped=True
        with patch('livewatch.providers._request',side_effect=ProbeError('limited','rate_limited',60,'http=444')) as request:
            result=self.provider.check(self.room,self.settings|{'_status_only':False})
        self.assertEqual(request.call_count,1)
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code,'rate_limited')


if __name__=='__main__': unittest.main(verbosity=2)
