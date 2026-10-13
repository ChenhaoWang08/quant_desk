
"""QuantDesk GDP 采集流程的数据契约；不执行网络、SQLite 或 GUI 操作。

逻辑：DateRange 校验区间 → FetchRequest 描述请求 → FetchPlan 保存 Planner 划分的缓存/抓取范围
      → Observation 校验单条 GDP 记录 → FetchResult 汇总结果。
注意：FetchResult 表示采集结果，不代表 SQLite 已提交数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any
from uuid import uuid4

SUPPORTED_SERIES = frozenset(("GDP",))


class FetchMode(str, Enum):
    """How observations should be fetched."""

    RANGE = "range"
    FULL = "full"
    REFRESH = "refresh"


class FetchStatus(str, Enum):
    """Outcome of a fetch operation."""

    SUCCESS = "success"
    CACHE_HIT = "cache_hit"
    NO_OBSERVATIONS = "no_observations"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 避免每次读取 FetchResult.is_successful 时重新分配集合。
_SUCCESSFUL_STATUSES = frozenset((
    FetchStatus.SUCCESS,
    FetchStatus.CACHE_HIT,
    FetchStatus.NO_OBSERVATIONS,
))


@dataclass(frozen=True)
class DateRange:
    """Inclusive calendar-date interval."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if not isinstance(self.start, date):
            raise TypeError("start must be a date")
        if not isinstance(self.end, date):
            raise TypeError("end must be a date")
        if isinstance(self.start, datetime):
            raise TypeError("start must not be a datetime")
        if isinstance(self.end, datetime):
            raise TypeError("end must not be a datetime")
        if self.start > self.end:
            raise ValueError("Date range start cannot be after end")

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class FetchRequest:
    """Description of a GDP fetch request; no network access."""

    series_id: str
    date_range: DateRange
    mode: FetchMode = FetchMode.RANGE
    force: bool = False
    request_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.series_id not in SUPPORTED_SERIES:
            raise ValueError(f"Unsupported series: {self.series_id}")
        if not isinstance(self.date_range, DateRange):
            raise TypeError("date_range must be a DateRange")
        if not isinstance(self.mode, FetchMode):
            raise TypeError("mode must be a FetchMode")
        if not isinstance(self.force, bool):
            raise TypeError("force must be a boolean")


@dataclass(frozen=True)
class Observation:
    """Single finite-valued GDP observation."""

    series_id: str
    observation_date: date
    value: float

    def __post_init__(self) -> None:
        if self.series_id not in SUPPORTED_SERIES:
            raise ValueError(f"Unsupported series: {self.series_id}")
        if not isinstance(self.observation_date, date) or isinstance(
            self.observation_date, datetime
        ):
            raise TypeError("observation_date must be a date")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError("Observation value must be numeric")

        numeric_value = float(self.value)
        if not isfinite(numeric_value):
            raise ValueError("Observation value must be finite")
        object.__setattr__(self, "value", numeric_value)


@dataclass(frozen=True)
class FetchPlan:
    """Cached and network ranges selected by the planner."""

    request: FetchRequest
    fetch_ranges: tuple[DateRange, ...]
    cached_ranges: tuple[DateRange, ...]
    reason: str

    @property
    def requires_network(self) -> bool:
        return len(self.fetch_ranges) > 0

    @property
    def estimated_requests(self) -> int:
        return len(self.fetch_ranges)


@dataclass
class FetchResult:
    """Fetch outcome, not confirmation of a SQLite commit."""

    request_id: str
    series_id: str
    status: FetchStatus
    observations: list[Observation] = field(default_factory=list)
    fetched_ranges: list[DateRange] = field(default_factory=list)
    missing_dates: list[date] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "FRED"
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def is_successful(self) -> bool:
        return self.status in _SUCCESSFUL_STATUSES

    def summary(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "series_id": self.series_id,
            "status": self.status.value,
            "source": self.source,
            "observation_count": self.observation_count,
            "missing_count": len(self.missing_dates),
            "fetched_at": self.fetched_at.isoformat(),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }
