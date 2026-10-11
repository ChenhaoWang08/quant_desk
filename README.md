# QuantDesk UI v0.2 — Local Desktop Shell

**Scope:** Offline PySide6 desktop UI, indicator metadata catalog, custom project CRUD,
persistent chart settings, empty PyQtGraph component and SQLite readiness. **No APIs,
no imports, no fabricated data.**

## Windows / VS Code

Open the existing `D:\quant_desk` folder in VS Code. Do not overwrite your `.venv` or
`data/macro.db` files. Copy the supplied source tree over your existing source tree,
after backing up any modified `.py` files.

From a PowerShell terminal in that folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py
```

The test suite includes five backend tests and an additional Qt widget smoke test.
The Qt smoke test skips if PySide6 is unavailable.

## Manual acceptance checks

1. Built-in sections **Macro Data**, **Markets**, **Analytics** appear on the left.
2. Selecting a section populates folders and known indicator **metadata** in the middle.
3. Searching `GDP` or `GDPC1` filters the local catalog.
4. Selecting `Real GDP` shows **No Local Data**, `0` observations and the official FRED series link.
5. `+ New` creates a custom project; `Rename` and `Delete` act only on selected custom projects.
6. In a custom project, `+ Add indicator` adds a catalog item; `Remove` removes it.
7. Choosing a custom-project indicator and editing **Period / Type / Grid** saves settings in
   `data/projects.json` and restores them when the indicator is reopened after restarting.
8. SQLite status reads `Ready`; provider status reads **Not configured**. No network requests occur.

## Boundaries

- `main.py`: application startup and dependency injection only.
- `backend/catalog.py`: static catalog metadata; **not** historical data.
- `backend/projects.py`: project creation/rename/deletion/membership/chart config persisted as JSON.
- `backend/storage.py`: SQLite schema and observation counts, with no network access.
- `backend/service.py`: sole backend API for UI.
- `frontend/sidebar.py`: first navigation rail.
- `frontend/explorer.py`: second navigation rail.
- `frontend/workspace.py`: chart shell and data information, no data rendering yet.
- `frontend/main_window.py`: UI navigation/controller; it never executes SQL.
- `frontend/theme.py`: stylesheet only.

`data/projects.json` is local user state, not part of GitHub source. The default chart
control values in system sections are session-only; custom-project chart settings persist.

## Data safety and GitHub

Keep `data/macro.db` if it exists. `CREATE TABLE IF NOT EXISTS` will not delete its rows.
Make backups of both `data/macro.db` and `data/projects.json`. The `.gitignore` excludes
`.venv` and local data. This is a single-process UI prototype: it does not implement
multi-process locking or crash-resistant portable-drive durability guarantees.