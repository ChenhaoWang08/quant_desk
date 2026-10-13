
"""QuantDesk SQLite persistence for committed economic observations.

Flow: validate complete Observation batch -> BEGIN IMMEDIATE -> classify and
UPSERT observations -> replace overlapping coverage, retaining timestamps on
untouched portions -> COMMIT or ROLLBACK. No HTTP, GUI, or normalization.
Only a completed transaction establishes committed coverage.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Iterable

from backend.models import DateRange, Observation


_UPSERT_OBSERVATION = (
    "INSERT INTO observations (series_id, date, value) VALUES (?, ?, ?) "
    "ON CONFLICT(series_id, date) DO UPDATE SET value = excluded.value "
    "WHERE observations.value != excluded.value"
)


@dataclass(frozen=True, slots=True)
class CommitSummary:
    """Result of one successful SQLite transaction."""

    inserted: int
    updated: int
    unchanged: int
    coverage: DateRange
    checked_at: datetime


class SQLiteStore:
    """Local SQLite storage with atomic observation/coverage commits."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=10000")
        except Exception:
            db.close()
            raise
        return db

    def initialize(self) -> None:
        """Create required tables without deleting existing records."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            with db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS observations (
                        series_id TEXT NOT NULL,
                        date TEXT NOT NULL,
                        value REAL NOT NULL,
                        PRIMARY KEY (series_id, date)
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS coverage (
                        series_id TEXT NOT NULL,
                        start_date TEXT NOT NULL,
                        end_date TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        PRIMARY KEY (series_id, start_date, end_date),
                        CHECK (start_date <= end_date)
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS sync_runs (
                        request_id TEXT PRIMARY KEY,
                        series_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        fetched_count INTEGER NOT NULL DEFAULT 0,
                        inserted_count INTEGER NOT NULL DEFAULT 0,
                        updated_count INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT
                    )
                """)

    def count_observations(self, series_id: str | None = None) -> int:
        """Count all observations or just one series."""
        sql = "SELECT COUNT(*) FROM observations"
        params = ()
        if series_id is not None:
            sql += " WHERE series_id = ?"
            params = (series_id,)
        with closing(self._connect()) as db:
            return db.execute(sql, params).fetchone()[0]

    def read_observations(
        self, series_id: str, date_range: DateRange | None = None
    ) -> tuple[Observation, ...]:
        """Read persisted observations, sorted by date."""
        if not isinstance(series_id, str) or not series_id:
            raise ValueError("series_id must be nonempty")
        if date_range is not None and not isinstance(date_range, DateRange):
            raise TypeError("date_range must be DateRange")

        sql = "SELECT date, value FROM observations WHERE series_id = ?"
        params: tuple = (series_id,)
        if date_range is not None:
            sql += " AND date BETWEEN ? AND ?"
            params += (date_range.start.isoformat(), date_range.end.isoformat())
        sql += " ORDER BY date ASC"

        with closing(self._connect()) as db:
            rows = db.execute(sql, params).fetchall()
        return tuple(
            Observation(series_id, date.fromisoformat(day), value)
            for day, value in rows
        )

    def read_coverage(self, series_id: str) -> tuple[DateRange, ...]:
        """Read verified coverage; the planner merges adjacent intervals."""
        if not isinstance(series_id, str) or not series_id:
            raise ValueError("series_id must be nonempty")
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT start_date, end_date FROM coverage
                WHERE series_id = ? ORDER BY start_date, end_date
                """,
                (series_id,),
            ).fetchall()
        return tuple(
            DateRange(date.fromisoformat(start), date.fromisoformat(end))
            for start, end in rows
        )

    @staticmethod
    def _validate_commit(
        series_id: str,
        interval: DateRange,
        observations: tuple[Observation, ...],
        missing_dates: tuple[date, ...],
        checked_at: datetime,
    ) -> None:
        """Reject malformed or inconsistent batches before writing."""
        if not isinstance(series_id, str) or not series_id:
            raise ValueError("series_id must be nonempty")
        if not isinstance(interval, DateRange):
            raise TypeError("interval must be DateRange")
        if (
            not isinstance(checked_at, datetime)
            or checked_at.tzinfo is None
            or checked_at.utcoffset() is None
        ):
            raise ValueError("checked_at must be timezone-aware")

        previous = None
        # Most GDP batches have no missing dates; avoid an unnecessary set.
        valid_dates = set() if missing_dates else None
        contains = interval.contains
        for item in observations:
            if not isinstance(item, Observation):
                raise TypeError("Expected Observation")
            if item.series_id != series_id:
                raise ValueError("Observation series mismatch")
            if not contains(item.observation_date):
                raise ValueError("Observation outside committed interval")
            if not isfinite(item.value):
                raise ValueError("Non-finite observation value")
            if previous is not None and item.observation_date <= previous:
                raise ValueError("Observations must be ordered and unique")
            previous = item.observation_date
            if valid_dates is not None:
                valid_dates.add(item.observation_date)

        previous = None
        for day in missing_dates:
            if type(day) is not date or not contains(day):
                raise ValueError("Invalid missing date")
            if previous is not None and day <= previous:
                raise ValueError("Missing dates must be ordered and unique")
            if day in valid_dates:
                raise ValueError("Date cannot be both valid and missing")
            previous = day

    def commit_range(
        self,
        series_id: str,
        interval: DateRange,
        observations: Iterable[Observation],
        *,
        missing_dates: Iterable[date] = (),
        checked_at: datetime | None = None,
        source: str = "FRED",
    ) -> CommitSummary:
        """Atomically upsert one validated interval and its verified coverage."""
        records = tuple(observations)
        missing = tuple(missing_dates)
        timestamp = checked_at if checked_at is not None else datetime.now(timezone.utc)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be nonempty")
        self._validate_commit(series_id, interval, records, missing, timestamp)

        checked = timestamp.astimezone(timezone.utc).isoformat()
        start, end = interval.start.isoformat(), interval.end.isoformat()
        inserted = updated = unchanged = 0

        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")

                if records:
                    # Small batches need no extra query. Custom observation
                    # triggers can mutate later rows, so retain sequential
                    # classification whenever such triggers are installed.
                    sequential = len(records) <= 8 or db.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'trigger' AND tbl_name = 'observations'
                        LIMIT 1
                        """
                    ).fetchone() is not None

                    if sequential:
                        for item in records:
                            day = item.observation_date.isoformat()
                            previous = db.execute(
                                """
                                SELECT value FROM observations
                                WHERE series_id = ? AND date = ?
                                """,
                                (series_id, day),
                            ).fetchone()
                            if previous is None:
                                inserted += 1
                            elif previous[0] != item.value:
                                updated += 1
                            else:
                                unchanged += 1
                            db.execute(
                                _UPSERT_OBSERVATION,
                                (series_id, day, item.value),
                            )
                    else:
                        # One indexed read replaces one SELECT per observation;
                        # the original UPSERT is applied in the original order.
                        first_day = records[0].observation_date.isoformat()
                        last_day = records[-1].observation_date.isoformat()
                        existing = dict(db.execute(
                            """
                            SELECT date, value FROM observations
                            WHERE series_id = ? AND date BETWEEN ? AND ?
                            """,
                            (series_id, first_day, last_day),
                        ))
                        absent = object()
                        lookup = existing.get

                        def rows():
                            nonlocal inserted, updated, unchanged
                            for item in records:
                                day = item.observation_date.isoformat()
                                old_value = lookup(day, absent)
                                if old_value is absent:
                                    inserted += 1
                                elif old_value != item.value:
                                    updated += 1
                                else:
                                    unchanged += 1
                                yield series_id, day, item.value

                        db.executemany(
                            _UPSERT_OBSERVATION,
                            rows(),
                        )

                overlapping = db.execute(
                    """
                    SELECT start_date, end_date, checked_at, source FROM coverage
                    WHERE series_id = ? AND start_date <= ? AND end_date >= ?
                    """,
                    (series_id, end, start),
                ).fetchall()
                db.execute(
                    """
                    DELETE FROM coverage
                    WHERE series_id = ? AND start_date <= ? AND end_date >= ?
                    """,
                    (series_id, end, start),
                )

                if overlapping:
                    left_end = right_start = None

                    def preserved_coverage():
                        nonlocal left_end, right_start
                        for old_start, old_end, old_checked, old_source in overlapping:
                            old_start_date = date.fromisoformat(old_start)
                            old_end_date = date.fromisoformat(old_end)
                            if old_start_date < interval.start:
                                if left_end is None:
                                    left_end = date.fromordinal(
                                        interval.start.toordinal() - 1
                                    ).isoformat()
                                yield series_id, old_start, left_end, old_checked, old_source
                            if old_end_date > interval.end:
                                if right_start is None:
                                    right_start = date.fromordinal(
                                        interval.end.toordinal() + 1
                                    ).isoformat()
                                yield series_id, right_start, old_end, old_checked, old_source

                    db.executemany(
                        "INSERT INTO coverage VALUES (?, ?, ?, ?, ?)",
                        preserved_coverage(),
                    )

                db.execute(
                    """
                    INSERT INTO coverage
                    (series_id, start_date, end_date, checked_at, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (series_id, start, end, checked, source),
                )

        return CommitSummary(
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            coverage=interval,
            checked_at=timestamp.astimezone(timezone.utc),
        )
