"""Offline four-second watchdog, late-result and recovery-state checks."""
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
        self.assertIn(room['id'],self.engine.pending)  # no overlapping retries
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
        self.now+=1; self.engine._tick()
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

    def test_all_room_profiles_are_capped_at_four_seconds(self):
        self.rooms(1)
        for settings in ({'status_only_mode':True,'status_request_timeout':15},
                         {'status_only_mode':False,'request_timeout':30}):
            self.assertEqual(self.engine._probe_timeout(settings,{}),4)

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
        self.now+=8; self.engine._tick(); self.now+=4.1
        self.engine.snapshot(); self.finish(ProbeResult(False))
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


if __name__=='__main__': unittest.main(verbosity=2)
