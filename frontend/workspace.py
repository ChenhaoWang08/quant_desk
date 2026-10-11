"""Right column: chart shell, controls and truthful data status."""

import pyqtgraph as pg
from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)


class ChartWorkspace(QWidget):
    settings_changed = Signal(dict)

    PERIODS = ("1Y", "5Y", "10Y", "MAX")
    DEFAULT_SETTINGS = {
        "period": "5Y",
        "chart_type": "Line",
        "grid": True,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        self.breadcrumb = QLabel("QuantDesk / Select a series")
        self.breadcrumb.setObjectName("muted")
        layout.addWidget(self.breadcrumb)

        self.title = QLabel("Select an indicator")
        self.title.setObjectName("workspaceTitle")
        layout.addWidget(self.title)

        self.subtitle = QLabel(
            "Select a category and series from the left panels."
        )
        self.subtitle.setObjectName("muted")
        layout.addWidget(self.subtitle)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(7)

        self.period_buttons: dict[str, QPushButton] = {}
        self._period_group = QButtonGroup(self)

        for index, period in enumerate(self.PERIODS):
            button = QPushButton(period)
            button.setCheckable(True)
            button.setObjectName("periodButton")
            button.clicked.connect(self._settings_modified)
            self._period_group.addButton(button, index)
            self.period_buttons[period] = button
            toolbar.addWidget(button)

        self.period_buttons["5Y"].setChecked(True)
        toolbar.addSpacing(12)

        self.chart_type = QComboBox()
        self.chart_type.addItems(["Line", "Area", "Bar"])
        self.chart_type.currentTextChanged.connect(self._settings_modified)
        toolbar.addWidget(self.chart_type)

        self.grid_checkbox = QCheckBox("Grid")
        self.grid_checkbox.setChecked(True)
        self.grid_checkbox.toggled.connect(self._settings_modified)
        toolbar.addWidget(self.grid_checkbox)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        chart_card = QFrame()
        chart_card.setObjectName("card")
        chart_card.setMinimumHeight(270)

        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(10, 10, 10, 10)
        self.chart_stack = QStackedWidget()

        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.addStretch()

        self.empty_title = QLabel("Select an indicator")
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty_description = QLabel(
            "The chart component is ready; no data has been loaded."
        )
        self.empty_description.setObjectName("muted")
        self.empty_description.setWordWrap(True)
        self.empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)

        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_description)
        empty_layout.addStretch()
        self.chart_stack.addWidget(self.empty_state)

        # Plot is intentionally empty until chart data loading is implemented.
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#ffffff")
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self._grid_visible = True
        self.plot.setLabel("bottom", "Date")
        self.plot.setLabel("left", "Value")
        self.plot.getPlotItem().hideButtons()
        self.chart_stack.addWidget(self.plot)

        chart_layout.addWidget(self.chart_stack)
        layout.addWidget(chart_card, 1)

        info_card = QFrame()
        info_card.setObjectName("card")
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(16, 14, 16, 14)
        info_layout.addWidget(QLabel("DATA INFORMATION"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        grid.setVerticalSpacing(9)
        self.fields: dict[str, QLabel] = {}

        for idx, (key, caption) in enumerate((
            ("source", "Source"),
            ("database", "Local Database"),
            ("api", "Provider API"),
            ("count", "Observations"),
            ("sync", "Last Successful Sync"),
            ("link", "Official Link"),
        )):
            label = QLabel(caption)
            label.setObjectName("muted")

            value = QLabel("—")
            value.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextBrowserInteraction
            )
            value.setOpenExternalLinks(True)
            self.fields[key] = value

            field = QVBoxLayout()
            field.setSpacing(2)
            field.addWidget(label)
            field.addWidget(value)
            grid.addLayout(field, *divmod(idx, 2))

        info_layout.addLayout(grid)
        layout.addWidget(info_card)

        self._set_enabled(False)
        self.show_indicator(None)

    def _set_enabled(self, enabled: bool) -> None:
        for button in self.period_buttons.values():
            button.setEnabled(enabled)
        self.chart_type.setEnabled(enabled)
        self.grid_checkbox.setEnabled(enabled)

    def settings(self) -> dict:
        selected = self._period_group.checkedId()
        return {
            "period": self.PERIODS[selected] if selected >= 0 else "5Y",
            "chart_type": self.chart_type.currentText(),
            "grid": self.grid_checkbox.isChecked(),
        }

    def _set_grid(self, enabled: bool) -> None:
        if enabled != self._grid_visible:
            self.plot.showGrid(x=enabled, y=enabled, alpha=0.12)
            self._grid_visible = enabled

    def _apply_settings(self, settings: dict) -> None:
        period = settings.get("period", "5Y")
        if period not in self.period_buttons:
            period = "5Y"
        self.period_buttons[period].setChecked(True)

        grid = bool(settings.get("grid", True))
        with QSignalBlocker(self.chart_type), QSignalBlocker(self.grid_checkbox):
            self.chart_type.setCurrentText(settings.get("chart_type", "Line"))
            self.grid_checkbox.setChecked(grid)

        self._set_grid(grid)

    def _settings_modified(self, *_args) -> None:
        if self.sender() is self.grid_checkbox:
            self._set_grid(self.grid_checkbox.isChecked())

        if self._series_id:
            self.settings_changed.emit(self.settings())

    def show_indicator(
        self,
        indicator,
        count: int = 0,
        settings: dict | None = None,
    ) -> None:
        self._series_id = indicator.series_id if indicator else None
        self._set_enabled(indicator is not None)
        self._apply_settings(settings or {})
        self.chart_stack.setCurrentWidget(self.empty_state)

        self.fields["database"].setText("Ready (SQLite)")
        self.fields["api"].setText("Not configured")
        self.fields["sync"].setText("Never")

        if indicator is None:
            self.breadcrumb.setText("QuantDesk / Select a series")
            self.title.setText("Select an indicator")
            self.subtitle.setText("Choose an indicator from the middle panel.")
            self.empty_title.setText("No indicator selected")
            self.empty_description.setText("Browse a folder to select an indicator.")
            self.fields["source"].setText("—")
            self.fields["count"].setText("—")
            self.fields["link"].setText("—")
            return

        self.breadcrumb.setText(
            f"QuantDesk / {indicator.group} / {indicator.series_id}"
        )
        self.title.setText(indicator.name)
        self.subtitle.setText(
            f"{indicator.series_id}  ·  United States  ·  "
            f"{indicator.frequency}  ·  {indicator.units}"
        )
        self.empty_title.setText(
            "No Local Data" if count == 0 else "Chart Display Not Connected Yet"
        )
        self.empty_description.setText(
            "No economic observations have been imported. API integration comes later."
            if count == 0
            else (
                f"{count} observations exist locally; "
                "chart data loading is not implemented in this UI phase."
            )
        )
        self.fields["source"].setText(indicator.source)
        self.fields["count"].setText(str(count))
        self.fields["link"].setText(
            f'<a href="{indicator.source_url}">Open official series ↗</a>'
        )