
"""Coordinate QuantDesk GDP fetching and durable SQLite synchronization.

Flow:
    FetchRequest -> committed coverage -> FetchPlanner
    -> register sync run -> register each range
    -> DataProvider -> GDPValidator -> GDPNormalizer
    -> SyncStorage atomic range commit -> PersistentSyncResult.

No GUI, direct SQL, API-key storage, or duplicated GDP parsing.

A successful HTTP fetch is never treated as a database commit.
The provider is created lazily, so a cache hit needs no API key.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Callable

from backend.models import DateRange, FetchMode, FetchRequest
from backend.providers.base import DataProvider, ProviderError
from backend.providers.fred import FredProvider
from backend.ingestion.planner import FetchPlanner
from backend.ingestion.validator import GDPValidator
from backend.ingestion.normalizer import GDPNormalizer
from backend.sync_storage import SyncStorage


@dataclass(frozen=True, slots=True)
class PersistentSyncResult:
    """Outcome of a persisted GDP synchronization run."""

    request_id: str
    series_id: str
    status: str
    planned_ranges: int
    committed_ranges: int
    inserted: int
    updated: int
    unchanged: int
    invalidated: int
    reinstated: int
    unconfirmed: int
    error_code: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status in ("COMPLETED", "CACHE_HIT")


class GDPSyncCoordinator:
    """Coordinate one synchronous GDP run.

    SyncStorage must be initialized before calling sync().
    Only one active synchronization may write to the database.
    Do not execute sync() on the PySide6 main UI thread.
    """

    def __init__(
        self,
        storage: SyncStorage,
        *,
        planner: FetchPlanner | None = None,
        validator: GDPValidator | None = None,
        normalizer: GDPNormalizer | None = None,
        provider_factory: Callable[[], DataProvider] = FredProvider,
    ) -> None:
        if not isinstance(storage, SyncStorage):
            raise TypeError("storage must be SyncStorage")

        self.storage = storage
        self.planner = planner if planner is not None else FetchPlanner()
        self.validator = validator if validator is not None else GDPValidator()
        self.normalizer = (
            normalizer if normalizer is not None else GDPNormalizer()
        )
        self.provider_factory = provider_factory

    @staticmethod
    def _check_provider(
        provider: DataProvider,
        series_id: str,
    ) -> None:
        if not isinstance(provider, DataProvider):
            raise TypeError("provider must implement DataProvider")

        if not provider.supports(series_id):
            raise ValueError(
                f"Provider does not support {series_id}"
            )

    def sync(
        self,
        request: FetchRequest,
        *,
        as_of_date: date,
        provider: DataProvider | None = None,
        earliest_available_date: date | None = None,
    ) -> PersistentSyncResult:
        """Synchronize one GDP request and persist committed ranges.

        The caller may inject a provider for deterministic tests.
        An owned provider is closed when execution ends.

        Planning errors and unrecoverable storage errors propagate.
        Provider failures inside registered ranges become FAILED
        range records and a durable final run status.
        """
        if not isinstance(request, FetchRequest):
            raise TypeError("request must be FetchRequest")

        if request.series_id != "GDP":
            raise ValueError("Only GDP is supported")

        if type(as_of_date) is not date:
            raise TypeError("as_of_date must be a date")

        owned_provider = None

        def acquire_provider() -> DataProvider:
            nonlocal owned_provider

            if provider is not None:
                current = provider
            else:
                if owned_provider is None:
                    owned_provider = self.provider_factory()
                current = owned_provider

            self._check_provider(current, request.series_id)
            return current

        try:
            # No provider is required for RANGE cache hits.
            # FULL needs the earliest historical observation date.
            if (
                request.mode is FetchMode.FULL
                and earliest_available_date is None
            ):
                earliest_available_date = (
                    acquire_provider().get_earliest_available_date(
                        request.series_id
                    )
                )

            coverage = self.storage.base.read_coverage(
                request.series_id
            )

            plan = self.planner.plan(
                request,
                coverage,
                as_of_date=as_of_date,
                earliest_available_date=earliest_available_date,
            )

            # Register the run after successful planning.
            # No HTTP request is made while holding a write transaction.
            self.storage.begin_run(
                request.request_id,
                series_id=request.series_id,
                mode=request.mode.value,
            )

            for index, current_range in enumerate(
                plan.fetch_ranges
            ):
                range_id = self.storage.start_range(
                    request.request_id,
                    index,
                    current_range,
                )

                try:
                    response = acquire_provider().fetch_observations(
                        series_id=request.series_id,
                        date_range=current_range,
                        realtime_date=as_of_date,
                    )

                    validated = self.validator.validate(response)
                    normalized = self.normalizer.normalize(validated)

                    if (
                        normalized.series_id != request.series_id
                        or normalized.requested_range != current_range
                    ):
                        raise ProviderError(
                            "Ingestion response does not match request",
                            provider="FRED",
                            code="RESULT_MISMATCH",
                        )

                    # The entire range is already validated.
                    # This method atomically updates observations,
                    # coverage, audit records and sync-range state.
                    self.storage.commit_range(
                        range_id,
                        normalized.observations,
                        missing_dates=normalized.missing_dates,
                        source="FRED",
                    )

                except ProviderError as error:
                    self.storage.fail_range(range_id, error.code)
                    break

                except (
                    sqlite3.DatabaseError,
                    OSError,
                ):
                    # The SQLite transaction must have rolled back.
                    # If failure logging also fails, let that error
                    # propagate; never report successful completion.
                    self.storage.fail_range(
                        range_id, "STORAGE_ERROR"
                    )
                    break

            status = self.storage.finish_run(request.request_id)
            ranges = self.storage.read_ranges(request.request_id)

            def total(field: str) -> int:
                return sum(int(row[field]) for row in ranges)

            error_code = next(
                (
                    row["error_code"]
                    for row in ranges
                    if row["error_code"] is not None
                ),
                None,
            )

            return PersistentSyncResult(
                request_id=request.request_id,
                series_id=request.series_id,
                status=status,
                planned_ranges=len(plan.fetch_ranges),
                committed_ranges=sum(
                    row["status"] == "COMMITTED"
                    for row in ranges
                ),
                inserted=total("inserted_count"),
                updated=total("updated_count"),
                unchanged=total("unchanged_count"),
                invalidated=total("invalidated_count"),
                reinstated=total("reinstated_count"),
                unconfirmed=total("unconfirmed_count"),
                error_code=error_code,
            )

        finally:
            if owned_provider is not None:
                owned_provider.close()
