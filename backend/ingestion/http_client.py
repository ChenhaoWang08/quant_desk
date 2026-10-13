
"""QuantDesk FRED GDP HTTP transport (network layer only).

Flow: validate URL and parameters -> HTTPS GET -> decode JSON or classify errors
      -> retry transient failures with bounded backoff -> release responses.
No API-key loading, GDP normalization, SQLite, GUI, or credential logging.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests

from backend.providers.base import ProviderError

FRED_API_ORIGIN = "https://api.stlouisfed.org"
RETRYABLE_HTTP_STATUSES = frozenset((423, 429, 500, 502, 503, 504))


@dataclass(frozen=True, slots=True)
class HTTPConfig:
    """Transport settings; max_retries excludes the initial request."""

    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    max_retries: int = 2
    backoff_base: float = 1.0
    max_retry_delay: float = 30.0

    def __post_init__(self) -> None:
        for name in ("connect_timeout", "read_timeout", "backoff_base", "max_retry_delay"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
            if name in ("connect_timeout", "read_timeout"):
                if value <= 0:
                    raise ValueError(f"{name} must be positive")
            elif value < 0:
                raise ValueError(f"{name} cannot be negative")

        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise TypeError("max_retries must be an integer")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")


class HTTPClient:
    """Synchronous FRED client; use from one worker at a time.

    Session and sleep injection enable tests without network access.
    """

    def __init__(
        self,
        config: HTTPConfig | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or HTTPConfig()
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._sleep = sleep
        self._closed = False
        if self._owns_session:
            # Prevent automatic .netrc credentials and environment proxies.
            self._session.trust_env = False

    @staticmethod
    def _validate_url(url: str) -> None:
        if not isinstance(url, str):
            raise ProviderError("URL must be a string", provider="FRED", code="INVALID_URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ProviderError(
                "Invalid URL structure", provider="FRED", code="INVALID_URL"
            ) from None

        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.stlouisfed.org"
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or not parsed.path.startswith("/fred/")
        ):
            raise ProviderError(
                "Only official FRED HTTPS API URLs are allowed",
                provider="FRED",
                code="INVALID_URL",
            )

    @staticmethod
    def _retry_after_seconds(header: str | None) -> float | None:
        """Parse Retry-After seconds or HTTP-date."""
        if not header:
            return None
        header = header.strip()
        try:
            seconds = float(header)
            return seconds if isfinite(seconds) and seconds >= 0 else None
        except ValueError:
            pass

        try:
            deadline = parsedate_to_datetime(header)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    def _retry_delay(self, retry_index: int, retry_after: str | None = None) -> float:
        """Choose a server-defined or jittered, bounded delay."""
        server_delay = self._retry_after_seconds(retry_after)
        if server_delay is not None:
            return min(server_delay, self.config.max_retry_delay)
        exponential = self.config.backoff_base * (2 ** retry_index)
        jitter = random.uniform(0.0, 0.25)
        return min(exponential + jitter, self.config.max_retry_delay)

    @staticmethod
    def _status_error(status: int) -> ProviderError:
        if status == 429:
            return ProviderError(
                "FRED API rate limit reached", provider="FRED",
                code="RATE_LIMITED", retryable=True, http_status=status,
            )
        if status in RETRYABLE_HTTP_STATUSES:
            return ProviderError(
                "Temporary HTTP service failure", provider="FRED",
                code=f"HTTP_{status}", retryable=True, http_status=status,
            )
        if status in (400, 401, 403):
            return ProviderError(
                "FRED API rejected the request or credentials", provider="FRED",
                code=f"HTTP_{status}", retryable=False, http_status=status,
            )
        return ProviderError(
            "FRED API returned an HTTP error", provider="FRED",
            code=f"HTTP_{status}", retryable=False, http_status=status,
        )

    def get_json(
        self, url: str, *, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET a JSON object, retrying eligible failures without following redirects."""
        if self._closed:
            raise RuntimeError("HTTPClient is closed")
        self._validate_url(url)
        if params is not None:
            if not isinstance(params, Mapping):
                raise TypeError("params must be a mapping")
            for key in params:
                if not isinstance(key, str):
                    raise TypeError("parameter names must be strings")

        attempts = self.config.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._session.get(
                    url,
                    params=params,
                    timeout=(self.config.connect_timeout, self.config.read_timeout),
                    allow_redirects=False,
                )
                try:
                    status = response.status_code
                    if status == 200:
                        try:
                            payload = response.json()
                        except ValueError:
                            raise ProviderError(
                                "Invalid JSON response", provider="FRED", code="INVALID_JSON"
                            ) from None
                        if not isinstance(payload, dict):
                            raise ProviderError(
                                "Expected a JSON object",
                                provider="FRED", code="INVALID_JSON_STRUCTURE",
                            )
                        return payload

                    error = self._status_error(status)
                    if not error.retryable or attempt == attempts - 1:
                        raise error
                    retry_after = response.headers.get("Retry-After")
                finally:
                    response.close()
                self._sleep(self._retry_delay(attempt, retry_after))

            except (requests.Timeout, requests.ConnectionError) as exc:
                if isinstance(exc, requests.Timeout):
                    message, code = "HTTP request timed out", "TIMEOUT"
                else:
                    message, code = "Unable to connect to FRED", "CONNECTION_ERROR"
                if attempt == attempts - 1:
                    raise ProviderError(
                        message, provider="FRED", code=code, retryable=True
                    ) from None
                self._sleep(self._retry_delay(attempt))

            except requests.RequestException:
                raise ProviderError(
                    "HTTP transport error", provider="FRED", code="TRANSPORT_ERROR"
                ) from None

        raise RuntimeError("HTTP retry loop ended unexpectedly")

    def close(self) -> None:
        """Close the session only if this client owns it."""
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> HTTPClient:
        if self._closed:
            raise RuntimeError("HTTPClient is closed")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
