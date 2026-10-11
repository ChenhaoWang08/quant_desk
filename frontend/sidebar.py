"""frontend/sidebar.py - 最左侧系统分类与自定义项目"""

"""Left column: built-in sections and persistent custom projects."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

_ROLE = Qt.ItemDataRole.UserRole


class ProjectSidebar(QFrame):
    project_selected = Signal(str, str)
    new_requested = Signal()
    rename_requested = Signal()
    delete_requested = Signal()

    def __init__(self, sections: dict[str, str], parent=None):
        super().__init__(parent)

        self.setObjectName("sidebar")
        self.setMinimumWidth(185)

        self._active = ("system", "macro")
        self._system_items: dict[str, QListWidgetItem] = {}
        self._custom_items: dict[str, QListWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(12)

        brand = QLabel("QuantDesk")
        brand.setObjectName("brand")
        layout.addWidget(brand)

        layout.addSpacing(12)
        layout.addWidget(self._heading("SYSTEM"))

        self.system_list = QListWidget()
        self.system_list.setFrameShape(QFrame.Shape.NoFrame)
        self.system_list.setFixedHeight(125)

        for key, name in sections.items():
            item = QListWidgetItem(name)
            item.setData(_ROLE, key)
            self.system_list.addItem(item)
            self._system_items[key] = item

        self.system_list.itemClicked.connect(
            self._on_system_clicked
        )
        layout.addWidget(self.system_list)

        custom_header = QHBoxLayout()
        custom_header.addWidget(self._heading("MY PROJECTS"))
        custom_header.addStretch()

        new_btn = QPushButton("+ New")
        new_btn.setObjectName("subtleButton")
        new_btn.clicked.connect(self.new_requested.emit)
        custom_header.addWidget(new_btn)

        layout.addLayout(custom_header)

        self.custom_list = QListWidget()
        self.custom_list.setFrameShape(QFrame.Shape.NoFrame)
        self.custom_list.itemClicked.connect(
            self._on_custom_clicked
        )
        layout.addWidget(self.custom_list, 1)

        actions = QHBoxLayout()

        self.rename_btn = QPushButton("Rename")
        self.delete_btn = QPushButton("Delete")

        self.rename_btn.clicked.connect(
            self.rename_requested.emit
        )
        self.delete_btn.clicked.connect(
            self.delete_requested.emit
        )

        actions.addWidget(self.rename_btn)
        actions.addWidget(self.delete_btn)
        layout.addLayout(actions)

        footer = QLabel("LOCAL MODE  ·  API OFFLINE")
        footer.setObjectName("muted")
        layout.addWidget(footer)

        self.set_active("system", "macro")

    @staticmethod
    def _heading(label: str) -> QLabel:
        widget = QLabel(label)
        widget.setObjectName("sectionHeading")
        return widget

    def _on_system_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(_ROLE)
        self.set_active("system", key)
        self.project_selected.emit("system", key)

    def _on_custom_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(_ROLE)
        self.set_active("custom", key)
        self.project_selected.emit("custom", key)

    def set_projects(self, projects: list[dict]) -> None:
        view = self.custom_list
        was_enabled = view.updatesEnabled()
        view.setUpdatesEnabled(False)

        try:
            self._custom_items.clear()
            view.clear()

            for project in projects:
                key = project["id"]
                item = QListWidgetItem(project["name"])
                item.setData(_ROLE, key)
                view.addItem(item)
                self._custom_items.setdefault(key, item)

            self.set_active(*self._active)
        finally:
            view.setUpdatesEnabled(was_enabled)

    def set_active(self, kind: str, key: str) -> None:
        self._active = (kind, key)

        self.system_list.clearSelection()
        self.custom_list.clearSelection()

        if kind == "system":
            view = self.system_list
            items = self._system_items
        else:
            view = self.custom_list
            items = self._custom_items

        item = items.get(key)

        if item is not None:
            item.setSelected(True)
            view.setCurrentItem(item)

        enabled = kind == "custom"
        self.rename_btn.setEnabled(enabled)
        self.delete_btn.setEnabled(enabled)