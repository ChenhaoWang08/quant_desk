"""frontend/main_window.py	组装这三个前端区域和处理导航事件"""

"""Compose the three UI panels; persistent operations use MacroService."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMainWindow, QMessageBox, QSplitter

from .explorer import IndicatorExplorer
from .sidebar import ProjectSidebar
from .theme import STYLE
from .workspace import ChartWorkspace


class MainWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self._sections = service.sections()
        self._active_kind = "system"
        self._active_key = "macro"
        self._current_series_id: str | None = None

        self.setWindowTitle("QuantDesk — Macro & Quant Research")
        self.resize(1360, 820)
        self.setMinimumSize(1050, 680)
        self.setStyleSheet(STYLE)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)

        self.sidebar = ProjectSidebar(self._sections)
        self.explorer = IndicatorExplorer()
        self.workspace = ChartWorkspace()

        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.explorer)
        splitter.addWidget(self.workspace)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([220, 260, 880])
        self.setCentralWidget(splitter)

        self.sidebar.project_selected.connect(self._switch_section)
        self.sidebar.new_requested.connect(self._create_project)
        self.sidebar.rename_requested.connect(self._rename_project)
        self.sidebar.delete_requested.connect(self._delete_project)
        self.explorer.indicator_selected.connect(self._open_indicator)
        self.explorer.add_requested.connect(self._add_indicator)
        self.explorer.remove_requested.connect(self._remove_indicator)
        self.workspace.settings_changed.connect(self._chart_settings_changed)

        self.sidebar.set_projects(service.list_projects())
        self._switch_section("system", "macro")
        self.statusBar().showMessage(
            "Local UI mode  ·  SQLite ready  ·  Provider APIs not configured"
        )

    def _switch_section(self, kind: str, key: str) -> None:
        self._active_kind = kind
        self._active_key = key
        self._current_series_id = None

        if kind == "system":
            name = self._sections[key]
            indicators = self.service.indicators(kind, key)
        else:
            project = self.service.get_project(key)
            if kind != "custom":
                raise ValueError(f"Unknown section kind: {kind}")

            name = project["name"]
            indicators = []

            for series_id in project["series_ids"]:
                try:
                    indicators.append(self.service.find_indicator(series_id))
                except KeyError:
                    pass

        self.explorer.set_contents(name, indicators, kind == "custom")
        self.workspace.show_indicator(None)
        self.sidebar.set_active(kind, key)

    def _open_indicator(self, series_id: str) -> None:
        self._current_series_id = series_id
        indicator = self.service.find_indicator(series_id)
        count = self.service.observation_count(series_id)

        settings = (
            self.service.chart_settings(self._active_key, series_id)
            if self._active_kind == "custom" else {}
        )

        self.workspace.show_indicator(indicator, count, settings)

    def _create_project(self) -> None:
        name, accepted = QInputDialog.getText(
            self, "New project", "Project name:"
        )
        if not accepted:
            return

        try:
            project = self.service.create_project(name)
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return

        self.sidebar.set_projects(self.service.list_projects())
        self._switch_section("custom", project["id"])

    def _rename_project(self) -> None:
        if self._active_kind != "custom":
            return

        project = self.service.get_project(self._active_key)

        name, accepted = QInputDialog.getText(
            self, "Rename project", "New name:",
            text=project["name"]
        )
        if not accepted:
            return

        try:
            self.service.rename_project(self._active_key, name)
        except (OSError, ValueError) as exc:
            self._warn(exc)
            return

        self.sidebar.set_projects(self.service.list_projects())
        self._switch_section("custom", self._active_key)

    def _delete_project(self) -> None:
        if self._active_kind != "custom":
            return

        project = self.service.get_project(self._active_key)

        answer = QMessageBox.question(
            self,
            "Delete project",
            f'Delete "{project["name"]}"?\n\n'
            "The project organization and chart settings "
            "will be removed. Economic observations are untouched.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            self.service.delete_project(self._active_key)
        except OSError as exc:
            self._warn(exc)
            return

        self.sidebar.set_projects(self.service.list_projects())
        self._switch_section("system", "macro")

    def _add_indicator(self) -> None:
        if self._active_kind != "custom":
            return

        project = self.service.get_project(self._active_key)
        included = set(project["series_ids"])

        choices = {
            f"{item.name}  [{item.series_id}]": item.series_id
            for item in self.service.all_indicators()
            if item.series_id not in included
        }

        if not choices:
            QMessageBox.information(
                self,
                "All indicators added",
                "This project already includes every catalog indicator."
            )
            return

        chosen, accepted = QInputDialog.getItem(
            self,
            "Add indicator",
            "Choose from the local catalog:",
            list(choices),
            0,
            False,
        )
        if not accepted:
            return

        try:
            series_id = choices[chosen]
            self.service.add_indicator(self._active_key, series_id)
        except (OSError, ValueError, KeyError) as exc:
            self._warn(exc)
            return

        self._switch_section("custom", self._active_key)
        self.explorer.select_indicator(series_id)

    def _remove_indicator(self, series_id: str) -> None:
        if self._active_kind != "custom":
            return

        try:
            self.service.remove_indicator(self._active_key, series_id)
        except OSError as exc:
            self._warn(exc)
            return

        self._switch_section("custom", self._active_key)

    def _chart_settings_changed(self, settings: dict) -> None:
        if self._active_kind != "custom" or not self._current_series_id:
            return

        try:
            self.service.save_chart_settings(
                self._active_key, self._current_series_id, settings
            )
        except (OSError, ValueError, KeyError) as exc:
            self._warn(exc)

    def _warn(self, error: Exception) -> None:
        QMessageBox.warning(self, "QuantDesk", str(error))