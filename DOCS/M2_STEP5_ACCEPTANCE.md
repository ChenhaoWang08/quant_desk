# QuantDesk M2 STEP 5 — End-to-End Acceptance

Status: **IN PROGRESS** (do not mark complete based solely on unit tests).

## Scope

Verify real FRED GDP observations already synchronized into a disposable SQLite
file, their committed coverage and audit records, deterministic revision and
fault-injection behavior, restart persistence, and backup restorability.

All new automated tests use temporary SQLite files. Never target the production
`data/macro.db` with fault injection. Close the GUI before actual CLI sync.

## Files

- `tests/test_e2e_integrity.py` — 8 deterministic integrity tests.
- `tests/test_e2e_failures.py` — 6 deterministic failure tests, including a
  documented known limitation for unhandled exceptions.
- `tools/verify_e2e.py` — read-only SQLite verification and optional reference
  and backup comparison.

## Windows commands (from project root)

```powershell
cd D:\ZYXE\quant_desk
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_e2e_integrity.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_e2e_failures.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m tools.verify_e2e --db .\data\e2e_test.db --json
```

Expected total after adding the 14 tests: 52 tests (38 existing + 14 new),
assuming the previously confirmed test suite is unchanged.

To validate an independently exported FRED snapshot, save a separately obtained
JSON array as `data/fred_gdp_reference.json` in the following format:

```json
[
  {"date": "2024-01-01", "value": "28793.191"},
  {"date": "2024-04-01", "value": "29232.964"}
]
```

This is a format illustration, **not** a complete current FRED reference. A
reference comparison requires every observation in the inspected GDP database.
Use the FRED source and retrieval date to establish the vintage; do not assume
old quoted GDP values are the latest revised values. Do not paste API keys.

```powershell
.\.venv\Scripts\python.exe -m tools.verify_e2e --db .\data\e2e_test.db --reference .\data\fred_gdp_reference.json --json
```

To inspect the existing backup independently (no restoration/overwrites):

```powershell
.\.venv\Scripts\python.exe -m tools.verify_e2e --db .\data\e2e_test.db --backup .\data\backups\e2e_test_20261013T061400867940Z.db --json
```

`matches_current_*: false` does not automatically mean a backup is corrupt:
a backup is a historical snapshot, and newer writes can change the live DB.
The verifier checks the backup's own integrity and schema. Automated tests
perform an actual restore to a separate disposable SQLite database.

## Acceptance evidence checklist

| ID | Acceptance item | Status before local execution |
|---|---|---|
| E2E-01 | First real FRED sync (8 observations) | PASS (user log) |
| E2E-02 | Independent FRED-vintage reference comparison | NOT RUN |
| E2E-03 | Restart persistence | PARTIAL (cache persisted; verify rows) |
| E2E-04 | CACHE_HIT | PASS (user log) |
| E2E-05 | Coverage correctness | NOT RUN |
| E2E-06 | Unchanged refresh | NOT RUN |
| E2E-07 | Revision update/audit | NOT RUN |
| E2E-08 | Explicit missing invalidation | NOT RUN |
| E2E-09 | Unconfirmed absence | NOT RUN |
| E2E-10 | Partial synchronization | NOT RUN |
| E2E-11 | API failure containment | NOT RUN |
| E2E-12 | SQL rollback | NOT RUN |
| E2E-13 | Duplicate/cache behavior | NOT RUN |
| E2E-14 | Audit integrity | NOT RUN |
| E2E-15 | Backup integrity | NOT RUN |
| E2E-16 | Independent backup restoration | NOT RUN |
| E2E-17 | Interrupted recovery and exclusivity | PARTIAL (manual exclusivity only) |
| E2E-18 | CLI exit codes | PARTIAL (mock tests passed) |
| E2E-19 | API key secrecy | PARTIAL (mocked output check passed) |
| E2E-20 | Regression tests | NOT RUN (for STEP 5 additions) |

## Known risks and release gating

1. `GDPSyncCoordinator` may leave a persisted RUNNING run after a non-Provider
   exception or failure while building its provider. The new failure test
   asserts and documents this, rather than claiming recovery is implemented.
2. `recover_interrupted(confirmed_exclusive_access=True)` trusts the caller;
   it does not implement OS-level exclusive locking.
3. CLI currently creates a backup even on a cache hit; this is not an error.
4. The production CLI computes its default `as_of_date` in UTC, not the user's
   local time. Around a UTC date boundary, verify the requested end date.
5. The read-only verifier does not prove network provenance. Independent
   source capture and same-vintage comparisons remain necessary.
6. Do not declare M2 STEP 5 fully accepted until real-data comparison, backup
   recovery, the required failure cases, and full regression evidence pass.
