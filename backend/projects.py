"""backend/projects.py - 自定义项目删除增加改动，指标归属和 图标设置持久化"""

"""Local custom projects, stored as JSON separately from economic observations."""

import json
from pathlib import Path
from uuid import uuid4


class ProjectStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            self._read()
        else:
            self._write({"schema_version": 1, "projects": []})

    def _read(self) -> dict:
        data = json.loads(self.path.read_text(encoding="utf-8"))

        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or not isinstance(data.get("projects"), list)
        ):
            raise ValueError("Unsupported or invalid projects.json format")

        return data

    def _write(self, data: dict) -> None:
        temp = self.path.with_name(
            f"{self.path.name}.{uuid4().hex}.tmp"
        )

        try:
            temp.write_text(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    separators=(",", ":")
                ),
                encoding="utf-8"
            )
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)

    def list_projects(self) -> list[dict]:
        return self._read()["projects"]

    @staticmethod
    def _valid_name(name: str) -> str:
        name = name.strip()

        if not name or len(name) > 60:
            raise ValueError(
                "Project name must contain 1–60 characters"
            )

        return name

    @staticmethod
    def _find(data: dict, project_id: str) -> dict:
        for project in data["projects"]:
            if project["id"] == project_id:
                return project

        raise KeyError(f"Project not found: {project_id}")

    def create(self, name: str) -> dict:
        name = self._valid_name(name)
        data = self._read()
        folded_name = name.casefold()

        if any(
            p["name"].casefold() == folded_name
            for p in data["projects"]
        ):
            raise ValueError(
                "A project with that name already exists"
            )

        project = {
            "id": uuid4().hex,
            "name": name,
            "series_ids": [],
            "chart_settings": {}
        }

        data["projects"].append(project)
        self._write(data)
        return project

    def rename(self, project_id: str, name: str) -> None:
        name = self._valid_name(name)
        data = self._read()
        project = self._find(data, project_id)
        folded_name = name.casefold()

        if any(
            p["id"] != project_id
            and p["name"].casefold() == folded_name
            for p in data["projects"]
        ):
            raise ValueError(
                "A project with that name already exists"
            )

        if project["name"] != name:
            project["name"] = name
            self._write(data)

    def delete(self, project_id: str) -> None:
        data = self._read()
        projects = data["projects"]

        remaining = [
            p for p in projects
            if p["id"] != project_id
        ]

        if len(remaining) == len(projects):
            raise KeyError(f"Project not found: {project_id}")

        data["projects"] = remaining
        self._write(data)

    def add_indicator(
        self,
        project_id: str,
        series_id: str
    ) -> None:
        data = self._read()
        project = self._find(data, project_id)

        if series_id not in project["series_ids"]:
            project["series_ids"].append(series_id)
            self._write(data)

    def remove_indicator(
        self,
        project_id: str,
        series_id: str
    ) -> None:
        data = self._read()
        project = self._find(data, project_id)

        try:
            project["series_ids"].remove(series_id)
        except ValueError:
            return

        project["chart_settings"].pop(series_id, None)
        self._write(data)

    def save_chart_settings(
        self,
        project_id: str,
        series_id: str,
        settings: dict
    ) -> None:
        if settings.get("period") not in (
            "1Y", "5Y", "10Y", "MAX"
        ):
            raise ValueError("Unsupported period")

        if settings.get("chart_type") not in (
            "Line", "Area", "Bar"
        ):
            raise ValueError("Unsupported chart type")

        if type(settings.get("grid")) is not bool:
            raise ValueError("Grid must be true or false")

        data = self._read()
        project = self._find(data, project_id)

        if series_id not in project["series_ids"]:
            raise ValueError(
                "Indicator is not in this project"
            )

        new_settings = {
            "period": settings["period"],
            "chart_type": settings["chart_type"],
            "grid": settings["grid"]
        }

        if project["chart_settings"].get(series_id) != new_settings:
            project["chart_settings"][series_id] = new_settings
            self._write(data)