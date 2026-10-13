
"""Coordinate QuantDesk GDP ingestion without persistent storage.

Flow:
    FetchRequest + committed coverage
        -> FetchPlanner
        -> DataProvider
        -> GDPValidator
        -> GDPNormalizer
        -> FetchResult

The engine performs no direct HTTP, SQLite, or GUI operations.
It delegates network access to DataProvider and never treats
successful fetching as proof of a successful database commit.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from backend.models import (
    DateRange,
    FetchMode,
    FetchRequest,
    FetchResult,
    FetchStatus,
    Observation,
)
from backend.providers.base import DataProvider, ProviderError
from backend.ingestion.planner import FetchPlanner
from backend.ingestion.validator import GDPValidator
from backend.ingestion.normalizer import GDPNormalizer


class GDPIngestionEngine:
    """Execute planned GDP fetches using injected components.

    One run is synchronous. A future GUI integration must
    execute it outside the PySide6 main thread.

    The caller must supply verified, committed coverage.
    This engine never creates or commits coverage records.
    """

    def __init__(
        self,
        provider: DataProvider,
        *,
        planner: FetchPlanner | None = None,
        validator: GDPValidator | None = None,
        normalizer: GDPNormalizer | None = None,
    ) -> None:
        if not isinstance(provider, DataProvider):
            raise TypeError("provider must be a DataProvider")

        self.provider = provider
        self.planner = planner if planner is not None else FetchPlanner()
        self.validator = validator if validator is not None else GDPValidator()
        self.normalizer = (
            normalizer if normalizer is not None else GDPNormalizer()
        )

    @staticmethod
    def _result(
        request: FetchRequest,
        status: FetchStatus,
        *,
        observations: list[Observation] | None = None,
        missing_dates: list[date] | None = None,
        fetched_ranges: list[DateRange] | None = None,
        error: ProviderError | None = None,
        metadata: dict | None = None,
    ) -> FetchResult:
        """Build one result without altering the input request."""
        return FetchResult(
            request_id=request.request_id,
            series_id=request.series_id,
            status=status,
            observations=observations if observations is not None else [],
            missing_dates=missing_dates if missing_dates is not None else [],
            fetched_ranges=fetched_ranges if fetched_ranges is not None else [],
            error_code=error.code if error is not None else None,
            error_message=str(error) if error is not None else None,
            metadata=metadata if metadata is not None else {},
        )

    def run(
        self,
        request: FetchRequest,
        coverage: Iterable[DateRange],
        *,
        as_of_date: date,
        earliest_available_date: date | None = None,
    ) -> FetchResult:
        """Execute a GDP fetch plan without writing to SQLite.

        Args:
            request:
                GDP fetch request.

            coverage:
                Successfully committed historical coverage
                supplied by a trusted storage layer.

            as_of_date:
                Effective date used by the planner.

            earliest_available_date:
                Optional known historical starting date.
                If absent in FULL mode, query the provider.

        Returns:
            FetchResult containing newly fetched observations,
            explicit missing dates, successful fetch ranges,
            and an execution status.

        Raises:
            TypeError or PlanningError for invalid planning
            inputs. Unexpected programming errors propagate.
        """
        if not isinstance(request, FetchRequest):
            raise TypeError("request must be FetchRequest")

        if not self.provider.supports(request.series_id):
            raise ValueError(
                f"Provider does not support {request.series_id}"
            )

        # FULL requires the provider's historical starting date.
        # A failure here must not be interpreted as a cache hit.
        if (
            request.mode is FetchMode.FULL
            and earliest_available_date is None
        ):
            try:
                earliest_available_date = (
                    self.provider.get_earliest_available_date(
                        request.series_id
                    )
                )
            except ProviderError as error:
                return self._result(
                    request,
                    FetchStatus.FAILED,
                    error=error,
                    metadata={"stage": "history_discovery"},
                )

        plan = self.planner.plan(
            request,
            coverage,
            as_of_date=as_of_date,
            earliest_available_date=earliest_available_date,
        )

        metadata = {
            "mode": request.mode.value,
            "reason": plan.reason,
            "planned_ranges": [
                item.to_dict() for item in plan.fetch_ranges
            ],
            "cached_ranges": [
                item.to_dict() for item in plan.cached_ranges
            ],
            "attempted_ranges": 0,
            "completed_ranges": 0,
            "committed": False,
        }

        if not plan.requires_network:
            return self._result(
                request,
                FetchStatus.CACHE_HIT,
                metadata=metadata,
            )

        observations: list[Observation] = []
        missing_dates: list[date] = []
        fetched_ranges: list[DateRange] = []

        # The planner supplies sorted, nonoverlapping ranges.
        # Each range must pass both validation and normalization.
        for current_range in plan.fetch_ranges:
            metadata["attempted_ranges"] += 1

            try:
                response = self.provider.fetch_observations(
                    series_id=request.series_id,
                    date_range=current_range,
                    realtime_date=as_of_date,
                )

                validated = self.validator.validate(response)

                normalized = self.normalizer.normalize(validated)

                # Verify that every layer kept the requested
                # series and interval unchanged.
                if (
                    normalized.series_id != request.series_id
                    or normalized.requested_range != current_range
                ):
                    raise ProviderError(
                        "Ingestion response does not match request",
                        provider="FRED",
                        code="RESULT_MISMATCH",
                    )

            except ProviderError as error:
                metadata["failed_range"] = current_range.to_dict()
                metadata["stage"] = "fetch_or_validation"

                status = (
                    FetchStatus.PARTIAL
                    if fetched_ranges
                    else FetchStatus.FAILED
                )

                return self._result(
                    request,
                    status,
                    observations=observations,
                    missing_dates=missing_dates,
                    fetched_ranges=fetched_ranges,
                    error=error,
                    metadata=metadata,
                )

            # Only add records after an entire individual
            # range has been successfully normalized.
            observations.extend(normalized.observations)
            missing_dates.extend(normalized.missing_dates)
            fetched_ranges.append(current_range)

            metadata["completed_ranges"] += 1

        # No valid observations is different from a network
        # or validation failure. Explicit missing dates are
        # still preserved in the result.
        status = (
            FetchStatus.SUCCESS
            if observations
            else FetchStatus.NO_OBSERVATIONS
        )

        return self._result(
            request,
            status,
            observations=observations,
            missing_dates=missing_dates,
            fetched_ranges=fetched_ranges,
            metadata=metadata,
        )
