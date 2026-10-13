
"""Tests for QuantDesk M2 STEP 3 service integration.

Flow:
    MacroService -> GDPSyncCoordinator -> FakeProvider
    -> GDPValidator -> GDPNormalizer -> SyncStorage.

No real network or FRED API key is needed.
All tests use disposable SQLite databases.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.models import (
    DateRange,
    FetchMode,
    FetchRequest,
)
from backend.providers.base import (
    DataProvider,
    ProviderError,
    ProviderResponse,
    RawObservation,
)
from backend.service import MacroService


TODAY = date(2026, 10, 13)
PERIOD = DateRange(
    date(2024, 1, 1),
    date(2025, 12, 31),
)


class FakeGDPProvider(DataProvider):
    """Deterministic FRED substitute for integration tests."""

    def __init__(self, records=(), fail_on=None):
        self.records = tuple(records)
        self.fail_on = fail_on
        self.calls = []
        self.discovery_calls = 0

    @property
    def name(self) -> str:
        return "FRED"

    def supports(self, series_id: str) -> bool:
        return series_id == "GDP"

    def get_earliest_available_date(
        self,
        series_id: str,
    ) -> date:
        self.discovery_calls += 1
        return date(1947, 1, 1)

    def fetch_observations(
        self,
        *,
        series_id: str,
        date_range: DateRange,
        realtime_date: date,
    ) -> ProviderResponse:
        self.calls.append(date_range)

        if len(self.calls) == self.fail_on:
            raise ProviderError(
                "Simulated FRED failure",
                provider="FRED",
                code="HTTP_503",
                retryable=True,
                http_status=503,
            )

        selected = tuple(
            RawObservation(day, value)
            for day, value in self.records
            if date_range.contains(date.fromisoformat(day))
        )

        return ProviderResponse(
            provider_name="FRED",
            series_id=series_id,
            requested_range=date_range,
            observations=selected,
            total_count=len(selected),
            realtime_date=realtime_date,
        )


class ServiceSyncTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db_path = (
            Path(self.temp.name) / "data" / "macro.db"
        )

        self.service = MacroService(self.db_path)
        self.service.initialize()
        self.service.initialize_sync_storage()

    def request(
        self,
        mode=FetchMode.RANGE,
        *,
        date_range=PERIOD,
        force=False,
    ):
        return FetchRequest(
            series_id="GDP",
            date_range=date_range,
            mode=mode,
            force=force,
        )

    def test_first_sync_and_local_query(self):
        provider = FakeGDPProvider(records=(
            ("2024-01-01", "28793.191"),
            ("2024-04-01", "29232.964"),
        ))

        request = self.request()

        result = self.service.sync_gdp(
            request,
            as_of_date=TODAY,
            provider=provider,
        )

        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.committed_ranges, 1)

        rows = self.service.get_gdp_history()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].value, 28793.191)
        self.assertEqual(
            self.service.get_gdp_coverage(),
            (PERIOD,),
        )

        self.assertEqual(
            self.service.get_sync_status(
                request.request_id
            )["status"],
            "COMPLETED",
        )

        self.assertEqual(
            self.service.get_sync_ranges(
                request.request_id
            )[0]["status"],
            "COMMITTED",
        )

    def test_cache_hit_needs_no_provider(self):
        first_provider = FakeGDPProvider(records=(
            ("2024-01-01", "28793.191"),
        ))

        self.service.sync_gdp(
            self.request(),
            as_of_date=TODAY,
            provider=first_provider,
        )

        # Do not supply a provider or API key.
        # A fully cached RANGE must not call FRED.
        result = self.service.sync_gdp(
            self.request(),
            as_of_date=TODAY,
        )

        self.assertEqual(result.status, "CACHE_HIT")
        self.assertEqual(result.planned_ranges, 0)
        self.assertEqual(result.committed_ranges, 0)

    def test_revision_creates_audit(self):
        self.service.sync_gdp(
            self.request(),
            as_of_date=TODAY,
            provider=FakeGDPProvider(records=(
                ("2024-01-01", "28793.191"),
            )),
        )

        result = self.service.sync_gdp(
            self.request(FetchMode.REFRESH),
            as_of_date=TODAY,
            provider=FakeGDPProvider(records=(
                ("2024-01-01", "29000.000"),
            )),
        )

        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.updated, 1)
        self.assertEqual(
            self.service.get_gdp_history()[0].value,
            29000.0,
        )

        actions = [
            row["action"]
            for row in self.service.get_gdp_audit()
        ]
        self.assertEqual(
            actions, ["INSERT", "UPDATE"]
        )

    def test_explicit_missing_invalidates(self):
        self.service.sync_gdp(
            self.request(),
            as_of_date=TODAY,
            provider=FakeGDPProvider(records=(
                ("2024-01-01", "28793.191"),
            )),
        )

        result = self.service.sync_gdp(
            self.request(FetchMode.REFRESH),
            as_of_date=TODAY,
            provider=FakeGDPProvider(records=(
                ("2024-01-01", "."),
            )),
        )

        self.assertEqual(result.invalidated, 1)
        self.assertEqual(
            len(self.service.get_gdp_history()), 0
        )

        self.assertEqual(
            self.service.get_gdp_audit()[-1]["action"],
            "INVALIDATE",
        )

    def test_failed_provider_preserves_storage(self):
        request = self.request()
        result = self.service.sync_gdp(
            request,
            as_of_date=TODAY,
            provider=FakeGDPProvider(fail_on=1),
        )

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "HTTP_503")
        self.assertEqual(result.committed_ranges, 0)
        self.assertEqual(
            self.service.get_gdp_history(), ()
        )
        self.assertEqual(
            self.service.get_gdp_coverage(), ()
        )

    def test_partial_sync(self):
        coverage = DateRange(
            date(2024, 7, 1),
            date(2024, 12, 31),
        )

        # Pre-existing committed coverage is established
        # using the existing tested SQLite storage API.
        self.service._storage.commit_range(
            "GDP",
            coverage,
            (),
        )

        requested = DateRange(
            date(2024, 1, 1),
            date(2025, 12, 31),
        )

        provider = FakeGDPProvider(
            records=(
                ("2024-01-01", "28793.191"),
                ("2025-01-01", "30171.979"),
            ),
            fail_on=2,
        )

        result = self.service.sync_gdp(
            self.request(date_range=requested),
            as_of_date=TODAY,
            provider=provider,
        )

        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.planned_ranges, 2)
        self.assertEqual(result.committed_ranges, 1)
        self.assertEqual(result.inserted, 1)

        self.assertEqual(
            [r["status"] for r in
             self.service.get_sync_ranges(result.request_id)],
            ["COMMITTED", "FAILED"],
        )

        self.assertEqual(
            self.service.get_gdp_history()[0].value,
            28793.191,
        )

    def test_full_mode_discovers_history(self):
        provider = FakeGDPProvider(records=())

        result = self.service.sync_gdp(
            self.request(FetchMode.FULL),
            as_of_date=TODAY,
            provider=provider,
        )

        self.assertEqual(provider.discovery_calls, 1)
        self.assertEqual(
            provider.calls[0].start,
            date(1947, 1, 1),
        )
        self.assertEqual(result.status, "COMPLETED")

    def test_existing_project_api_survives(self):
        project = self.service.create_project(
            "GDP Research"
        )

        self.assertEqual(
            self.service.get_project(project["id"])["id"],
            project["id"],
        )

        self.service.add_indicator(
            project["id"], "GDP"
        )

        self.assertEqual(
            self.service.get_project(
                project["id"]
            )["series_ids"],
            ["GDP"],
        )

        self.service.rename_project(
            project["id"], "Macro Research"
        )

        self.assertEqual(
            self.service.get_project(
                project["id"]
            )["name"],
            "Macro Research",
        )


if __name__ == "__main__":
    unittest.main()
