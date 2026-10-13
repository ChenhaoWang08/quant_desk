
"""QuantDesk M2 STEP 2 synchronization integrity tests.

Tests persistent run/range states, snapshot reconciliation,
audit history, rollback, interruption recovery, and cache hits.

All tests use temporary SQLite databases.
The real data/macro.db is never accessed.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from backend.models import DateRange, Observation
from backend.sync_storage import SyncStorage, SyncIntegrityError


class SyncIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test_macro.db"
        self.store = SyncStorage(self.path)
        self.store.initialize()
        self.interval = DateRange(
            date(2024, 1, 1), date(2024, 12, 31)
        )

    def record(self, month, value):
        return Observation("GDP", date(2024, month, 1), value)

    def begin_range(self, request_id):
        self.store.begin_run(request_id)
        return self.store.start_range(
            request_id, 0, self.interval
        )

    def test_schema_and_idempotent_initialization(self):
        self.store.initialize()
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table'"
                )
            }
        self.assertTrue({
            "observations", "coverage", "sync_runs",
            "sync_ranges", "observation_audit"
        } <= tables)

    def test_first_commit(self):
        rid = self.begin_range("run_first")
        result = self.store.commit_range(
            rid, (self.record(1, 28793.191),)
        )
        self.assertEqual(result.inserted, 1)
        self.assertEqual(
            self.store.finish_run("run_first"), "COMPLETED"
        )
        self.assertEqual(
            self.store.base.count_observations("GDP"), 1
        )
        self.assertEqual(
            self.store.base.read_coverage("GDP"),
            (self.interval,)
        )

    def test_duplicate_commit_is_rejected(self):
        rid = self.begin_range("run_duplicate")
        self.store.commit_range(rid, (self.record(1, 100),))
        with self.assertRaises(SyncIntegrityError):
            self.store.commit_range(
                rid, (self.record(1, 100),)
            )
        self.assertEqual(len(self.store.read_audit()), 1)

    def test_revision_and_unchanged(self):
        first = self.begin_range("run_initial")
        self.store.commit_range(
            first, (self.record(1, 100),)
        )
        self.store.finish_run("run_initial")

        second = self.begin_range("run_revision")
        result = self.store.commit_range(
            second, (self.record(1, 110),)
        )
        self.assertEqual(result.updated, 1)
        self.store.finish_run("run_revision")

        third = self.begin_range("run_unchanged")
        result = self.store.commit_range(
            third, (self.record(1, 110),)
        )
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(len(self.store.read_audit()), 2)

    def test_explicit_missing_and_reinstate(self):
        first = self.begin_range("run_value")
        self.store.commit_range(
            first, (self.record(1, 100),)
        )
        self.store.finish_run("run_value")

        second = self.begin_range("run_missing")
        result = self.store.commit_range(
            second,
            (),
            missing_dates=(date(2024, 1, 1),),
        )
        self.assertEqual(result.invalidated, 1)
        self.assertEqual(
            self.store.base.count_observations("GDP"), 0
        )
        self.store.finish_run("run_missing")

        third = self.begin_range("run_reinstate")
        result = self.store.commit_range(
            third, (self.record(1, 120),)
        )
        self.assertEqual(result.reinstated, 1)

        actions = [
            row["action"] for row in self.store.read_audit()
        ]
        self.assertEqual(
            actions, ["INSERT", "INVALIDATE", "REINSTATE"]
        )

    def test_unconfirmed_absence_keeps_old_value(self):
        first = self.begin_range("run_two_values")
        self.store.commit_range(
            first,
            (self.record(1, 100), self.record(4, 200)),
        )
        self.store.finish_run("run_two_values")

        second = self.begin_range("run_absence")
        result = self.store.commit_range(
            second, (self.record(1, 100),)
        )

        self.assertEqual(result.unconfirmed, 1)
        self.assertEqual(
            self.store.base.count_observations("GDP"), 2
        )

        self.assertEqual(
            self.store.base.read_coverage("GDP"),
            (
                DateRange(
                    date(2024, 1, 1),
                    date(2024, 3, 31),
                ),
                DateRange(
                    date(2024, 4, 2),
                    date(2024, 12, 31),
                ),
            ),
        )

        self.assertEqual(
            self.store.finish_run("run_absence"),
            "PARTIAL",
        )

    def test_sql_failure_rolls_back_everything(self):
        rid = self.begin_range("run_rollback")

        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("""
                    CREATE TRIGGER fail_audit
                    BEFORE INSERT ON observation_audit
                    BEGIN
                        SELECT RAISE(ABORT, 'forced failure');
                    END
                """)

        with self.assertRaises(sqlite3.DatabaseError):
            self.store.commit_range(
                rid, (self.record(1, 100),)
            )

        self.assertEqual(
            self.store.base.count_observations("GDP"), 0
        )
        self.assertEqual(
            self.store.base.read_coverage("GDP"), ()
        )
        self.assertEqual(
            self.store.read_ranges("run_rollback")[0]["status"],
            "RUNNING",
        )

    def test_failure_and_partial_run(self):
        self.store.begin_run("run_partial")

        first = self.store.start_range(
            "run_partial", 0,
            DateRange(
                date(2024, 1, 1),
                date(2024, 3, 31),
            ),
        )
        self.store.commit_range(
            first, (self.record(1, 100),)
        )

        second = self.store.start_range(
            "run_partial", 1,
            DateRange(
                date(2024, 4, 1),
                date(2024, 12, 31),
            ),
        )
        self.store.fail_range(second, "HTTP_503")

        self.assertEqual(
            self.store.finish_run("run_partial"),
            "PARTIAL",
        )
        self.assertEqual(
            len(self.store.read_ranges("run_partial")), 2
        )

    def test_recovery_requires_exclusive_confirmation(self):
        self.begin_range("run_crashed")

        with self.assertRaises(SyncIntegrityError):
            self.store.recover_interrupted()

        count = self.store.recover_interrupted(
            confirmed_exclusive_access=True
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            self.store.read_run("run_crashed")["status"],
            "INTERRUPTED",
        )
        self.assertEqual(
            self.store.read_ranges("run_crashed")[0]["status"],
            "INTERRUPTED",
        )

    def test_cache_hit(self):
        self.store.begin_run("run_cached")
        self.assertEqual(
            self.store.finish_run("run_cached"),
            "CACHE_HIT",
        )

    def test_concurrent_active_runs_rejected(self):
        self.store.begin_run("run_one")
        with self.assertRaises(SyncIntegrityError):
            self.store.begin_run("run_two")


if __name__ == "__main__":
    unittest.main()
