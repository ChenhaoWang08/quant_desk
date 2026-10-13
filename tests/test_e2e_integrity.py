"""QuantDesk M2 STEP 5: persistence and independent read-only verification.

Flow: disposable SQLite -> SyncStorage commits -> restart -> compare rows,
coverage and audit -> read-only verifier -> independently generated backup.
No FRED requests, keys or production database access.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from backend.models import DateRange, Observation
from backend.sync_storage import SyncStorage
from tools.verify_e2e import verify, main


class E2EIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.db"
        self.store = SyncStorage(self.db)
        self.store.initialize()
        self.window = DateRange(date(2024, 1, 1), date(2024, 12, 31))

    def commit(self, rid, entries, missing=()):
        self.store.begin_run(rid)
        range_id = self.store.start_range(rid, 0, self.window)
        summary = self.store.commit_range(
            range_id, tuple(Observation("GDP", date(2024, m, 1), value)
                            for m, value in entries),
            missing_dates=tuple(date(2024, m, 1) for m in missing),
        )
        return summary, self.store.finish_run(rid)

    def test_restart_persists_observations_and_coverage(self):
        self.commit("one", ((1, 100.125), (4, 200.250)))
        reopened = SyncStorage(self.db)
        reopened.initialize()
        rows = reopened.base.read_observations("GDP")
        self.assertEqual([x.value for x in rows], [100.125, 200.25])
        self.assertEqual(reopened.base.read_coverage("GDP"), (self.window,))
        self.assertEqual(reopened.read_run("one")["status"], "COMPLETED")

    def test_revision_unchanged_and_audit(self):
        self.commit("one", ((1, 100.0),))
        result, status = self.commit("two", ((1, 110.0),))
        self.assertEqual((result.updated, status), (1, "COMPLETED"))
        result, _ = self.commit("three", ((1, 110.0),))
        self.assertEqual(result.unchanged, 1)
        self.assertEqual([r["action"] for r in self.store.read_audit()],
                         ["INSERT", "UPDATE"])

    def test_missing_and_reinstate_are_audited(self):
        self.commit("one", ((1, 100.0),))
        invalidation, _ = self.commit("two", (), missing=(1,))
        self.assertEqual(invalidation.invalidated, 1)
        self.assertEqual(self.store.base.count_observations("GDP"), 0)
        reinstatement, _ = self.commit("three", ((1, 101.0),))
        self.assertEqual(reinstatement.reinstated, 1)
        self.assertEqual([r["action"] for r in self.store.read_audit()],
                         ["INSERT", "INVALIDATE", "REINSTATE"])

    def test_absent_existing_date_remains_but_unconfirmed(self):
        self.commit("one", ((1, 100.0), (4, 200.0)))
        result, status = self.commit("two", ((1, 100.0),))
        self.assertEqual((result.unconfirmed, status), (1, "PARTIAL"))
        self.assertEqual(self.store.base.count_observations("GDP"), 2)
        self.assertFalse(any(x.contains(date(2024, 4, 1))
                             for x in self.store.base.read_coverage("GDP")))

    def test_reference_exact_match_and_mismatch(self):
        self.commit("one", ((1, 100.125), (4, 200.250)))
        reference = self.root / "reference.json"
        reference.write_text(json.dumps([
            {"date": "2024-01-01", "value": "100.125"},
            {"date": "2024-04-01", "value": "200.250"},
        ]), encoding="utf-8")
        self.assertEqual(verify(self.db, reference=reference)["reference"]["status"], "PASS")
        reference.write_text(json.dumps([
            {"date": "2024-01-01", "value": "999.000"},
        ]), encoding="utf-8")
        result = verify(self.db, reference=reference)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reference"]["different_value_dates"], ["2024-01-01"])
        self.assertEqual(result["reference"]["extra_dates"], ["2024-04-01"])

    def test_verified_backup_is_restorable(self):
        self.commit("one", ((1, 100.0),))
        backup = self.root / "backup.db"
        restored = self.root / "restored.db"
        with closing(sqlite3.connect(self.db)) as source:
            with closing(sqlite3.connect(backup)) as dest:
                source.backup(dest)
        with closing(sqlite3.connect(backup)) as source:
            with closing(sqlite3.connect(restored)) as dest:
                source.backup(dest)
        self.assertEqual(verify(restored)["status"], "PASS")
        self.assertEqual(verify(self.db, backup=backup)["backup"]["status"], "PASS")
        self.assertEqual(verify(self.db)["observation_sha256"],
                         verify(restored)["observation_sha256"])

    def test_verifier_does_not_mutate_database(self):
        self.commit("one", ((1, 100.0),))
        before = self.db.read_bytes()
        result = verify(self.db)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(before, self.db.read_bytes())

    def test_missing_database_does_not_get_created(self):
        absent = self.root / "absent.db"
        self.assertEqual(main(["--db", str(absent), "--json"]), 4)
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
