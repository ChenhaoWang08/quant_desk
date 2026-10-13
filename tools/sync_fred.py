
"""QuantDesk manual FRED GDP synchronization CLI.

Flow:
    CLI arguments -> validate request -> optional read-only dry run
    -> SQLite integrity check and backup -> initialize schema
    -> MacroService.sync_gdp -> display durable synchronization result.

No duplicated HTTP, normalization, or SQLite ingestion logic.
Only the FRED GDP series is supported.

Exit codes:
    0   Completed or cache hit
    1   Invalid arguments
    2   Provider/network failure
    3   Partial synchronization
    4   Database/persistence failure
    5   Safety or configuration failure
    130 Interrupted by user

Run from the project root:
    python -m tools.sync_fred --help
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from backend.ingestion.planner import (
    FetchPlanner,
    PlanningError,
    subtract_years,
)
from backend.models import DateRange, FetchMode, FetchRequest
from backend.providers.base import ProviderError
from backend.service import MacroService
from backend.sync_storage import SyncIntegrityError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "macro.db"

EXIT_SUCCESS = 0
EXIT_ARGUMENT = 1
EXIT_PROVIDER = 2
EXIT_PARTIAL = 3
EXIT_STORAGE = 4
EXIT_SAFETY = 5
EXIT_INTERRUPTED = 130


class CLIError(Exception):
    """Safe user-facing CLI failure."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def parse_date(value: str) -> date:
    """Parse a strictly formatted ISO calendar date."""
    if (
        len(value) != 10
        or value[4:5] != "-"
        or value[7:8] != "-"
        or not (
            value[:4] + value[5:7] + value[8:]
        ).isascii()
        or not (
            value[:4] + value[5:7] + value[8:]
        ).isdigit()
    ):
        raise argparse.ArgumentTypeError(
            "Dates must use YYYY-MM-DD"
        )

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Invalid calendar date"
        ) from exc


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="QuantDesk manual FRED GDP synchronization."
    )

    parser.add_argument(
        "--mode",
        choices=("range", "full", "refresh"),
        default="range",
        help="GDP fetching strategy (default: range)",
    )
    parser.add_argument(
        "--start",
        type=parse_date,
        help="Inclusive observation start: YYYY-MM-DD",
    )
    parser.add_argument(
        "--end",
        type=parse_date,
        help="Inclusive observation end: YYYY-MM-DD",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached coverage and refetch the target",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite database path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; no writes, backup or network",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )

    return parser


def build_request(
    args: argparse.Namespace,
    *,
    as_of_date: date,
) -> FetchRequest:
    """Build a GDP-only request from validated CLI arguments."""
    if args.mode == "full" and (
        args.start is not None or args.end is not None
    ):
        raise CLIError(
            "FULL mode does not accept --start or --end.",
            EXIT_ARGUMENT,
        )

    if args.start is not None and args.end is not None:
        start, end = args.start, args.end
    elif args.start is None and args.end is None:
        end = as_of_date
        start = subtract_years(end, 5)
    else:
        raise CLIError(
            "Specify both --start and --end, or neither.",
            EXIT_ARGUMENT,
        )

    try:
        interval = DateRange(start, end)
    except ValueError as exc:
        raise CLIError(
            "Start date cannot be after end date.",
            EXIT_ARGUMENT,
        ) from exc

    return FetchRequest(
        series_id="GDP",
        date_range=interval,
        mode=FetchMode(args.mode),
        force=args.force,
    )


def check_database(path: Path) -> bool:
    """Read-only integrity check.

    Returns False if the database does not exist.
    Never creates an SQLite file.
    """
    if not path.exists():
        return False

    if not path.is_file():
        raise CLIError(
            "Database path is not a regular file.",
            EXIT_SAFETY,
        )

    uri = path.resolve().as_uri() + "?mode=ro"

    try:
        with closing(sqlite3.connect(
            uri, uri=True, timeout=10
        )) as db:
            rows = db.execute(
                "PRAGMA integrity_check"
            ).fetchall()
    except sqlite3.Error as exc:
        raise CLIError(
            "SQLite integrity inspection failed.",
            EXIT_STORAGE,
        ) from exc

    if rows != [("ok",)]:
        raise CLIError(
            "SQLite integrity check failed; synchronization stopped.",
            EXIT_SAFETY,
        )

    return True


