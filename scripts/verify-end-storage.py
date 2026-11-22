"""Offline persistence checks for delayed platform broadcast finish times."""
from __future__ import annotations

import csv
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "app"))

from livewatch.storage import Store, room_directory

START = "2023-11-14T22:13:20+00:00"
OBSERVED_START = "2023-11-14T22:15:00+00:00"
END = "2023-11-14T23:00:00+00:00"
OBSERVED_END = "2023-11-14T23:01:00+00:00"


class EndStorageChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="livewatch-end-storage-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.room = self.store.add_room({"platform": "douyin", "room_input": "123456", "nickname": "test"})

    def closed_session(self, sid="700001"):
        local_id = self.store.open_session(self.room, OBSERVED_START, START, sid, "douyin.room.start_time")
        self.store.close_session(self.room, OBSERVED_END)
        return local_id

    def row(self, local_id):
        return dict(self.store.db.execute("SELECT * FROM sessions WHERE id=?", (local_id,)).fetchone())

    def test_platform_end_preserves_observation_and_writes_csv(self):
        local_id = self.closed_session()
        self.assertEqual(self.store.get_pending_end_sessions(self.room["id"])[0]["id"], local_id)
        self.assertTrue(self.store.update_session_end(self.room, local_id, "700001", END))
        row = self.row(local_id)
        self.assertEqual(row["actual_ended_at"], END)
        self.assertEqual(row["ended_at_source"], "douyin.room.end_time")
        self.assertEqual(row["started_at"], OBSERVED_START)
        self.assertEqual(row["ended_at"], OBSERVED_END)
        self.assertEqual(row["actual_started_at"], START)
        self.assertEqual(self.store.get_pending_end_sessions(self.room["id"]), [])
        self.assertFalse(self.store.update_session_end(self.room, local_id, "700001", END))
        path = Path(self.directory.name) / "time_begin_over" / room_directory(self.room) / "events.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            events = list(csv.DictReader(handle))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]["event"], "metadata_updated")
        self.assertEqual(events[-1]["actual_ended_at"], END)
        self.assertEqual(events[-1]["observed_ended_at"], OBSERVED_END)

    def test_initial_offline_history_does_not_invent_observed_start(self):
        local_id=self.store.discover_ended_session(self.room,'700010',START,END,OBSERVED_END)
        self.assertTrue(local_id)
        row=self.row(local_id)
        self.assertEqual(row['started_at'],'')
        self.assertEqual(row['ended_at'],OBSERVED_END)
        self.assertEqual(row['actual_started_at'],START)
        self.assertEqual(row['actual_ended_at'],END)
        self.assertIsNone(self.store.get_open_session(self.room['id']))
        path=Path(self.directory.name)/'time_begin_over'/room_directory(self.room)/'events.csv'
        with path.open(encoding='utf-8-sig',newline='') as handle: events=list(csv.DictReader(handle))
        self.assertEqual(events[0]['event'],'discovered_offline')
        self.assertEqual(events[0]['observed_started_at'],'')
        self.assertFalse(any(event['event']=='start' for event in events))
        self.assertEqual(self.store.list_events(),[])

    def test_initial_history_reuses_closed_session_and_later_fills_times(self):
        local_id=self.store.discover_ended_session(self.room,'700010',observed_at=OBSERVED_END)
        self.assertEqual(self.store.get_pending_end_sessions(self.room['id'])[0]['id'],local_id)
        self.assertEqual(self.store.discover_ended_session(self.room,'700010',START,END,OBSERVED_END),local_id)
        self.assertEqual(self.store.discover_ended_session(self.room,'700010',START,END,OBSERVED_END),local_id)
        row=self.row(local_id)
        self.assertEqual(row['started_at'],'')
        self.assertEqual(row['actual_started_at'],START)
        self.assertEqual(row['actual_ended_at'],END)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sessions').fetchone()[0],1)

    def test_initial_history_never_closes_matching_open_session(self):
        local_id=self.store.open_session(self.room,OBSERVED_START,START,'700010','douyin.room.start_time')
        before=self.row(local_id)
        self.assertEqual(self.store.discover_ended_session(self.room,'700010',START,END,OBSERVED_END),local_id)
        self.assertEqual(self.row(local_id),before)
        self.assertEqual(self.store.get_open_session(self.room['id'])['id'],local_id)

    def test_initial_history_rejects_invalid_target_or_times(self):
        for sid,start,end,observed in (
                ('',START,END,OBSERVED_END),('bad',START,END,OBSERVED_END),
                ('700010','invalid',END,OBSERVED_END),('700010',START,'invalid',OBSERVED_END),
                ('700010',START,START,OBSERVED_END),('700010',START,END,'invalid'),
                ('700010',START,'2999-01-01T00:00:00+00:00',OBSERVED_END),
                ('700010','2023-11-14T22:13:20',END,OBSERVED_END)):
            with self.subTest(sid=sid,start=start,end=end,observed=observed):
                self.assertEqual(self.store.discover_ended_session(self.room,sid,start,end,observed),'')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)

    def test_old_closed_session_can_complete_while_new_session_stays_open(self):
        old_id = self.closed_session()
        new_id = self.store.open_session(self.room, "2023-11-15T22:15:00+00:00", "", "700002")
        before = self.store.get_open_session(self.room["id"])
        self.assertTrue(self.store.update_session_end(self.room, old_id, "700001", END))
        self.assertEqual(self.store.get_open_session(self.room["id"]), before)
        self.assertEqual(self.store.get_open_session(self.room["id"])["id"], new_id)
        self.assertFalse(self.store.update_session_end(self.room, new_id, "700002", END))

    def test_wrong_identity_and_invalid_platform_times_are_rejected(self):
        local_id = self.closed_session()
        other_room = self.store.add_room({"platform": "douyin", "room_input": "789"})
        self.assertFalse(self.store.update_session_end(other_room, local_id, "700001", END))
        self.assertFalse(self.store.update_session_end(self.room, local_id, "700002", END))
        self.assertFalse(self.store.update_session_end(self.room, "missing", "700001", END))
        for invalid in ("", "invalid", "2023-11-14T23:00:00", START, "2023-11-14T21:00:00+00:00", "2999-01-01T00:00:00+00:00"):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.store.update_session_end(self.room, local_id, "700001", invalid))
        self.assertEqual(self.row(local_id)["actual_ended_at"], "")

    def test_pending_queue_only_returns_closed_numeric_missing_ends_with_bound(self):
        for index in range(103):
            self.store.db.execute("INSERT INTO sessions(id,room_id,started_at,ended_at,platform_session_id) VALUES(?,?,?,?,?)",
                                  (str(index), self.room["id"], START, f"2023-11-14T23:01:{index:03d}+00:00", str(800000+index)))
        for local_id, observed_end, platform_id, actual_end in (
                ("open", "", "700001", ""), ("missing", OBSERVED_END, "", ""),
                ("nonnumeric", OBSERVED_END, "other", ""), ("done", OBSERVED_END, "700001", END)):
            self.store.db.execute("INSERT INTO sessions(id,room_id,started_at,ended_at,platform_session_id,actual_ended_at) VALUES(?,?,?,?,?,?)",
                                  (local_id, self.room["id"], START, observed_end, platform_id, actual_end))
        self.store.db.commit()
        rows = self.store.get_pending_end_sessions(self.room["id"])
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["id"], "102")
        self.assertEqual(rows[-1]["id"], "3")

    def test_old_csv_generations_are_preserved_when_new_end_column_is_added(self):
        directory = Path(self.directory.name) / "time_begin_over" / room_directory(self.room)
        directory.mkdir(parents=True)
        old = b"legacy_header\r\nuntouched\r\n"
        previous = b"session_id,ended_at_source\r\nprevious,observed_offline\r\n"
        (directory / "events.csv").write_bytes(old)
        (directory / "events_v2.csv").write_bytes(previous)
        local_id = self.closed_session()
        self.assertTrue(self.store.update_session_end(self.room, local_id, "700001", END))
        self.assertEqual((directory / "events.csv").read_bytes(), old)
        self.assertEqual((directory / "events_v2.csv").read_bytes(), previous)
        with (directory / "events_v3.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[-1]["actual_ended_at"], END)


class EndMigrationChecks(unittest.TestCase):
    def test_existing_session_migration_preserves_local_clocks(self):
        with tempfile.TemporaryDirectory(prefix="livewatch-end-migration-") as directory:
            data = Path(directory) / "data"
            data.mkdir()
            with closing(sqlite3.connect(data / "livewatch.db")) as db:
                db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,room_id TEXT,started_at TEXT,ended_at TEXT DEFAULT '')")
                db.execute("INSERT INTO sessions VALUES(?,?,?,?)", ("old", "room", OBSERVED_START, OBSERVED_END))
                db.commit()
            store = Store(directory)
            try:
                row = dict(store.db.execute("SELECT * FROM sessions WHERE id='old'").fetchone())
                self.assertEqual(row["started_at"], OBSERVED_START)
                self.assertEqual(row["ended_at"], OBSERVED_END)
                self.assertEqual(row["actual_ended_at"], "")
                self.assertEqual(row["ended_at_source"], "")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
