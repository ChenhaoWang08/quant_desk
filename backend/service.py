
"""QuantDesk backend service for catalog, projects and GDP synchronization.

Flow:
    Frontend or CLI -> MacroService
        -> catalog / ProjectStore for metadata and projects
        -> SQLiteStore for local historical queries
        -> GDPSyncCoordinator for persistent GDP synchronization.

The service does not execute SQL or HTTP directly.
Existing v0.2 project and chart APIs are preserved.
Sync schema initialization is explicit and never fetches data.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from backend.catalog import (
    CATALOG,
    BY_ID,
    SYSTEM_SECTIONS,
    Indicator,
    indicators_for_section,
    indicators_for_ids,
)
from backend.models import DateRange, FetchRequest, Observation
from backend.projects import ProjectStore
from backend.storage import SQLiteStore
from backend.sync_storage import SyncStorage
from backend.sync_coordinator import (
    GDPSyncCoordinator,
    PersistentSyncResult,
)
from backend.providers.base import DataProvider


class MacroService:
    __slots__ = ("_storage", "_projects", "_sync_storage")

    def __init__(self, db_path: Path):
        path = Path(db_path)
        self._storage = SQLiteStore(path)
        self._projects = ProjectStore(
            path.parent / "projects.json"
        )
        self._sync_storage = SyncStorage(path)

    def initialize(self) -> None:
        """Initialize the existing v0.2 application data."""
        self._storage.initialize()
        self._projects.initialize()

    def initialize_sync_storage(self) -> None:
        """Explicitly initialize the M2 synchronization schema.

        Does not call FRED or download observations.
        Back up a real database before first migration.
        """
        self._sync_storage.initialize()

    def observation_count(
        self,
        series_id: str | None = None,
    ) -> int:
        return self._storage.count_observations(series_id)

    def sections(self) -> dict[str, str]:
        return SYSTEM_SECTIONS.copy()

    def indicators(
        self,
        kind: str,
        key: str,
    ) -> list[Indicator]:
        if kind == "system":
            return indicators_for_section(key)

        if kind == "custom":
            return indicators_for_ids(
                self.get_project(key)["series_ids"]
            )

        raise ValueError(f"Unknown section kind: {kind}")

    def all_indicators(self) -> list[Indicator]:
        return list(CATALOG)

    def find_indicator(self, series_id: str) -> Indicator:
        return BY_ID[series_id]

    def list_projects(self) -> list[dict]:
        return self._projects.list_projects()

    def get_project(self, project_id: str) -> dict:
        for project in self.list_projects():
            if project["id"] == project_id:
                return project

        raise KeyError(
            f"Project not found: {project_id}"
        )

    def create_project(self, name: str) -> dict:
        return self._projects.create(name)

    def rename_project(
        self,
        project_id: str,
        name: str,
    ) -> None:
        self._projects.rename(project_id, name)

    def delete_project(self, project_id: str) -> None:
        self._projects.delete(project_id)

    def add_indicator(
        self,
        project_id: str,
        series_id: str,
    ) -> None:
        self.find_indicator(series_id)
        self._projects.add_indicator(
            project_id, series_id
        )

    def remove_indicator(
        self,
        project_id: str,
        series_id: str,
    ) -> None:
        self._projects.remove_indicator(
            project_id, series_id
        )

    def chart_settings(
        self,
        project_id: str,
        series_id: str,
    ) -> dict:
        return self.get_project(project_id)[
            "chart_settings"
        ].get(series_id, {})

    def save_chart_settings(
        self,
        project_id: str,
        series_id: str,
        settings: dict,
    ) -> None:
        self._projects.save_chart_settings(
            project_id,
            series_id,
            settings,
        )

    # --------------------------------------
    # M2 STEP 3 — GDP synchronization
    # --------------------------------------

    def sync_gdp(
        self,
        request: FetchRequest,
        *,
        as_of_date: date,
        provider: DataProvider | None = None,
        earliest_available_date: date | None = None,
    ) -> PersistentSyncResult:
        """Execute and persist a GDP request.

        initialize_sync_storage() must have been called
        before the first synchronization.

        This operation is synchronous and must not run
        on the PySide6 main GUI thread.
        """
        coordinator = GDPSyncCoordinator(
            self._sync_storage
        )

        return coordinator.sync(
            request,
            as_of_date=as_of_date,
            provider=provider,
            earliest_available_date=earliest_available_date,
        )

    def get_gdp_history(
        self,
        date_range: DateRange | None = None,
    ) -> tuple[Observation, ...]:
        """Read persisted GDP observations only."""
        return self._storage.read_observations(
            "GDP",
            date_range,
        )

    def get_gdp_coverage(
        self,
    ) -> tuple[DateRange, ...]:
        """Read committed GDP coverage intervals."""
        return self._storage.read_coverage("GDP")

    def get_sync_status(
        self,
        request_id: str,
    ) -> dict | None:
        """Read a persisted synchronization run."""
        return self._sync_storage.read_run(request_id)

    def get_sync_ranges(
        self,
        request_id: str,
    ) -> list[dict]:
        """Read persisted interval-level execution logs."""
        return self._sync_storage.read_ranges(request_id)

    def get_gdp_audit(self) -> list[dict]:
        """Read historical GDP insert/update/invalidation logs."""
        return self._sync_storage.read_audit("GDP")
