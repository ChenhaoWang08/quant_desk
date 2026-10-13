
"""QuantDesk GDP normalization (no HTTP, SQLite, or GUI).

Flow: ValidationResult -> parse dates/positive finite GDP values -> immutable
Observation records -> validate and preserve missing dates -> NormalizedResult.
A malformed record rejects the entire batch; no interpolation or fabricated data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite

from backend.models import DateRange, Observation
from backend.providers.base import ProviderError, RawObservation
from backend.ingestion.validator import ValidationResult

GDP_SERIES_ID = "GDP"
GDP_PROVIDER_NAME = "FRED"


class NormalizationError(ProviderError):
    """GDP normalization failure; always non-retryable."""

    def __init__(self, message: str, *, code: str = "NORMALIZATION_ERROR") -> None:
        super().__init__(
            message, provider=GDP_PROVIDER_NAME, code=code, retryable=False
        )


@dataclass(frozen=True, slots=True)
class NormalizedResult:
    """Normalized data; not proof that SQLite committed the observations."""

    series_id: str
    requested_range: DateRange
    realtime_date: date
    observations: tuple[Observation, ...]
    missing_dates: tuple[date, ...]
    total_count: int

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def missing_count(self) -> int:
        return len(self.missing_dates)

    @property
    def is_empty(self) -> bool:
        return self.total_count == 0

    @property
    def has_missing_values(self) -> bool:
        return bool(self.missing_dates)


class GDPNormalizer:
    """Convert validated FRED GDP strings into immutable observations."""

    @staticmethod
    def _normalize_observation(record: RawObservation) -> Observation:
        if not isinstance(record, RawObservation):
            raise NormalizationError(
                "Expected RawObservation", code="INVALID_OBSERVATION"
            )
        try:
            observed_at = date.fromisoformat(record.observation_date)
        except (TypeError, ValueError):
            raise NormalizationError(
                "Unable to parse GDP observation date", code="INVALID_DATE"
            ) from None

        try:
            numeric_value = float(record.value)
        except (TypeError, ValueError, OverflowError):
            raise NormalizationError(
                "Unable to convert GDP value", code="INVALID_VALUE"
            ) from None

        if not isfinite(numeric_value):
            raise NormalizationError(
                "GDP value must be finite", code="NON_FINITE_VALUE"
            )
        if numeric_value <= 0:
            raise NormalizationError(
                "Nominal GDP must be positive", code="INVALID_VALUE"
            )
        try:
            return Observation(
                series_id=GDP_SERIES_ID,
                observation_date=observed_at,
                value=numeric_value,
            )
        except (TypeError, ValueError, OverflowError):
            raise NormalizationError(
                "Unable to construct GDP Observation", code="INVALID_OBSERVATION"
            ) from None

    def normalize(self, result: ValidationResult) -> NormalizedResult:
        """Normalize every valid record, then validate missing-date consistency."""
        if not isinstance(result, ValidationResult):
            raise TypeError("result must be a ValidationResult")
        if result.series_id != GDP_SERIES_ID:
            raise NormalizationError("Only GDP is supported", code="UNSUPPORTED_SERIES")
        if not isinstance(result.requested_range, DateRange):
            raise NormalizationError("Invalid requested range", code="INVALID_RANGE")

        requested_range = result.requested_range
        contains = requested_range.contains
        normalize_one = self._normalize_observation
        observations: list[Observation] = []
        previous_date: date | None = None

        for record in result.valid_observations:
            observation = normalize_one(record)
            current_date = observation.observation_date
            if not contains(current_date):
                raise NormalizationError(
                    "Observation outside requested range", code="OUT_OF_RANGE"
                )
            if previous_date is not None and current_date <= previous_date:
                raise NormalizationError(
                    "Observations must be strictly ordered", code="INVALID_DATE_ORDER"
                )
            previous_date = current_date
            observations.append(observation)

        missing_dates = result.missing_dates
        if not isinstance(missing_dates, tuple):
            raise NormalizationError(
                "missing_dates must be a tuple", code="INVALID_MISSING_DATES"
            )

        previous_missing: date | None = None
        for missing_date in missing_dates:
            # Exact date type is required; datetime and date subclasses are invalid.
            if type(missing_date) is not date:
                raise NormalizationError(
                    "Invalid missing observation date", code="INVALID_MISSING_DATES"
                )
            if not contains(missing_date):
                raise NormalizationError(
                    "Missing date outside requested range", code="OUT_OF_RANGE"
                )
            if previous_missing is not None and missing_date <= previous_missing:
                raise NormalizationError(
                    "Missing dates must be strictly ordered", code="INVALID_MISSING_DATES"
                )
            previous_missing = missing_date

        # No missing values -> skip the otherwise unnecessary date-index allocation.
        if observations and missing_dates:
            valid_dates = {observation.observation_date for observation in observations}
            if any(missing_date in valid_dates for missing_date in missing_dates):
                raise NormalizationError(
                    "A GDP date cannot be both valid and missing",
                    code="CONFLICTING_DATES",
                )

        if (
            isinstance(result.total_count, bool)
            or not isinstance(result.total_count, int)
            or result.total_count < 0
            or result.total_count != len(observations) + len(missing_dates)
        ):
            raise NormalizationError(
                "Normalized GDP counts are inconsistent", code="COUNT_MISMATCH"
            )

        return NormalizedResult(
            series_id=result.series_id,
            requested_range=requested_range,
            realtime_date=result.realtime_date,
            observations=tuple(observations),
            missing_dates=missing_dates,
            total_count=result.total_count,
        )
