"""frontend/explorer.py	中间指标目录、文件夹、搜索"""

"""Middle column: searchable folders and indicator selections."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

_ROLE = Qt.ItemDataRole.UserRole


class IndicatorExplorer(QFrame):
    indicator_selected = Signal(str)
    add_requested = Signal()
    remove_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setObjectName("explorer")
        self.setMinimumWidth(210)
        self._custom = False
        self._by_id: dict[str, QTreeWidgetItem] = {}
        self._groups: list[
            tuple[QTreeWidgetItem, list[tuple[QTreeWidgetItem, str]]]
        ] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 18, 15, 16)
        layout.setSpacing(12)

        self.title = QLabel("MACRO DATA")
        self.title.setObjectName("sectionHeading")
        layout.addWidget(self.title)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or series ID...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(15)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()

        self.add_btn = QPushButton("+ Add indicator")
        self.add_btn.clicked.connect(self.add_requested.emit)

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_current)

        buttons.addWidget(self.add_btn, 1)
        buttons.addWidget(self.remove_btn)
        layout.addLayout(buttons)

        self.add_btn.hide()
        self.remove_btn.hide()

    def set_contents(
        self,
        title: str,
        indicators: list,
        is_custom: bool
    ) -> None:
        self._custom = is_custom
        self.title.setText(title.upper())

        tree = self.tree
        was_enabled = tree.updatesEnabled()
        tree.setUpdatesEnabled(False)

        try:
            self._by_id.clear()
            self._groups.clear()
            tree.clear()
            self.search.clear()

            folders = {}

            for indicator in indicators:
                group_name = (
                    "Saved Indicators" if is_custom
                    else indicator.group
                )

                group = folders.get(group_name)

                if group is None:
                    folder = QTreeWidgetItem([group_name])
                    folder.setFlags(
                        folder.flags() & ~Qt.ItemFlag.ItemIsSelectable
                    )

                    group = (folder, [])
                    folders[group_name] = group
                    self._groups.append(group)

                label = f"{indicator.name}   ·   {indicator.series_id}"

                child = QTreeWidgetItem([label])
                child.setData(0, _ROLE, indicator.series_id)
                child.setToolTip(
                    0, f"{indicator.name} ({indicator.series_id})"
                )

                group[0].addChild(child)
                group[1].append((child, label.casefold()))
                self._by_id.setdefault(indicator.series_id, child)

            tree.addTopLevelItems([
                folder for folder, _ in self._groups
            ])
            tree.expandAll()

        finally:
            tree.setUpdatesEnabled(was_enabled)

        self.add_btn.setVisible(is_custom)
        self.remove_btn.setVisible(is_custom)
        self.remove_btn.setEnabled(False)

    def _filter(self, text: str) -> None:
        search = text.casefold().strip()

        for folder, children in self._groups:
            has_match = False

            for item, folded_text in children:
                match = search in folded_text
                hidden = not match

                if item.isHidden() != hidden:
                    item.setHidden(hidden)

                if match:
                    has_match = True

            hidden = not has_match

            if folder.isHidden() != hidden:
                folder.setHidden(hidden)

    def _selection_changed(self) -> None:
        item = self.tree.currentItem()
        series_id = item.data(0, _ROLE) if item is not None else None
        valid = isinstance(series_id, str)

        self.remove_btn.setEnabled(valid and self._custom)

        if valid:
            self.indicator_selected.emit(series_id)

    def select_indicator(self, series_id: str) -> None:
        item = self._by_id.get(series_id)

        if item is not None:
            self.tree.setCurrentItem(item)

    def _remove_current(self) -> None:
        item = self.tree.currentItem()

        if item is not None:
            series_id = item.data(0, _ROLE)

            if isinstance(series_id, str):
                self.remove_requested.emit(series_id)