
"""
Service（业务服务层）：量化系统 Backend 的业务逻辑与调度中心。
1. 核心职责：协调数据获取、处理、存储和查询，并向 Frontend 提供统一接口。
2. 数据获取：调用 Data Provider（如 FRED API）获取 GDP、利率、通胀等宏观经济数据。
3. 数据处理：检查数据有效性、处理缺失值、统一日期与数值格式。
4. 数据存储：调用 SQLiteStore，将处理后的数据写入 SQLite 数据库。
5. 数据查询：从 Storage 获取历史数据，整理后返回给 Frontend，用于图表展示和量化分析。
6. 架构原则：Service 负责业务逻辑；Storage 负责 SQL 和数据库操作；Frontend 负责界面展示；main.py 负责初始化和组装模块。

执行逻辑：
Frontend → Service → Data Provider / Storage → Service → Frontend

注意：你之前提供的 SQLiteStore 属于 Storage，并非 Service。目前尚未提供 service.py 的实际代码，因此以上解释的是 Service 的架构职责，而非具体代码实现。
"""

"""Single public service used by the UI; no PySide6 dependency."""

from pathlib import Path

from .catalog import (
    CATALOG, BY_ID, SYSTEM_SECTIONS, Indicator,
    indicators_for_section, indicators_for_ids,
)
from .projects import ProjectStore
from .storage import SQLiteStore


class MacroService:
    __slots__ = ("_storage", "_projects")

    def __init__(self, db_path: Path):
        path = Path(db_path)
        self._storage = SQLiteStore(path)
        self._projects = ProjectStore(path.parent / "projects.json")

    def initialize(self) -> None:
        self._storage.initialize()
        self._projects.initialize()

    def observation_count(self, series_id: str | None = None) -> int:
        return self._storage.count_observations(series_id)

    def sections(self) -> dict[str, str]:
        return SYSTEM_SECTIONS.copy()

    def indicators(self, kind: str, key: str) -> list[Indicator]:
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

        raise KeyError(f"Project not found: {project_id}")

    def create_project(self, name: str) -> dict:
        return self._projects.create(name)

    def rename_project(self, project_id: str, name: str) -> None:
        self._projects.rename(project_id, name)

    def delete_project(self, project_id: str) -> None:
        self._projects.delete(project_id)

    def add_indicator(self, project_id: str, series_id: str) -> None:
        self.find_indicator(series_id)
        self._projects.add_indicator(project_id, series_id)

    def remove_indicator(self, project_id: str, series_id: str) -> None:
        self._projects.remove_indicator(project_id, series_id)

    def chart_settings(self, project_id: str, series_id: str) -> dict:
        return self.get_project(project_id)["chart_settings"].get(
            series_id, {}
        )

    def save_chart_settings(
        self,
        project_id: str,
        series_id: str,
        settings: dict
    ) -> None:
        self._projects.save_chart_settings(
            project_id, series_id, settings
        )
