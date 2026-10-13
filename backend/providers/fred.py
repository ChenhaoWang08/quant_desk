
"""QuantDesk FRED GDP provider: obtain raw observations, not normalized values.

Flow: validate input -> build FRED request -> fetch and validate each page
      -> check page ordering/completeness -> return ProviderResponse.
No SQLite, GUI, or GDP-value normalization. The API key comes from an
explicit argument or FRED_API_KEY; HTTP transport is delegated to HTTPClient.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from backend.ingestion.http_client import FRED_API_ORIGIN, HTTPClient
from backend.models import DateRange, SUPPORTED_SERIES
from backend.providers.base import (
    DataProvider,
    ProviderError,
    ProviderResponse,
    RawObservation,
)

OBSERVATIONS_URL = FRED_API_ORIGIN + "/fred/series/observations"
FRED_PAGE_LIMIT = 100000
MAX_PAGES = 100


class FredProvider(DataProvider):
    """FRED provider restricted to the GDP series."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: HTTPClient | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("FRED_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise ProviderError(
                "FRED_API_KEY is not configured",
                provider="FRED", code="MISSING_API_KEY",
            )

        self._api_key = key.strip()
        self._http = http_client or HTTPClient()
        self._owns_http = http_client is None
        self._closed = False

    @property
    def name(self) -> str:
        return "FRED"

    def supports(self, series_id: str) -> bool:
        return series_id in SUPPORTED_SERIES

    @staticmethod
    def _validate_series(series_id: str) -> None:
        if series_id not in SUPPORTED_SERIES:
            raise ValueError(f"Unsupported FRED series: {series_id}")

    @staticmethod
    def _validate_date(value: date) -> None:
        if not isinstance(value, date) or isinstance(value, datetime):
            raise TypeError("realtime_date must be a date")

    def _request_page(
        self,
        *,
        series_id: str,
        date_range: DateRange,
        realtime_date: date,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("FredProvider is closed")
        self._validate_series(series_id)
        self._validate_date(realtime_date)

        return self._http.get_json(
            OBSERVATIONS_URL,
            params={
                "api_key": self._api_key,
                "file_type": "json",
                "series_id": series_id,
                "observation_start": date_range.start.isoformat(),
                "observation_end": date_range.end.isoformat(),
                "units": "lin",
                "output_type": 1,
                "sort_order": "asc",
                "limit": limit,
                "offset": offset,
            },
        )

    @staticmethod
    def _parse_page(
        payload: dict[str, Any],
        *,
        expected_offset: int,
        requested_range: DateRange,
    ) -> tuple[int, tuple[RawObservation, ...]]:
        """Validate FRED pagination metadata, dates, and raw observation types."""
        try:
            total = int(payload["count"])
            offset = int(payload["offset"])
            limit = int(payload["limit"])
            records = payload["observations"]
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError(
                "Invalid FRED response metadata",
                provider="FRED", code="INVALID_RESPONSE",
            ) from None

        if total < 0 or offset != expected_offset or limit <= 0 or not isinstance(records, list):
            raise ProviderError(
                "Inconsistent FRED pagination metadata",
                provider="FRED", code="INVALID_PAGINATION",
            )
        if len(records) != min(limit, max(0, total - offset)):
            raise ProviderError(
                "Incomplete FRED response page",
                provider="FRED", code="INCOMPLETE_PAGE",
            )

        observations: list[RawObservation] = []
        previous_date: date | None = None
        contains = requested_range.contains

        for record in records:
            if not isinstance(record, dict):
                raise ProviderError(
                    "Invalid observation structure",
                    provider="FRED", code="INVALID_OBSERVATION",
                )

            raw_date, raw_value = record.get("date"), record.get("value")
            if not isinstance(raw_date, str) or not isinstance(raw_value, str):
                raise ProviderError(
                    "Invalid observation fields",
                    provider="FRED", code="INVALID_OBSERVATION",
                )
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError:
                raise ProviderError(
                    "Invalid FRED observation date",
                    provider="FRED", code="INVALID_OBSERVATION_DATE",
                ) from None

            if not contains(parsed_date):
                raise ProviderError(
                    "Observation outside requested range",
                    provider="FRED", code="OUT_OF_RANGE",
                )
            if previous_date is not None and parsed_date <= previous_date:
                raise ProviderError(
                    "Observations are not strictly ordered",
                    provider="FRED", code="INVALID_ORDER",
                )

            previous_date = parsed_date
            observations.append(RawObservation(observation_date=raw_date, value=raw_value))

        return total, tuple(observations)

    def fetch_observations(
        self,
        *,
        series_id: str,
        date_range: DateRange,
        realtime_date: date,
    ) -> ProviderResponse:
        """Fetch the complete inclusive interval, verifying every response page."""
        self._validate_series(series_id)
        if not isinstance(date_range, DateRange):
            raise TypeError("date_range must be DateRange")
        self._validate_date(realtime_date)

        all_observations: list[RawObservation] = []
        total_count: int | None = None
        offset = pages = 0
        last_date: date | None = None

        while True:
            if pages >= MAX_PAGES:
                raise ProviderError(
                    "FRED pagination safety limit reached",
                    provider="FRED", code="PAGE_LIMIT_EXCEEDED",
                )

            payload = self._request_page(
                series_id=series_id,
                date_range=date_range,
                realtime_date=realtime_date,
                offset=offset,
                limit=FRED_PAGE_LIMIT,
            )
            count, observations = self._parse_page(
                payload, expected_offset=offset, requested_range=date_range,
            )
            if total_count is None:
                total_count = count
            elif count != total_count:
                raise ProviderError(
                    "FRED result count changed during pagination",
                    provider="FRED", code="INCONSISTENT_COUNT",
                )

            if observations:
                if last_date is not None and date.fromisoformat(
                    observations[0].observation_date
                ) <= last_date:
                    raise ProviderError(
                        "Overlapping FRED pages",
                        provider="FRED", code="INVALID_PAGE_ORDER",
                    )
                last_date = date.fromisoformat(observations[-1].observation_date)

            all_observations.extend(observations)
            pages += 1
            offset += len(observations)
            if offset >= total_count:
                break
            if not observations:
                raise ProviderError(
                    "Pagination made no progress",
                    provider="FRED", code="PAGINATION_STALLED",
                )

        return ProviderResponse(
            provider_name=self.name,
            series_id=series_id,
            requested_range=date_range,
            observations=tuple(all_observations),
            total_count=total_count,
            realtime_date=realtime_date,
            metadata={"pages": pages, "source": "FRED", "units": "lin", "output_type": 1},
        )

    def get_earliest_available_date(self, series_id: str) -> date:
        """Read only the earliest observation (limit=1), not all history."""
        self._validate_series(series_id)
        today = date.today()
        historical_range = DateRange(date(1776, 7, 4), today)
        payload = self._request_page(
            series_id=series_id,
            date_range=historical_range,
            realtime_date=today,
            offset=0,
            limit=1,
        )
        count, observations = self._parse_page(
            payload, expected_offset=0, requested_range=historical_range,
        )
        if count == 0 or not observations:
            raise ProviderError(
                "No historical GDP observations available",
                provider="FRED", code="NO_HISTORY",
            )
        return date.fromisoformat(observations[0].observation_date)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> FredProvider:
        if self._closed:
            raise RuntimeError("FredProvider is closed")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
