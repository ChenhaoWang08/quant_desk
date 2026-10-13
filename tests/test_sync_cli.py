
"""QuantDesk M2 STEP 4 CLI tests.

Tests argument parsing, date handling, dry-run behavior,
SQLite backup, safe output, and synchronization exit codes.

All databases are temporary. No real FRED calls.
"""

from __future__ import annotations
from contextlib import closing
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.models import (
    DateRange,
    FetchMode,
)
from backend.storage import SQLiteStore
from tools import sync_fred


TODAY = date(2026, 10, 13)


class SyncCLITests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db = Path(self.temp.name) / "data" / "macro.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)

    def parse(self, *args):
        return sync_fred.make_parser().parse_args(args)

    def test_default_range_is_five_years(self):
        args = self.parse()
        request = sync_fred.build_request(
            args, as_of_date=TODAY
        )

        self.assertEqual(request.mode, FetchMode.RANGE)
        self.assertEqual(
            request.date_range,
            DateRange(
                date(2021, 10, 13),
                date(2026, 10, 13),
            ),
        )

    def test_custom_date_range(self):
        args = self.parse(
            "--start", "2024-01-01",
            "--end", "2025-12-31",
        )

        request = sync_fred.build_request(
            args, as_of_date=TODAY
        )

        self.assertEqual(
            request.date_range,
            DateRange(
                date(2024, 1, 1),
                date(2025, 12, 31),
            ),
        )

    def test_invalid_date_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(
                "--start", "2024-13-01",
                "--end", "2025-12-31",
            )

    def test_partial_date_range_rejected(self):
        args = self.parse(
            "--start", "2024-01-01"
        )

        with self.assertRaises(sync_fred.CLIError):
            sync_fred.build_request(
                args, as_of_date=TODAY
            )

    def test_full_requires_explicit_mode(self):
        args = self.parse()
        request = sync_fred.build_request(
            args, as_of_date=TODAY
        )
        self.assertEqual(request.mode, FetchMode.RANGE)

        args = self.parse("--mode", "full")
        request = sync_fred.build_request(
            args, as_of_date=TODAY
        )
        self.assertEqual(request.mode, FetchMode.FULL)

    def test_dry_run_does_not_create_database(self):
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = sync_fred.execute(
                [
                    "--db", str(self.db),
                    "--dry-run",
                    "--json",
                ],
                as_of_date=TODAY,
            )

        self.assertEqual(code, 0)
        self.assertFalse(self.db.exists())

        result = json.loads(output.getvalue())
        self.assertEqual(
            result["status"], "FETCH_REQUIRED"
        )

    def test_dry_run_uses_existing_coverage(self):
        store = SQLiteStore(self.db)
        store.initialize()

        interval = DateRange(
            date(2024, 1, 1),
            date(2025, 12, 31),
        )

        store.commit_range("GDP", interval, ())
        before = self.db.read_bytes()

        output = sync_fred.dry_run(
            sync_fred.build_request(
                self.parse(
                    "--start", "2024-01-01",
                    "--end", "2025-12-31",
                ),
                as_of_date=TODAY,
            ),
            db_path=self.db,
            as_of_date=TODAY,
        )

        self.assertEqual(
            output["status"], "CACHE_HIT"
        )
        self.assertEqual(
            output["planned_ranges"], 0
        )
        self.assertEqual(
            self.db.read_bytes(), before
        )

    def test_full_dry_run_is_indeterminate(self):
        request = sync_fred.build_request(
            self.parse("--mode", "full"),
            as_of_date=TODAY,
        )

        result = sync_fred.dry_run(
            request,
            db_path=self.db,
            as_of_date=TODAY,
        )

        self.assertEqual(
            result["status"], "INDETERMINATE"
        )
        self.assertIsNone(
            result["planned_ranges"]
        )

    def test_backup_is_readable(self):
        store = SQLiteStore(self.db)
        store.initialize()

        backup = sync_fred.create_backup(self.db)

        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        self.assertNotEqual(backup, self.db)

        with closing(sqlite3.connect(backup)) as connection:
            result = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]

        self.assertEqual(result, "ok")

    def test_no_backup_for_missing_database(self):
        self.assertIsNone(
            sync_fred.create_backup(self.db)
        )
        self.assertFalse(self.db.exists())

    def test_success_exit_code(self):
        fake_result = SimpleNamespace(
            request_id="test-run",
            series_id="GDP",
            status="COMPLETED",
            planned_ranges=1,
            committed_ranges=1,
            inserted=2,
            updated=0,
            unchanged=0,
            invalidated=0,
            reinstated=0,
            unconfirmed=0,
            error_code=None,
        )

        output = io.StringIO()

        with patch.object(
            sync_fred, "MacroService"
        ) as service_class:
            service = service_class.return_value
            service.sync_gdp.return_value = fake_result

            with contextlib.redirect_stdout(output):
                code = sync_fred.execute(
                    [
                        "--db", str(self.db),
                        "--json",
                    ],
                    as_of_date=TODAY,
                )

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.getvalue())["status"],
            "COMPLETED",
        )

    def test_partial_exit_code(self):
        fake_result = SimpleNamespace(
            request_id="test-partial",
            series_id="GDP",
            status="PARTIAL",
            planned_ranges=2,
            committed_ranges=1,
            inserted=1,
            updated=0,
            unchanged=0,
            invalidated=0,
            reinstated=0,
            unconfirmed=0,
            error_code="HTTP_503",
        )

        with patch.object(
            sync_fred, "MacroService"
        ) as service_class:
            service_class.return_value.sync_gdp.return_value = fake_result

            with contextlib.redirect_stdout(io.StringIO()):
                code = sync_fred.execute(
                    ["--db", str(self.db)],
                    as_of_date=TODAY,
                )

        self.assertEqual(code, 3)

    def test_provider_error_is_sanitized(self):
        from backend.providers.base import ProviderError

        secret = "FAKE_SECRET_DO_NOT_PRINT"

        with patch.object(
            sync_fred, "execute",
            side_effect=ProviderError(
                secret,
                provider="FRED",
                code="HTTP_400",
            ),
        ):
            output = io.StringIO()

            with contextlib.redirect_stderr(output):
                code = sync_fred.main([])

        self.assertEqual(code, 2)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("HTTP_400", output.getvalue())


if __name__ == "__main__":
    unittest.main()
