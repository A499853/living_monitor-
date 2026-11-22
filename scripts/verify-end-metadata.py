"""Offline scheduler checks for delayed Douyin broadcast finish metadata."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('start_metadata_checks', HERE / 'verify-start-metadata.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
Engine, ProbeResult = support.Engine, support.ProbeResult
END = '2026-11-20T11:00:00+08:00'


class EndMetadataChecks(unittest.TestCase):
    setUp = support.MetadataChecks.setUp
    configure = support.MetadataChecks.configure
    live = support.MetadataChecks.live
    first_wave = support.MetadataChecks.first_wave
    state = support.MetadataChecks.state

    def begin(self):
        self.configure()
        self.first_wave([self.live(actual=support.START, source=support.SOURCE)])
        self.local_session = self.store.get_open_session(self.rooms[0]['id'])['id']

    def confirm_end(self):
        rid = self.rooms[0]['id']
        self.engine.refresh([rid])
        self.engine._tick()
        self.status.calls[-1].future.set_result(ProbeResult(False))
        self.assertIs(self.state()['confirmed_live'], True)
        self.assertFalse(self.engine.end_metadata_jobs)
        self.clock.now += 1
        self.engine._tick()
        self.status.calls[-1].future.set_result(ProbeResult(False))
        self.assertIs(self.state()['confirmed_live'], False)
        self.assertIn(self.local_session, self.engine.end_metadata_jobs)

    def collect_end(self):
        self.engine._tick()
        call = self.metadata.calls[-1]
        self.assertTrue(call.settings['_metadata_only'])
        self.assertTrue(call.settings['_metadata_ended'])
        self.assertEqual(call.settings['_metadata_session_id'], '700000')
        return call

    def ended(self, call):
        return ProbeResult(False, live_session_id=call.settings['_metadata_session_id'], live_ended_at=END)

    def session(self):
        return dict(self.store.db.execute('SELECT * FROM sessions WHERE id=?', (self.local_session,)).fetchone())

    def test_confirmed_end_is_queued_and_success_persists_without_changing_observations(self):
        self.begin()
        self.confirm_end()
        call = self.collect_end()
        preserved = {key: self.state()[key] for key in ('confirmed_live', 'live_status', 'last_checked',
                     'last_success_at', 'live_ended_at', 'observed_started_at', 'record_status')}
        observed_end = self.session()['ended_at']
        call.future.set_result(self.ended(call))
        actual = self.engine._actual_time(END)
        self.assertEqual(self.state()['platform_ended_at'], actual)
        self.assertEqual(self.session()['actual_ended_at'], actual)
        self.assertEqual(self.session()['ended_at'], observed_end)
        self.assertEqual({key: self.state()[key] for key in preserved}, preserved)
        self.assertNotIn(self.local_session, self.engine.end_metadata_jobs)
        csv_rows = []
        for path in Path(self.directory.name).rglob('events*.csv'):
            with path.open(encoding='utf-8-sig', newline='') as handle:
                csv_rows.extend(csv.DictReader(handle))
        self.assertTrue(any(row['actual_ended_at'] == actual and row['observed_ended_at'] == observed_end for row in csv_rows))
        self.assertEqual(self.store.get_runtime(self.rooms[0]['id'])['platform_ended_at'], actual)

    def test_failed_end_lookup_waits_one_minute_and_status_checks_continue(self):
        self.begin()
        self.confirm_end()
        call = self.collect_end()
        call.future.set_result(ProbeResult(None, error_code='metadata_pending'))
        self.clock.now += 59
        self.engine._tick()
        self.assertEqual(len(self.metadata.calls), 1)
        self.status.calls[-1].future.set_result(ProbeResult(False))
        self.assertEqual(self.state()['last_error'], '')
        self.clock.now += 1
        self.engine._tick()
        self.assertEqual(len(self.metadata.calls), 2)
        self.assertIs(self.state()['confirmed_live'], False)

    def test_latest_end_remains_visible_after_new_broadcast(self):
        self.begin()
        self.confirm_end()
        call = self.collect_end()
        self.engine.refresh([self.rooms[0]['id']])
        self.engine._tick()
        self.status.calls[-1].future.set_result(self.live(sid='800000', actual=support.START, source=support.SOURCE))
        new_session = self.store.get_open_session(self.rooms[0]['id'])
        call.future.set_result(self.ended(call))
        self.assertTrue(self.session()['actual_ended_at'])
        self.assertEqual(self.state()['platform_ended_at'], self.engine._actual_time(END))
        self.assertEqual(self.state()['live_session_id'], '800000')
        self.assertIs(self.state()['confirmed_live'], True)
        self.assertEqual(self.store.get_open_session(self.rooms[0]['id'])['id'], new_session['id'])

    def test_initial_offline_fetches_previous_broadcast_without_observed_start(self):
        self.configure()
        self.first_wave([ProbeResult(False,live_session_id='700000')])
        state=self.state()
        self.assertEqual(state['observed_started_at'],'')
        self.assertEqual(state['live_ended_at'],'')
        self.local_session=state['last_ended_session_id']
        self.assertTrue(self.local_session)
        call=self.collect_end()
        call.future.set_result(ProbeResult(False,live_session_id='700000',
            live_started_at=support.START,started_at_source=support.SOURCE,live_ended_at=END))
        self.assertEqual(state['live_started_at'],self.engine._actual_time(support.START))
        self.assertEqual(state['platform_ended_at'],self.engine._actual_time(END))
        self.assertEqual(self.session()['started_at'],'')
        self.assertFalse(self.engine.end_metadata_jobs)
        self.clock.now+=61
        self.engine._tick()
        self.assertEqual(len(self.metadata.calls),1)

    def test_new_start_preserves_last_end_until_next_confirmed_end(self):
        self.begin(); self.confirm_end()
        call=self.collect_end(); call.future.set_result(self.ended(call))
        previous_end=self.state()['platform_ended_at']
        previous_observed=self.state()['live_ended_at']
        self.engine.refresh([self.rooms[0]['id']]); self.engine._tick()
        self.status.calls[-1].future.set_result(self.live(sid='800000'))
        self.assertEqual(self.state()['platform_ended_at'],previous_end)
        self.assertEqual(self.state()['live_ended_at'],previous_observed)

    def test_end_only_initial_result_keeps_missing_start_queued(self):
        self.configure()
        self.first_wave([ProbeResult(False,live_session_id='700000')])
        self.local_session=self.state()['last_ended_session_id']
        call=self.collect_end(); call.future.set_result(self.ended(call))
        self.assertIn(self.local_session,self.engine.end_metadata_jobs)
        self.clock.now+=60; self.engine._tick()
        self.status.calls[-1].future.set_result(ProbeResult(False,live_session_id='700000'))
        self.engine._tick(); call=self.metadata.calls[-1]
        call.future.set_result(ProbeResult(False,live_session_id='700000',
            live_started_at=support.START,started_at_source=support.SOURCE,live_ended_at=END))
        self.assertEqual(self.state()['live_started_at'],self.engine._actual_time(support.START))
        self.assertNotIn(self.local_session,self.engine.end_metadata_jobs)

    def test_legacy_monitor_mode_also_dispatches_pending_time(self):
        self.configure()
        self.store.update_settings({'status_only_mode':False,'room_check_delay_seconds':0})
        self.engine.settings_changed()
        self.first_wave([ProbeResult(False,live_session_id='700000')])
        self.local_session=self.state()['last_ended_session_id']
        self.collect_end()

    def test_restart_restores_pending_closed_sessions(self):
        self.begin()
        self.confirm_end()
        self.engine.close()
        self.engine = Engine(self.store, providers=support.Provider())
        self.engine.pool.shutdown()
        self.engine.metadata_pool.shutdown()
        self.status, self.metadata = support.HeldPool(), support.HeldPool()
        self.engine.pool, self.engine.metadata_pool = self.status, self.metadata
        self.assertIn(self.local_session, self.engine.end_metadata_jobs)
        call = self.collect_end()
        call.future.set_result(self.ended(call))
        self.assertTrue(self.session()['actual_ended_at'])

    def test_deleted_disabled_changed_source_and_auth_results_are_discarded(self):
        for change in ('deleted', 'disabled', 'changed_source', 'auth_changed'):
            with self.subTest(change=change):
                if self.engine:
                    self.engine.close()
                self.store.db.execute('DELETE FROM rooms')
                self.store.db.commit()
                self.begin()
                self.confirm_end()
                call = self.collect_end()
                rid = self.rooms[0]['id']
                if change == 'deleted':
                    self.store.delete_room(rid)
                    self.engine.room_changed(rid)
                elif change == 'auth_changed':
                    self.engine.auth_changed()
                else:
                    self.store.update_room(rid, {'monitor_enabled': False} if change == 'disabled' else {'room_input': '999999'})
                    self.engine.room_changed(rid)
                call.future.set_result(self.ended(call))
                row = self.store.db.execute('SELECT actual_ended_at FROM sessions WHERE id=?', (self.local_session,)).fetchone()
                self.assertTrue(row is None or row[0] == '')
                self.assertFalse(any(event['kind'] == 'live_end_metadata' and event['room_id'] == rid
                                     for event in self.store.list_events()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
