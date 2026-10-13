
"""QuantDesk M2: SQLite-backed synchronization integrity.

Flow: register run/range -> verify complete M1 snapshot -> reconcile GDP values
and explicit missing dates -> write ordered audit and coverage -> atomic commit.
Absent old values remain stored but are excluded from newly confirmed coverage.
No HTTP, API keys, GUI or normalization. A fetch is not a database commit.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.models import DateRange, Observation
from backend.storage import SQLiteStore


class SyncIntegrityError(ValueError):
    """Invalid synchronization operation or state transition."""


@dataclass(frozen=True, slots=True)
class RangeCommitSummary:
    inserted: int
    updated: int
    unchanged: int
    invalidated: int
    reinstated: int
    unconfirmed: int
    committed_at: datetime


def _utc(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError("Timestamp must have a UTC offset")
    return value.astimezone(timezone.utc).isoformat()


def _coverage_segments(
    interval: DateRange,
    excluded: set[date],
) -> tuple[DateRange, ...]:
    """Split a fetched interval around unconfirmed dates."""
    cursor = interval.start.toordinal()
    end = interval.end.toordinal()
    segments = []

    for excluded_date in sorted(excluded):
        day = excluded_date.toordinal()
        if cursor < day:
            segments.append(
                DateRange(
                    date.fromordinal(cursor),
                    date.fromordinal(day - 1),
                )
            )
        cursor = day + 1

    if cursor <= end:
        segments.append(
            DateRange(date.fromordinal(cursor), interval.end)
        )

    return tuple(segments)


class SyncStorage:
    """Synchronization extension for an existing SQLiteStore.

    Do not share an active writer across independent processes.
    Each method opens and explicitly closes its connection.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.base = SQLiteStore(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=10000")
        except Exception:
            db.close()
            raise
        return db

    def initialize(self) -> None:
        """Extend the existing database without dropping its data."""
        self.base.initialize()

        with closing(self._connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 2):
                raise SyncIntegrityError(
                    f"Unsupported database schema version: {version}"
                )

            with db:
                db.execute("BEGIN IMMEDIATE")

                db.execute("""
                    CREATE TABLE IF NOT EXISTS sync_ranges (
                        range_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        range_index INTEGER NOT NULL,
                        series_id TEXT NOT NULL,
                        start_date TEXT NOT NULL,
                        end_date TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reconciliation_status TEXT,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        committed_at TEXT,
                        fetched_count INTEGER NOT NULL DEFAULT 0,
                        missing_count INTEGER NOT NULL DEFAULT 0,
                        inserted_count INTEGER NOT NULL DEFAULT 0,
                        updated_count INTEGER NOT NULL DEFAULT 0,
                        unchanged_count INTEGER NOT NULL DEFAULT 0,
                        invalidated_count INTEGER NOT NULL DEFAULT 0,
                        reinstated_count INTEGER NOT NULL DEFAULT 0,
                        unconfirmed_count INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT,
                        UNIQUE(request_id, range_index),
                        FOREIGN KEY(request_id)
                            REFERENCES sync_runs(request_id),
                        CHECK(start_date <= end_date)
                    )
                """)

                db.execute("""
                    CREATE TABLE IF NOT EXISTS observation_audit (
                        audit_id INTEGER PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        range_id TEXT NOT NULL,
                        series_id TEXT NOT NULL,
                        observation_date TEXT NOT NULL,
                        action TEXT NOT NULL,
                        old_value REAL,
                        new_value REAL,
                        reason TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        FOREIGN KEY(range_id)
                            REFERENCES sync_ranges(range_id)
                    )
                """)

                db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sync_ranges_run
                    ON sync_ranges(request_id, range_index)
                """)

                db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_series_date
                    ON observation_audit(series_id, observation_date)
                """)

                db.execute("PRAGMA user_version=2")

    def begin_run(
        self,
        request_id: str,
        *,
        series_id: str = "GDP",
        mode: str = "range",
    ) -> None:
        """Register a new run; reject concurrent active runs."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be nonempty")
        if series_id != "GDP":
            raise ValueError("Only GDP is supported")
        if mode not in ("range", "full", "refresh"):
            raise ValueError("Unsupported fetch mode")

        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")

                active = db.execute("""
                    SELECT request_id FROM sync_runs
                    WHERE status = 'RUNNING'
                    LIMIT 1
                """).fetchone()

                if active:
                    raise SyncIntegrityError(
                        "Another synchronization is RUNNING"
                    )

                db.execute("""
                    INSERT INTO sync_runs
                    (request_id, series_id, mode, started_at, status)
                    VALUES (?, ?, ?, ?, 'RUNNING')
                """, (request_id, series_id, mode, _utc()))

    def start_range(
        self,
        request_id: str,
        range_index: int,
        interval: DateRange,
    ) -> str:
        """Register a range before its network request starts."""
        if type(range_index) is not int or range_index < 0:
            raise ValueError("range_index must be non-negative")
        if not isinstance(interval, DateRange):
            raise TypeError("interval must be DateRange")

        range_id = f"{request_id}:{range_index}"

        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                run = db.execute("""
                    SELECT series_id, status FROM sync_runs
                    WHERE request_id = ?
                """, (request_id,)).fetchone()

                if run is None or run[1] != "RUNNING":
                    raise SyncIntegrityError("Run is not active")

                db.execute("""
                    INSERT INTO sync_ranges
                    (range_id, request_id, range_index, series_id,
                     start_date, end_date, status, started_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?)
                """, (
                    range_id, request_id, range_index, run[0],
                    interval.start.isoformat(),
                    interval.end.isoformat(), _utc(),
                ))

        return range_id

    @staticmethod
    def _validate_snapshot(
        interval: DateRange,
        records: tuple[Observation, ...],
        missing: tuple[date, ...],
    ) -> None:
        """Validate a complete M1 result before writing anything."""
        if not isinstance(interval, DateRange):
            raise TypeError("interval must be DateRange")

        previous = None

        # Sorted unique records need a lookup set only if missing dates exist.
        seen = set() if missing else None
        contains = interval.contains

        for item in records:
            if not isinstance(item, Observation):
                raise TypeError("Expected Observation")
            if item.series_id != "GDP":
                raise ValueError("Only GDP observations are accepted")
            if not contains(item.observation_date):
                raise ValueError("Observation outside range")
            if previous is not None and item.observation_date <= previous:
                raise ValueError("Observations not strictly ordered")
            if item.value <= 0:
                raise ValueError("GDP value must be positive")

            previous = item.observation_date

            if seen is not None:
                seen.add(item.observation_date)

        previous = None
        for day in missing:
            if type(day) is not date or not contains(day):
                raise ValueError("Invalid missing date")
            if previous is not None and day <= previous:
                raise ValueError("Missing dates not strictly ordered")
            if day in seen:
                raise ValueError("Date both valid and missing")
            previous = day

    def commit_range(
        self,
        range_id: str,
        observations: Iterable[Observation],
        *,
        missing_dates: Iterable[date] = (),
        source: str = "FRED",
    ) -> RangeCommitSummary:
        """Atomically reconcile an entire validated range.

        The caller guarantees complete M1 validation.
        This method independently checks basic batch consistency.
        """
        records = tuple(observations)
        missing = tuple(missing_dates)
        if source != "FRED":
            raise ValueError("Only FRED is supported")

        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")

                info = db.execute("""
                    SELECT r.request_id, r.series_id,
                           r.start_date, r.end_date, r.status,
                           s.status
                    FROM sync_ranges AS r
                    JOIN sync_runs AS s
                      ON r.request_id = s.request_id
                    WHERE r.range_id = ?
                """, (range_id,)).fetchone()

                if info is None:
                    raise SyncIntegrityError("Unknown range")

                request_id, series_id, start, end, status, run_status = info

                if status != "RUNNING" or run_status != "RUNNING":
                    raise SyncIntegrityError("Range is not active")

                interval = DateRange(
                    date.fromisoformat(start),
                    date.fromisoformat(end),
                )
                self._validate_snapshot(interval, records, missing)

                timestamp = _utc()

                existing = {
                    date.fromisoformat(day): value
                    for day, value in db.execute("""
                        SELECT date, value FROM observations
                        WHERE series_id = ?
                        AND date BETWEEN ? AND ?
                    """, (series_id, start, end))
                }

                # Key-only comparisons: avoid a redundant date->value dict.
                unconfirmed = (
                    existing.keys()
                    - {item.observation_date for item in records}
                    - set(missing)
                )

                inserted = updated = unchanged = 0
                invalidated = reinstated = 0

                def audit(day, action, old, new, reason):
                    db.execute("""
                        INSERT INTO observation_audit
                        (request_id, range_id, series_id,
                         observation_date, action, old_value,
                         new_value, reason, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        request_id, range_id, series_id,
                        day.isoformat(), action, old, new,
                        reason, timestamp,
                    ))

                for item in records:
                    day = item.observation_date
                    value = item.value

                    if day in existing:
                        old = existing[day]
                        if old == value:
                            unchanged += 1
                            continue

                        updated += 1
                        action = "UPDATE"
                    else:
                        old = None
                        previously_invalidated = db.execute("""
                            SELECT 1 FROM observation_audit
                            WHERE series_id = ?
                            AND observation_date = ?
                            AND action = 'INVALIDATE'
                            LIMIT 1
                        """, (series_id, day.isoformat())).fetchone()

                        if previously_invalidated:
                            reinstated += 1
                            action = "REINSTATE"
                        else:
                            inserted += 1
                            action = "INSERT"

                    audit(day, action, old, value, "FRED_SNAPSHOT")

                    db.execute("""
                        INSERT INTO observations (series_id, date, value)
                        VALUES (?, ?, ?)
                        ON CONFLICT(series_id, date)
                        DO UPDATE SET value = excluded.value
                    """, (series_id, day.isoformat(), value))

                for day in missing:
                    if day not in existing:
                        continue

                    old = existing[day]
                    audit(day, "INVALIDATE", old, None, "FRED_DOT")

                    db.execute("""
                        DELETE FROM observations
                        WHERE series_id = ? AND date = ?
                    """, (series_id, day.isoformat()))

                    invalidated += 1

                # Old coverage in this interval must be replaced.
                # Preserve its timestamps outside the replacement.
                old_ranges = db.execute("""
                    SELECT start_date, end_date, checked_at, source
                    FROM coverage
                    WHERE series_id = ?
                    AND start_date <= ? AND end_date >= ?
                """, (series_id, end, start)).fetchall()

                db.execute("""
                    DELETE FROM coverage
                    WHERE series_id = ?
                    AND start_date <= ? AND end_date >= ?
                """, (series_id, end, start))

                for a, b, checked, old_source in old_ranges:
                    left, right = date.fromisoformat(a), date.fromisoformat(b)

                    if left < interval.start:
                        db.execute("""
                            INSERT INTO coverage VALUES (?, ?, ?, ?, ?)
                        """, (
                            series_id, a,
                            date.fromordinal(
                                interval.start.toordinal() - 1
                            ).isoformat(),
                            checked, old_source,
                        ))

                    if right > interval.end:
                        db.execute("""
                            INSERT INTO coverage VALUES (?, ?, ?, ?, ?)
                        """, (
                            series_id,
                            date.fromordinal(
                                interval.end.toordinal() + 1
                            ).isoformat(),
                            b, checked, old_source,
                        ))

                # Do not claim absent old dates were reconciled.
                for segment in _coverage_segments(interval, unconfirmed):
                    db.execute("""
                        INSERT INTO coverage
                        (series_id, start_date, end_date, checked_at, source)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        series_id,
                        segment.start.isoformat(),
                        segment.end.isoformat(),
                        timestamp, source,
                    ))

                reconciliation = (
                    "UNCONFIRMED_ABSENCE"
                    if unconfirmed else "COMPLETE"
                )

                db.execute("""
                    UPDATE sync_ranges SET
                        status='COMMITTED',
                        reconciliation_status=?,
                        finished_at=?,
                        committed_at=?,
                        fetched_count=?,
                        missing_count=?,
                        inserted_count=?,
                        updated_count=?,
                        unchanged_count=?,
                        invalidated_count=?,
                        reinstated_count=?,
                        unconfirmed_count=?
                    WHERE range_id = ?
                """, (
                    reconciliation, timestamp, timestamp,
                    len(records), len(missing),
                    inserted, updated, unchanged,
                    invalidated, reinstated, len(unconfirmed),
                    range_id,
                ))

                db.execute("""
                    UPDATE sync_runs SET
                        fetched_count = fetched_count + ?,
                        inserted_count = inserted_count + ?,
                        updated_count = updated_count + ?
                    WHERE request_id = ?
                """, (
                    len(records), inserted + reinstated,
                    updated, request_id,
                ))

        return RangeCommitSummary(
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            invalidated=invalidated,
            reinstated=reinstated,
            unconfirmed=len(unconfirmed),
            committed_at=datetime.fromisoformat(timestamp),
        )

    def fail_range(
        self,
        range_id: str,
        error_code: str,
    ) -> None:
        """Mark a running range as failed, without changing GDP data."""
        if not isinstance(error_code, str) or not error_code:
            raise ValueError("error_code must be nonempty")

        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("""
                    UPDATE sync_ranges
                    SET status='FAILED', finished_at=?, error_code=?
                    WHERE range_id=? AND status='RUNNING'
                """, (_utc(), error_code, range_id))

                if cursor.rowcount != 1:
                    raise SyncIntegrityError("Range is not RUNNING")

    def finish_run(self, request_id: str) -> str:
        """Finish a run based on actual persisted range outcomes."""
        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""
                    SELECT status FROM sync_runs WHERE request_id=?
                """, (request_id,)).fetchone()

                if row is None or row[0] != "RUNNING":
                    raise SyncIntegrityError("Run is not RUNNING")

                statuses = db.execute("""
                    SELECT status, reconciliation_status
                    FROM sync_ranges WHERE request_id=?
                """, (request_id,)).fetchall()

                if any(s == "RUNNING" or s == "PENDING" for s, _ in statuses):
                    raise SyncIntegrityError("Some ranges are unfinished")

                committed = sum(s == "COMMITTED" for s, _ in statuses)
                clean = all(
                    s == "COMMITTED" and r == "COMPLETE"
                    for s, r in statuses
                )

                if not statuses:
                    final = "CACHE_HIT"
                elif clean:
                    final = "COMPLETED"
                elif committed:
                    final = "PARTIAL"
                else:
                    final = "FAILED"

                db.execute("""
                    UPDATE sync_runs
                    SET status=?, finished_at=?
                    WHERE request_id=?
                """, (final, _utc(), request_id))

        return final

    def read_run(self, request_id: str) -> dict | None:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""
                SELECT * FROM sync_runs WHERE request_id=?
            """, (request_id,)).fetchone()
            return dict(row) if row else None

    def read_ranges(self, request_id: str) -> list[dict]:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("""
                SELECT * FROM sync_ranges
                WHERE request_id=?
                ORDER BY range_index
            """, (request_id,))]

    def read_audit(self, series_id: str = "GDP") -> list[dict]:
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("""
                SELECT * FROM observation_audit
                WHERE series_id=?
                ORDER BY audit_id
            """, (series_id,))]

    def recover_interrupted(
        self,
        *,
        confirmed_exclusive_access: bool = False,
    ) -> int:
        """Mark orphaned runs/ranges INTERRUPTED.

        The caller must first establish that no previous
        synchronization worker is still active.
        """
        if confirmed_exclusive_access is not True:
            raise SyncIntegrityError(
                "Exclusive access must be confirmed before recovery"
            )

        with closing(self._connect()) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                now = _utc()

                db.execute("""
                    UPDATE sync_ranges
                    SET status='INTERRUPTED', finished_at=?
                    WHERE status IN ('RUNNING', 'PENDING')
                """, (now,))

                cursor = db.execute("""
                    UPDATE sync_runs
                    SET status='INTERRUPTED', finished_at=?
                    WHERE status='RUNNING'
                """, (now,))

                return cursor.rowcount