def read_coverage_ro(
    path: Path,
    series_id: str,
) -> tuple[DateRange, ...]:
    """Read only committed coverage from an existing database."""
    if not check_database(path):
        return ()

    uri = path.resolve().as_uri() + "?mode=ro"

    try:
        with closing(sqlite3.connect(
            uri, uri=True, timeout=10
        )) as db:
            table = db.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='coverage'
            """).fetchone()

            if table is None:
                return ()

            rows = db.execute("""
                SELECT start_date, end_date
                FROM coverage
                WHERE series_id = ?
                ORDER BY start_date, end_date
            """, (series_id,)).fetchall()
    except sqlite3.Error as exc:
        raise CLIError(
            "Unable to read SQLite coverage.",
            EXIT_STORAGE,
        ) from exc

    return tuple(
        DateRange(
            date.fromisoformat(start),
            date.fromisoformat(end),
        )
        for start, end in rows
    )


def create_backup(path: Path) -> Path | None:
    """Create and verify an independent SQLite backup file.

    This is a logical SQLite backup, not a raw file copy.
    Existing databases only. Backups never overwrite.
    """
    if not check_database(path):
        return None

    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    backup_path = backup_dir / (
        f"{path.stem}_{timestamp}.db"
    )

    source_uri = path.resolve().as_uri() + "?mode=ro"

    try:
        with closing(sqlite3.connect(
            source_uri, uri=True, timeout=10
        )) as source:
            with closing(sqlite3.connect(
                backup_path, timeout=10
            )) as destination:
                source.backup(destination)

        with closing(sqlite3.connect(
            backup_path, timeout=10
        )) as verification:
            if verification.execute(
                "PRAGMA integrity_check"
            ).fetchall() != [("ok",)]:
                raise CLIError(
                    "Backup integrity verification failed.",
                    EXIT_SAFETY,
                )

    except (sqlite3.Error, OSError) as exc:
        raise CLIError(
            "SQLite backup failed; synchronization stopped.",
            EXIT_SAFETY,
        ) from exc

    return backup_path


def dry_run(
    request: FetchRequest,
    *,
    db_path: Path,
    as_of_date: date,
) -> dict:
    """Plan using local coverage without HTTP or database writes."""
    coverage = read_coverage_ro(
        db_path, request.series_id
    )

    if request.mode is FetchMode.FULL:
        return {
            "series_id": "GDP",
            "mode": "full",
            "status": "INDETERMINATE",
            "database": str(db_path),
            "earliest_available_date": None,
            "cached_ranges": [
                item.to_dict() for item in coverage
            ],
            "planned_ranges": None,
            "message": (
                "Exact FULL plan requires the provider's "
                "earliest date; dry-run does not access FRED."
            ),
        }

    try:
        plan = FetchPlanner().plan(
            request,
            coverage,
            as_of_date=as_of_date,
        )
    except PlanningError as exc:
        raise CLIError(
            str(exc), EXIT_ARGUMENT
        ) from exc

    return {
        "series_id": "GDP",
        "mode": request.mode.value,
        "status": (
            "FETCH_REQUIRED"
            if plan.requires_network
            else "CACHE_HIT"
        ),
        "database": str(db_path),
        "requested_range": request.date_range.to_dict(),
        "fetch_ranges": [
            item.to_dict() for item in plan.fetch_ranges
        ],
        "cached_ranges": [
            item.to_dict() for item in plan.cached_ranges
        ],
        "planned_ranges": plan.estimated_requests,
        "message": plan.reason,
    }


def show_result(data: dict, as_json: bool) -> None:
    """Render one result without printing credentials."""
    if as_json:
        print(json.dumps(
            data, ensure_ascii=False, indent=2
        ))
        return

    print("\nQuantDesk GDP Sync")
    print("------------------------------")

    for key, value in data.items():
        print(f"{key}: {value}")


def execute(
    argv: Sequence[str] | None = None,
    *,
    as_of_date: date | None = None,
) -> int:
    """Parse options, optionally synchronize, and return an exit code."""
    parser = make_parser()

    # argparse owns --help and standard usage output.
    args = parser.parse_args(argv)

    today = (
        as_of_date
        if as_of_date is not None
        else datetime.now(timezone.utc).date()
    )

    db_path = args.db.resolve()
    request = build_request(args, as_of_date=today)

    if args.dry_run:
        output = dry_run(
            request,
            db_path=db_path,
            as_of_date=today,
        )
        show_result(output, args.json)
        return EXIT_SUCCESS

    # A schema migration or real sync must not start
    # unless the existing database passes inspection
    # and the pre-sync backup has been verified.
    existed = check_database(db_path)
    backup = create_backup(db_path) if existed else None

    # No source data is downloaded during initialization.
    service = MacroService(db_path)
    service.initialize()
    service.initialize_sync_storage()

    result = service.sync_gdp(
        request, as_of_date=today
    )

    output = {
        "request_id": result.request_id,
        "series_id": result.series_id,
        "mode": request.mode.value,
        "status": result.status,
        "database": str(db_path),
        "backup": str(backup) if backup else None,
        "planned_ranges": result.planned_ranges,
        "committed_ranges": result.committed_ranges,
        "inserted": result.inserted,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "invalidated": result.invalidated,
        "reinstated": result.reinstated,
        "unconfirmed": result.unconfirmed,
        "error_code": result.error_code,
    }

    show_result(output, args.json)

    if result.status in ("COMPLETED", "CACHE_HIT"):
        return EXIT_SUCCESS
    if result.status == "PARTIAL":
        return EXIT_PARTIAL
    return EXIT_PROVIDER


def main(
    argv: Sequence[str] | None = None,
) -> int:
    """CLI boundary; never print unredacted request exceptions."""
    try:
        return execute(argv)
    except CLIError as exc:
        print(f"QuantDesk: {exc}", file=sys.stderr)
        return exc.exit_code
    except ProviderError as exc:
        print(
            f"QuantDesk provider failure: {exc.code}",
            file=sys.stderr,
        )
        return EXIT_PROVIDER
    except (SyncIntegrityError, sqlite3.Error, OSError):
        print(
            "QuantDesk storage or synchronization failure.",
            file=sys.stderr,
        )
        return EXIT_STORAGE
    except KeyboardInterrupt:
        print(
            "QuantDesk interrupted by user.",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    except (TypeError, ValueError):
        print(
            "QuantDesk rejected invalid input.",
            file=sys.stderr,
        )
        return EXIT_ARGUMENT


if __name__ == "__main__":
    sys.exit(main())
