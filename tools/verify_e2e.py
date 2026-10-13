"""Read-only verification of a QuantDesk GDP SQLite database.

Flow: open SQLite using mode=ro -> integrity/schema checks -> observations,
coverage, run and audit summaries -> optional independent reference comparison
and backup snapshot comparison -> report JSON or text.

Never initializes/migrates, changes, or restores a database. Never uses FRED
credentials. A reference must be obtained independently from FRED and saved
as JSON: [{"date": "2024-01-01", "value": "28793.191"}, ...].
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

TABLES = ("observations", "coverage", "sync_runs", "sync_ranges", "observation_audit")


def _connect(path: Path):
    if not path.is_file():
        raise ValueError("Database file does not exist")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def _snapshot(path: Path) -> dict:
    with closing(_connect(path)) as db:
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        existing = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing = sorted(set(TABLES) - existing)
        if integrity != ["ok"] or missing:
            return {"integrity": integrity, "missing_tables": missing,
                    "counts": {}, "observations": [], "coverage": [], "audit": [],
                    "runs": [], "ranges": []}
        observations = db.execute(
            "SELECT date, value FROM observations WHERE series_id='GDP' ORDER BY date"
        ).fetchall()
        coverage = db.execute(
            "SELECT start_date, end_date FROM coverage WHERE series_id='GDP' "
            "ORDER BY start_date, end_date"
        ).fetchall()
        audit = db.execute(
            "SELECT observation_date, action, old_value, new_value, reason "
            "FROM observation_audit WHERE series_id='GDP' ORDER BY audit_id"
        ).fetchall()
        runs = db.execute(
            "SELECT request_id, status FROM sync_runs WHERE series_id='GDP' "
            "ORDER BY started_at, request_id"
        ).fetchall()
        ranges = db.execute(
            "SELECT request_id, range_index, status, reconciliation_status "
            "FROM sync_ranges WHERE series_id='GDP' ORDER BY request_id, range_index"
        ).fetchall()
        return {"integrity": integrity, "missing_tables": missing,
                "counts": {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in TABLES},
                "observations": observations, "coverage": coverage,
                "audit": audit, "runs": runs, "ranges": ranges}


def _digest(rows) -> str:
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reference(path: Path) -> dict[str, Decimal]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Reference must be a JSON list")
    result = {}
    for entry in data:
        if not isinstance(entry, dict) or set(entry) != {"date", "value"}:
            raise ValueError("Each reference row must contain date and value")
        day = date.fromisoformat(entry["date"]).isoformat()
        try:
            val = Decimal(str(entry["value"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Invalid reference value") from exc
        if not val.is_finite() or day in result:
            raise ValueError("Non-finite or duplicate reference observation")
        result[day] = val
    return result


def verify(db_path: Path, *, backup: Path | None = None,
           reference: Path | None = None) -> dict:
    """Return a non-mutating report; a backup is historical, not necessarily current."""
    current = _snapshot(db_path)
    healthy = current["integrity"] == ["ok"] and not current["missing_tables"]
    report = {"database": str(db_path), "integrity": current["integrity"],
              "missing_tables": current["missing_tables"],
              "counts": current["counts"],
              "observation_sha256": _digest(current["observations"]),
              "coverage_sha256": _digest(current["coverage"]),
              "audit_sha256": _digest(current["audit"]),
              "run_statuses": [status for _, status in current["runs"]],
              "range_statuses": [status for _, _, status, _ in current["ranges"]],
              "reference": "NOT_CHECKED", "backup": "NOT_CHECKED",
              "status": "PASS" if healthy else "FAIL"}
    if reference is not None:
        expected = _reference(reference)
        actual = {d: Decimal(str(v)) for d, v in current["observations"]}
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        mismatched = sorted(d for d in expected.keys() & actual.keys()
                            if expected[d] != actual[d])
        report["reference"] = {"status": "PASS" if not (missing or extra or mismatched)
                                else "FAIL", "missing_dates": missing,
                                "extra_dates": extra, "different_value_dates": mismatched}
        if report["reference"]["status"] != "PASS":
            report["status"] = "FAIL"
    if backup is not None:
        saved = _snapshot(backup)
        backup_healthy = saved["integrity"] == ["ok"] and not saved["missing_tables"]
        report["backup"] = {"path": str(backup),
                            "status": "PASS" if backup_healthy else "FAIL",
                            "observation_count": len(saved["observations"]),
                            "observation_sha256": _digest(saved["observations"]),
                            "matches_current_observations":
                                saved["observations"] == current["observations"],
                            "matches_current_coverage": saved["coverage"] == current["coverage"],
                            "matches_current_audit": saved["audit"] == current["audit"]}
        if not backup_healthy:
            report["status"] = "FAIL"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify QuantDesk GDP SQLite (read only)")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--reference", type=Path, help="Independently saved FRED GDP JSON")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(args.db, backup=args.backup, reference=args.reference)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(f"Verification failed: {type(exc).__name__}", file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("QuantDesk E2E read-only verification")
        for name, value in result.items():
            print(f"{name}: {value}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
