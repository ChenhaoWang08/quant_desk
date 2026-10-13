
"""QuantDesk 外部数据提供者的数据契约（当前仅支持 FRED GDP）。

流程：DataProvider 获取原始记录 -> ProviderResponse 验证结构与完整数量
     -> 后续模块执行数值标准化和持久化；本文件不执行 HTTP、密钥读取、SQLite 或 GUI 操作。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from backend.models import DateRange, SUPPORTED_SERIES


@dataclass(frozen=True, slots=True)
class RawObservation:
    """Provider 原始观测；保留字符串，以便后续处理 '.' 等缺失标记。"""

    observation_date: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.observation_date, str):
            raise TypeError("observation_date must be a string")
        if not isinstance(self.value, str):
            raise TypeError("value must be a string")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """已成功传输和结构解析的数据；不代表数值有效或数据库已提交。"""

    provider_name: str
    series_id: str
    requested_range: DateRange
    observations: tuple[RawObservation, ...]
    total_count: int
    realtime_date: date
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_name, str):
            raise TypeError("provider_name must be a string")
        if not self.provider_name.strip():
            raise ValueError("provider_name cannot be empty")
        if self.series_id not in SUPPORTED_SERIES:
            raise ValueError(f"Unsupported series: {self.series_id}")
        if not isinstance(self.requested_range, DateRange):
            raise TypeError("requested_range must be a DateRange")
        if not isinstance(self.observations, tuple):
            raise TypeError("observations must be a tuple")

        # 直接循环避免生成器分配；保留原有的短路行为与错误信息。
        for item in self.observations:
            if not isinstance(item, RawObservation):
                raise TypeError("All observations must be RawObservation")

        if isinstance(self.total_count, bool) or not isinstance(self.total_count, int):
            raise TypeError("total_count must be an integer")
        if self.total_count < 0:
            raise ValueError("total_count cannot be negative")
        if self.total_count != len(self.observations):
            raise ValueError(
                "ProviderResponse must contain all "
                "observations in the requested result"
            )
        if not isinstance(self.realtime_date, date) or hasattr(self.realtime_date, "hour"):
            raise TypeError("realtime_date must be a date")


class DataProvider(ABC):
    """外部经济数据源接口；具体实现负责网络访问和分页。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider's stable identifier."""

    @abstractmethod
    def supports(self, series_id: str) -> bool:
        """Check whether the provider supports a series."""

    @abstractmethod
    def fetch_observations(
        self,
        *,
        series_id: str,
        date_range: DateRange,
        realtime_date: date,
    ) -> ProviderResponse:
        """Fetch all observations for an inclusive range, handling pagination.

        Raises:
            ValueError: Unsupported series or invalid parameters.
            ProviderError: Transport, API, or response failure.
        """

    @abstractmethod
    def get_earliest_available_date(self, series_id: str) -> date:
        """Return the earliest available observation date."""


class ProviderError(Exception):
    """Base exception for provider-level failures."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        code: str = "PROVIDER_ERROR",
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.http_status = http_status

    def __str__(self) -> str:
        return f"[{self.provider}:{self.code}] {super().__str__()}"
