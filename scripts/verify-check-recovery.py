"""Offline configurable watchdog, late-result and recovery-state checks."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('monitor_fixture',Path(__file__).with_name('verify-monitor-resilience.py'))
fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
ProbeResult=fixture.ProbeResult


class CheckRecoveryChecks(unittest.TestCase):
    setUp=fixture.ResilienceChecks.setUp
    rooms=fixture.ResilienceChecks.rooms
    finish=fixture.ResilienceChecks.finish

    def snapshot_room(self): return self.engine.snapshot()['rooms'][0]

    def test_four_second_timeout_marks_recovery_before_transport_returns(self):
        room=self.rooms(1)[0]
        self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_state'],'checking')
        self.now+=3.99
        self.assertFalse(self.snapshot_room()['monitor_recovering'])
        self.now+=.02
        failed=self.snapshot_room()
        self.assertEqual(failed['check_state'],'recovering')
        self.assertTrue(failed['recovering'])
        self.assertFalse(failed['checking'])
        self.assertTrue(failed['last_check_finished_at'])
        self.assertNotIn(room['id'],self.engine.pending)  # logical slot released
        self.assertIn(room['id'],self.engine.draining_probes)  # physical re-entry fenced
        self.assertEqual(self.engine.probe_timeouts,1)
        self.engine._tick(); self.engine.snapshot()
        self.assertEqual(self.engine.probe_timeouts,1)
        self.assertEqual(len(self.engine.pool.calls),1)

    def test_late_success_cannot_leave_recovery_or_invent_live_event(self):
        self.rooms(1)
        self.engine._tick(); self.now+=4.1
        self.finish(ProbeResult(True))  # no snapshot/tick before callback
        failed=self.snapshot_room()
        self.assertEqual(failed['check_state'],'recovering')
        self.assertEqual(failed['last_confirmed_status'],'unknown')
        self.assertEqual(self.engine.probe_successes,0)
        self.assertEqual(self.engine.probe_timeouts,1)
        self.assertFalse(any(e['kind']=='live_started' for e in self.store.list_events()))
        self.now+=self.store.get_settings()['status_poll_interval_seconds']; self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_state'],'recovering')
        self.finish(ProbeResult(True))
        success=self.snapshot_room()
        self.assertEqual(success['check_state'],'idle')
        self.assertEqual(success['live_status'],'live')
        self.assertFalse(success['monitor_recovering'])

    def test_recovery_survives_rescue_expiration_and_paced_backoff(self):
        room=self.rooms(1)[0]
        self.engine._tick(); self.finish(ProbeResult(True))
        self.now+=8; self.engine._tick(); self.now+=4.1
        self.engine.snapshot()
        self.finish(ProbeResult(False))
        self.now+=11; self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_state'],'recovering')
        self.assertNotIn(room['id'],self.engine.recoveries)
        self.assertTrue(self.engine.states[room['id']]['confirmed_live'])
        self.assertFalse(any(e['kind']=='live_ended' for e in self.store.list_events()))
        self.now=self.engine._status_due_at(room['id'],self.store.get_settings())
        self.engine._tick(); self.finish(ProbeResult(True))
        self.assertEqual(self.snapshot_room()['check_state'],'idle')

    def test_faster_failure_also_stays_recovering_until_success(self):
        self.rooms(1)
        self.engine._tick()
        self.finish(ProbeResult(None,error_code='rate_limited',retry_after=60,diagnostic='http=444'))
        self.assertEqual(self.snapshot_room()['check_state'],'recovering')
        self.assertFalse(self.snapshot_room()['checking'])
        self.now+=20; self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_state'],'recovering')

    def test_disable_clears_recovery_and_late_result_is_ignored(self):
        room=self.rooms(1)[0]
        self.engine._tick(); self.now+=4.1; self.engine.snapshot()
        self.store.update_room(room['id'],{'monitor_enabled':False}); self.engine.room_changed(room['id'])
        self.finish(ProbeResult(True))
        disabled=self.snapshot_room()
        self.assertEqual(disabled['check_state'],'idle')
        self.assertFalse(disabled['monitor_recovering'])
        self.assertEqual(disabled['live_status'],'disabled')
        self.assertEqual(disabled['timeout_retry_yield_count'],0)
        self.assertEqual(disabled['timeout_retry_remaining'],0)
        self.assertEqual(disabled['timeout_retry_step'],0)
        self.store.update_room(room['id'],{'monitor_enabled':True}); self.engine.room_changed(room['id'])
        self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_state'],'checking')
        self.assertEqual(len(self.engine.pool.calls),2)

    def test_undispatched_room_and_waiting_worker_do_not_time_out(self):
        rooms=self.rooms(2)
        self.engine._tick()
        future=self.engine.pool.calls[0][2]
        future.probe_timing={'started_at':None,'started_iso':''}
        self.now+=20
        snapshot=self.engine.snapshot()
        self.assertTrue(all(r['check_state']=='idle' for r in snapshot['rooms']))
        self.assertEqual(self.engine.probe_timeouts,0)
        future.probe_timing.update(started_at=self.now,started_iso='2026-11-22T20:00:00+08:00')
        active=next(r for r in self.engine.snapshot()['rooms'] if r['id']==rooms[0]['id'])
        self.assertEqual(active['check_state'],'checking')
        self.now+=4.1
        active=next(r for r in self.engine.snapshot()['rooms'] if r['id']==rooms[0]['id'])
        self.assertEqual(active['check_state'],'recovering')

    def test_profiles_accept_configured_budget_up_to_sixty_seconds(self):
        self.rooms(1)
        for settings,expected in [({'status_only_mode':True,'status_request_timeout':20},20),
                                  ({'status_only_mode':True,'status_request_timeout':60},60),
                                  ({'status_only_mode':False,'request_timeout':30},30),
                                  ({'status_only_mode':False,'request_timeout':90},60)]:
            self.assertEqual(self.engine._probe_timeout(settings,{}),expected)

    def test_twenty_second_check_survives_four_seconds_and_setting_reduction(self):
        self.rooms(1); self.store.update_settings({'status_request_timeout':20})
        self.engine._tick(); self.now+=5
        self.assertEqual(self.snapshot_room()['check_state'],'checking')
        self.assertEqual(self.snapshot_room()['check_timeout_seconds'],20)
        self.store.update_settings({'status_request_timeout':4}); self.engine.settings_changed()
        self.now+=10
        self.assertEqual(self.snapshot_room()['check_state'],'checking')
        self.finish(ProbeResult(True))
        self.assertEqual(self.snapshot_room()['live_status'],'live')
        self.now+=8; self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_timeout_seconds'],4)

    def test_sixty_second_check_times_out_only_after_its_frozen_budget(self):
        self.rooms(1); self.store.update_settings({'status_request_timeout':60})
        self.engine._tick(); self.now+=59.9
        self.assertEqual(self.snapshot_room()['check_state'],'checking')
        self.assertEqual(self.engine.probe_timeouts,0)
        self.now+=.2
        self.assertEqual(self.snapshot_room()['check_state'],'recovering')
        self.assertIn('60 秒',self.snapshot_room()['last_error'])
        self.finish(ProbeResult(True))
        self.assertEqual(self.engine.probe_successes,0)
        self.assertEqual(self.engine.probe_timeouts,1)

    def test_setting_increase_does_not_extend_existing_four_second_check(self):
        self.rooms(1); self.engine._tick()
        self.store.update_settings({'status_request_timeout':60}); self.engine.settings_changed()
        self.now+=4.1
        self.assertEqual(self.snapshot_room()['check_state'],'recovering')
        self.assertIn('4 秒',self.snapshot_room()['last_error'])

    def test_full_source_uses_request_timeout_and_fast_retry_keeps_own_budget(self):
        room=self.rooms(1)[0]
        self.store.update_settings({'request_timeout':20})
        self.engine.request_sources(room['id']); self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_timeout_seconds'],20)
        self.now+=5
        self.assertEqual(self.snapshot_room()['check_state'],'checking')
        self.finish(ProbeResult(None,error_code='network_error'))
        self.now+=1; self.engine._tick()
        self.assertEqual(self.snapshot_room()['check_timeout_seconds'],2.5)
        self.now+=2.6; self.engine.snapshot()
        self.assertEqual(self.engine.probe_timeouts,1)

    def test_timely_long_response_waiting_for_callback_keeps_frozen_budget(self):
        self.rooms(1); self.store.update_settings({'status_request_timeout':20})
        self.engine._tick()
        future=self.engine.pool.calls[-1][2]
        future.probe_timing={'started_at':self.now,'started_iso':'fixture','response_at':self.now+19}
        self.store.update_settings({'status_request_timeout':4})
        self.now+=25
        self.finish(ProbeResult(True))
        self.assertEqual(self.snapshot_room()['live_status'],'live')
        self.assertEqual(self.engine.probe_timeouts,0)

    def test_timely_response_is_not_timed_out_by_delayed_callback_lock(self):
        self.rooms(1)
        self.engine._tick()
        future=self.engine.pool.calls[0][2]
        future.probe_timing={'started_at':self.now,'started_iso':'fixture','response_at':self.now+3}
        self.now+=5
        self.finish(ProbeResult(True))
        self.assertEqual(self.snapshot_room()['live_status'],'live')
        self.assertEqual(self.snapshot_room()['check_state'],'idle')
        self.assertEqual(self.engine.probe_timeouts,0)

    def test_timely_rescue_response_survives_rescue_window_expiry_while_callback_waits(self):
        room=self.rooms(1)[0]
        self.engine._tick(); self.finish(ProbeResult(True))
        self.now+=8; self.engine._tick()
        self.finish(ProbeResult(None,error_code='network_error'))
        self.now+=1; self.engine._tick()
        future=self.engine.pool.calls[-1][2]
        future.probe_timing={'started_at':self.now,'started_iso':'fixture','response_at':self.now+1}
        self.now+=11
        self.engine._tick()  # Scheduler can also run before the waiting callback.
        self.engine.snapshot()
        self.assertNotIn(future,self.engine.discarded_probes)
        self.assertIn(room['id'],self.engine.recoveries)
        self.finish(ProbeResult(True))
        self.assertEqual(self.snapshot_room()['check_state'],'idle')
        self.assertEqual(self.snapshot_room()['live_status'],'live')
        self.assertNotIn(room['id'],self.engine.recoveries)

    def test_timeout_releases_only_logical_slot_and_runs_healthy_room(self):
        rooms=self.rooms(2,custom=True)
        self.store.update_settings({'status_check_concurrency':1})
        self.engine._tick(); old=self.engine.pool.calls[-1][2]
        self.now+=4.1; self.engine._tick()
        self.assertEqual(len(self.engine.pending),1)
        self.assertIn(rooms[1]['id'],self.engine.pending)
        self.assertIn(rooms[0]['id'],self.engine.draining_probes)
        self.assertEqual(len(self.engine.pool.calls),2)
        self.finish(ProbeResult(True))
        self.now+=100
        self.engine.refresh([rooms[0]['id']]); self.engine._tick()
        self.assertEqual(sum(r['id']==rooms[0]['id'] for r,_,_ in self.engine.pool.calls),1)
        old.set_result(ProbeResult(True))
        self.assertNotIn(rooms[0]['id'],self.engine.draining_probes)
        self.assertIsNone(self.engine.states[rooms[0]['id']]['confirmed_live'])

    def test_timeout_yields_one_two_four_eight_other_dispatches_then_resets(self):
        room=self.rooms(1)[0]
        self.store.update_settings({'monitor_douyin_requests_per_second':20,'monitor_max_requests_per_second':100,'status_check_concurrency':1})
        other=None
        for yields in (1,2,4,8,1,2):
            self.engine._tick(); self.now+=4.1
            self.engine.snapshot(); self.finish(ProbeResult(True))
            state=self.engine.states[room['id']]
            if other is None:
                other=self.store.add_room(dict(platform='wechat',room_input='healthy',status_url='http://127.0.0.1/healthy'))
            self.assertEqual(state['timeout_retry_yield_count'],yields)
            self.assertEqual(state['timeout_retry_remaining'],yields)
            self.assertFalse(self.engine.recoveries)
            for remaining in range(yields-1,-1,-1):
                self.engine.refresh([other['id']]); self.engine._tick()
                self.assertEqual(self.engine.pool.calls[-1][0]['id'],other['id'])
                self.finish(ProbeResult(True))
                self.assertEqual(state['timeout_retry_remaining'],remaining)
            # No clock advance or normal polling wait after N real dispatches.
        self.engine._tick(); self.finish(ProbeResult(True))
        self.assertEqual(self.engine.states[room['id']]['timeout_retry_yield_count'],0)
        self.assertEqual(self.engine.states[room['id']]['timeout_retry_remaining'],0)
        self.assertEqual(self.engine.states[room['id']]['timeout_retry_step'],0)
        self.assertNotIn('_timeout_fallback_at',self.engine.states[room['id']])

    def test_provider_timeout_result_uses_cycles_without_quick_recovery(self):
        room=self.rooms(1)[0]
        self.engine._tick(); self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=deadline'))
        self.assertEqual(self.snapshot_room()['timeout_retry_yield_count'],1)
        self.assertEqual(self.snapshot_room()['timeout_retry_remaining'],1)
        self.assertEqual(self.engine.states[room['id']]['_timeout_fallback_at'],self.now+8)
        self.assertFalse(self.engine.recoveries)
        self.assertEqual(self.engine.probe_timeouts,1)

    def test_timeout_preserves_longer_server_and_platform_cooldown(self):
        room=self.rooms(1)[0]
        self.engine._tick()
        self.engine.due[room['id']]=self.now+60
        self.engine.douyin_blocked_until=self.now+90
        self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=timeout retry_after=70'))
        self.assertEqual(self.snapshot_room()['timeout_retry_yield_count'],1)
        self.assertGreaterEqual(self.engine.due[room['id']],self.now+70)
        self.assertEqual(self.snapshot_room()['next_check_seconds'],90)
        self.engine.refresh(); self.now+=8; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),1)

    def test_legacy_timeout_cycle_uses_configured_round_period(self):
        room=self.rooms(1)[0]
        self.store.update_settings({'status_only_mode':False,'monitor_interval_minutes':.5,'room_check_delay_seconds':0})
        self.engine._tick(); self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
        self.assertEqual(self.engine.states[room['id']]['_timeout_fallback_at'],self.now+30)
        self.now+=29.9; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),1)
        self.now+=.1; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),2)

    def test_inflight_freshness_keeps_frozen_budget_after_settings_reduce(self):
        self.rooms(1); self.engine._tick(); self.finish(ProbeResult(True))
        self.store.update_settings({'status_request_timeout':60})
        self.now+=8; self.engine._tick()
        self.store.update_settings({'status_request_timeout':2})
        self.now+=20
        self.assertEqual(self.snapshot_room()['live_status'],'live')
        self.assertFalse(self.snapshot_room()['status_stale'])
        self.assertEqual(self.snapshot_room()['check_timeout_seconds'],60)

    def test_all_rooms_yielding_get_one_fallback_then_advance_each_other(self):
        rooms=self.rooms(2,custom=True)
        self.engine._tick()
        for _,_,future in self.engine.pool.calls:
            future.set_result(ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
        self.assertTrue(all(self.engine.states[r['id']]['timeout_retry_remaining']==1 for r in rooms))
        self.now+=7.9; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),2)
        self.now+=.1; self.engine._tick()
        self.assertGreater(len(self.engine.pool.calls),2)
        self.assertTrue(any(self.engine.states[r['id']]['timeout_retry_remaining']==0 for r in rooms))

    def test_no_other_room_fallback_does_not_multiply_time_by_yield_count(self):
        room=self.rooms(1)[0]
        for target in (1,2,4,8,1):
            self.engine._tick()
            self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
            self.assertEqual(self.engine.states[room['id']]['timeout_retry_yield_count'],target)
            count=len(self.engine.pool.calls)
            self.now+=7.99; self.engine.refresh(); self.engine._tick()
            self.assertEqual(len(self.engine.pool.calls),count)
            self.now+=.01
        self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),6)

    def test_metadata_lookup_does_not_consume_another_room_opportunity(self):
        room=self.rooms(1)[0]
        self.engine._tick(); self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
        other=self.store.add_room(dict(platform='douyin',room_input='metadata-only',nickname='metadata'))
        state=self.engine._state(other)
        state.update(confirmed_live=True,live_status='live',live_session_id='123456',detection_uncertain=False)
        self.now+=.5
        self.engine._tick_metadata({other['id']:other},self.store.get_settings(),self.now)
        self.assertEqual(len(self.engine.metadata_pool.calls),1)
        self.assertEqual(self.engine.states[room['id']]['timeout_retry_remaining'],1)

    def test_old_disabled_request_frees_logical_slot_without_recovering_disabled_room(self):
        rooms=self.rooms(2,custom=True); self.store.update_settings({'status_check_concurrency':1})
        self.engine._tick(); old=self.engine.pool.calls[-1][2]
        self.store.update_room(rooms[0]['id'],{'monitor_enabled':False}); self.engine.room_changed(rooms[0]['id'])
        self.now+=4.1; self.engine._tick()
        self.assertIn(rooms[1]['id'],self.engine.pending)
        self.assertEqual(self.engine.states[rooms[0]['id']]['live_status'],'disabled')
        self.assertFalse(self.engine.states[rooms[0]['id']]['monitor_recovering'])
        old.set_result(ProbeResult(True))
        self.assertIsNone(self.engine.states[rooms[0]['id']]['confirmed_live'])

    def test_non_timeout_failure_after_yielded_retry_keeps_new_backoff(self):
        room=self.rooms(1)[0]
        self.engine._tick(); self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
        self.now+=8; self.engine._tick()
        self.finish(ProbeResult(None,error_code='anonymous_restricted',retry_after=300,
                                diagnostic='http=200 body=empty stage=room_web_enter'))
        state=self.engine.states[room['id']]
        self.assertEqual(state['timeout_retry_step'],1)
        self.assertFalse(state.get('_timeout_yield_waiting'))
        self.assertGreater(state['_error_recheck_at'],self.now)
        self.assertEqual(self.engine._status_due_at(room['id'],self.store.get_settings()),state['_error_recheck_at'])
        count=len(self.engine.pool.calls)
        self.now+=.5; self.engine._tick()
        self.assertEqual(len(self.engine.pool.calls),count)
        self.now=state['_error_recheck_at']; self.engine._tick()
        self.finish(ProbeResult(None,error_code='network_error',diagnostic='network=timeout'))
        self.assertEqual(state['timeout_retry_yield_count'],2)


if __name__=='__main__': unittest.main(verbosity=2)
