
"""QuantDesk FRED GDP validation, independent of HTTP, SQLite, and GUI.

Flow: verify ProviderResponse -> validate date/order/quarter and raw values
      -> separate finite positive GDP values from '.' missing markers
      -> return ValidationResult; reject the entire response on any error.
Raw records remain strings; no Observation conversion or silent filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from re import compile as re_compile, fullmatch

from backend.models import DateRange
from backend.providers.base import ProviderError, ProviderResponse, RawObservation

MISSING_VALUE_MARKER = "."
GDP_SERIES_ID = "GDP"
GDP_PROVIDER_NAME = "FRED"
_DATE_PATTERN = re_compile(r"\d{4}-\d{2}-\d{2}")


class ValidationError(ProviderError):
    """Invalid or inconsistent FRED GDP response."""

    def __init__(self, message: str, *, code: str = "VALIDATION_ERROR") -> None:
        super().__init__(
            message, provider=GDP_PROVIDER_NAME, code=code, retryable=False
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validated raw GDP data; does not confirm SQLite persistence."""

    series_id: str
    requested_range: DateRange
    realtime_date: date
    valid_observations: tuple[RawObservation, ...]
    missing_dates: tuple[date, ...]
    total_count: int

    @property
    def valid_count(self) -> int:
        return len(self.valid_observations)

    @property
    def missing_count(self) -> int:
        return len(self.missing_dates)

    @property
    def is_empty(self) -> bool:
        return self.total_count == 0

    @property
    def has_missing_values(self) -> bool:
        return self.missing_count > 0


class GDPValidator:
    """Validate a complete GDP response; any malformed record fails it."""

    @staticmethod
    def _parse_observation_date(raw_date: str) -> date:
        """Accept only canonical YYYY-MM-DD calendar dates."""
        if not isinstance(raw_date, str) or _DATE_PATTERN.fullmatch(raw_date) is None:
            raise ValidationError(
                "Observation date must use YYYY-MM-DD",
                code="INVALID_OBSERVATION_DATE",
            )
        try:
            return date.fromisoformat(raw_date)
        except ValueError:
            raise ValidationError(
                "Invalid calendar date in GDP observation",
                code="INVALID_OBSERVATION_DATE",
            ) from None

    @staticmethod
    def _validate_numeric_value(raw_value: str) -> None:
        """Check for finite, positive GDP while preserving the raw string."""
        if not isinstance(raw_value, str):
            raise ValidationError("GDP value must be a string", code="INVALID_VALUE_TYPE")
        if not raw_value or raw_value != raw_value.strip():
            raise ValidationError(
                "GDP value is empty or contains surrounding whitespace",
                code="INVALID_GDP_VALUE",
            )
        try:
            value = float(raw_value)
        except (ValueError, OverflowError):
            raise ValidationError(
                "GDP value is not numeric", code="INVALID_GDP_VALUE"
            ) from None
        if not isfinite(value):
            raise ValidationError("GDP value must be finite", code="NON_FINITE_VALUE")
        if value <= 0:
            raise ValidationError(
                "Nominal GDP value must be positive", code="INVALID_GDP_VALUE"
            )

    @staticmethod
    def _validate_response(response: ProviderResponse) -> None:
        if not isinstance(response, ProviderResponse):
            raise TypeError("response must be a ProviderResponse")
        if response.provider_name != GDP_PROVIDER_NAME:
            raise ValidationError("Unexpected data provider", code="INVALID_PROVIDER")
        if response.series_id != GDP_SERIES_ID:
            raise ValidationError("Only GDP is supported", code="UNSUPPORTED_SERIES")
        if response.total_count != len(response.observations):
            raise ValidationError(
                "Incomplete GDP provider response", code="INCOMPLETE_RESPONSE"
            )
        if not isinstance(response.realtime_date, date) or isinstance(
            response.realtime_date, datetime
        ):
            raise ValidationError("Invalid realtime date", code="INVALID_REALTIME_DATE")

    def validate(self, response: ProviderResponse) -> ValidationResult:
        """Validate observations in order and split valid values from missing ones."""
        self._validate_response(response)

        valid: list[RawObservation] = []
        missing: list[date] = []
        previous_date: date | None = None
        parse_date = self._parse_observation_date
        validate_value = self._validate_numeric_value
        contains = response.requested_range.contains

        for record in response.observations:
            if not isinstance(record, RawObservation):
                raise ValidationError(
                    "Unexpected observation type", code="INVALID_OBSERVATION"
                )
            observed_at = parse_date(record.observation_date)
            if not contains(observed_at):
                raise ValidationError(
                    "Observation outside requested range", code="OUT_OF_RANGE"
                )
            if previous_date is not None and observed_at <= previous_date:
                raise ValidationError(
                    "GDP dates must be strictly increasing", code="INVALID_DATE_ORDER"
                )
            previous_date = observed_at
            if observed_at.day != 1 or observed_at.month not in (1, 4, 7, 10):
                raise ValidationError(
                    "Unexpected GDP quarterly date", code="INVALID_QUARTER_DATE"
                )
            if record.value == MISSING_VALUE_MARKER:
                missing.append(observed_at)
                continue
            validate_value(record.value)
            valid.append(record)

        return ValidationResult(
            series_id=response.series_id,
            requested_range=response.requested_range,
            realtime_date=response.realtime_date,
            valid_observations=tuple(valid),
            missing_dates=tuple(missing),
            total_count=response.total_count,
        )
