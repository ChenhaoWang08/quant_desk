"""QuantDesk M2 STEP 5: isolated synchronization failure injection.

Flow: fake provider -> production GDPSyncCoordinator -> SyncStorage,
with disposable SQLite. Verify failed/partial statuses, rollback, and
explicit manual recovery. Never contact FRED or modify production files.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from backend.models import DateRange, FetchMode, FetchRequest, Observation
from backend.providers.base import DataProvider, ProviderError, ProviderResponse, RawObservation
from backend.storage import SQLiteStore
from backend.sync_storage import SyncStorage, SyncIntegrityError
from backend.sync_coordinator import GDPSyncCoordinator


class FakeProvider(DataProvider):
    def __init__(self, fail_on=None):
        self.calls = 0
        self.fail_on = fail_on

    @property
    def name(self):
        return "FRED"

    def supports(self, series_id):
        return series_id == "GDP"

    def get_earliest_available_date(self, series_id):
        return date(1947, 1, 1)

    def fetch_observations(self, *, series_id, date_range, realtime_date):
        self.calls += 1
        if self.calls == self.fail_on:
            raise ProviderError("synthetic network failure", provider="FRED",
                                code="HTTP_503", retryable=True, http_status=503)
        return ProviderResponse(
            provider_name="FRED", series_id=series_id,
            requested_range=date_range,
            observations=(RawObservation("2024-01-01", "100.000"),)
            if date_range.contains(date(2024, 1, 1)) else (),
            total_count=1 if date_range.contains(date(2024, 1, 1)) else 0,
            realtime_date=realtime_date,
        )


class E2EFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.db"
        self.storage = SyncStorage(self.db)
        self.storage.initialize()
        self.coordinator = GDPSyncCoordinator(self.storage)
        self.as_of = date(2026, 10, 13)
        self.period = DateRange(date(2024, 1, 1), date(2025, 12, 31))

    def request(self):
        return FetchRequest(series_id="GDP", date_range=self.period,
                            mode=FetchMode.RANGE)

    def test_network_failure_does_not_commit_coverage(self):
        result = self.coordinator.sync(self.request(), as_of_date=self.as_of,
                                       provider=FakeProvider(fail_on=1))
        self.assertEqual((result.status, result.error_code), ("FAILED", "HTTP_503"))
        self.assertEqual(self.storage.base.read_coverage("GDP"), ())
        self.assertEqual(self.storage.base.count_observations("GDP"), 0)

    def test_successful_first_range_survives_second_range_failure(self):
        self.storage.base.commit_range("GDP", DateRange(
            date(2024, 7, 1), date(2024, 12, 31)), ())
        result = self.coordinator.sync(self.request(), as_of_date=self.as_of,
                                       provider=FakeProvider(fail_on=2))
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.committed_ranges, 1)
        self.assertEqual(self.storage.base.count_observations("GDP"), 1)
        self.assertEqual([r["status"] for r in self.storage.read_ranges(result.request_id)],
                         ["COMMITTED", "FAILED"])

    def test_sql_trigger_rolls_back_range_and_marks_failed(self):
        with closing(sqlite3.connect(self.db)) as db:
            with db:
                db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON observation_audit
                              BEGIN SELECT RAISE(ABORT, 'injected SQL failure'); END""")
        result = self.coordinator.sync(self.request(), as_of_date=self.as_of,
                                       provider=FakeProvider())
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "STORAGE_ERROR")
        self.assertEqual(self.storage.base.count_observations("GDP"), 0)
        self.assertEqual(self.storage.base.read_coverage("GDP"), ())

    def test_recovery_requires_explicit_exclusivity(self):
        self.storage.begin_run("interrupted")
        self.storage.start_range("interrupted", 0, self.period)
        with self.assertRaises(SyncIntegrityError):
            self.storage.recover_interrupted()
        self.assertEqual(self.storage.recover_interrupted(
            confirmed_exclusive_access=True), 1)
        self.assertEqual(self.storage.read_run("interrupted")["status"], "INTERRUPTED")

    def test_same_range_second_time_is_cache_hit(self):
        self.coordinator.sync(self.request(), as_of_date=self.as_of,
                              provider=FakeProvider())
        fresh = FakeProvider(fail_on=1)
        result = self.coordinator.sync(self.request(), as_of_date=self.as_of,
                                       provider=fresh)
        self.assertEqual(result.status, "CACHE_HIT")
        self.assertEqual(fresh.calls, 0)

    def test_non_provider_exception_is_not_silently_reported_completed(self):
        class ExplodingProvider(FakeProvider):
            def fetch_observations(self, **kwargs):
                raise RuntimeError("synthetic unexpected failure")
        request = self.request()
        with self.assertRaises(RuntimeError):
            self.coordinator.sync(request, as_of_date=self.as_of,
                                  provider=ExplodingProvider())
        self.assertEqual(self.storage.read_run(request.request_id)["status"], "RUNNING")
        self.assertEqual(self.storage.base.read_coverage("GDP"), ())
        # Known limitation: recovery requires exclusive access confirmation.


if __name__ == "__main__":
    unittest.main()
