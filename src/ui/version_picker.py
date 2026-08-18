"""Searchable year/month grouped selector for one channel's price versions."""

from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.models import PriceVersion, VersionStatus


class VersionPicker(QWidget):
    version_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._versions: tuple[PriceVersion, ...] = ()
        self._selected_version_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.button = QToolButton()
        self.button.setObjectName("versionPickerButton")
        self.button.setText("无版本")
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.button)

        self.popup = QFrame(None, Qt.WindowType.Popup)
        self.popup.setFrameShape(QFrame.Shape.StyledPanel)
        popup_layout = QVBoxLayout(self.popup)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("versionSearch")
        self.search_edit.setPlaceholderText("搜索版本号、来源文件或导入日期")
        self.tree_widget = QTreeWidget()
        self.tree_widget.setObjectName("versionTree")
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.setMinimumSize(430, 300)
        popup_layout.addWidget(self.search_edit)
        popup_layout.addWidget(self.tree_widget)

        self.button.clicked.connect(self._show_popup)
        self.search_edit.textChanged.connect(self._apply_search)
        self.tree_widget.itemClicked.connect(self._select_item)

    @property
    def selected_version_id(self) -> str | None:
        return self._selected_version_id

    @property
    def versions(self) -> tuple[PriceVersion, ...]:
        return self._versions

    def set_versions(
        self,
        versions: tuple[PriceVersion, ...],
        *,
        preferred_version_id: str | None = None,
        select_active: bool = False,
    ) -> None:
        self._versions = tuple(
            sorted(versions, key=lambda version: (version.import_time, version.version_id), reverse=True)
        )
        available = {version.version_id for version in self._versions}
        active = next(
            (
                version
                for version in self._versions
                if version.status is VersionStatus.ACTIVE
            ),
            None,
        )
        if select_active:
            selected = active.version_id if active is not None else None
        elif preferred_version_id in available:
            selected = preferred_version_id
        elif self._selected_version_id in available:
            selected = self._selected_version_id
        else:
            selected = active.version_id if active is not None else None
        self._selected_version_id = selected
        self._rebuild_tree()
        self._update_button()

    def select_version(self, version_id: str | None, *, emit: bool = True) -> None:
        if version_id is not None and version_id not in {
            version.version_id for version in self._versions
        }:
            version_id = None
        changed = version_id != self._selected_version_id
        self._selected_version_id = version_id
        self._update_button()
        self._highlight_selected()
        if emit and changed:
            self.version_changed.emit(version_id)

    def selected_version(self) -> PriceVersion | None:
        return next(
            (
                version
                for version in self._versions
                if version.version_id == self._selected_version_id
            ),
            None,
        )

    def _rebuild_tree(self) -> None:
        self.tree_widget.clear()
        active = next(
            (
                version
                for version in self._versions
                if version.status is VersionStatus.ACTIVE
            ),
            None,
        )
        if active is not None:
            item = self._version_item(active, prefix="当前活动版本｜")
            self.tree_widget.addTopLevelItem(item)

        history = [
            version for version in self._versions if version.status is not VersionStatus.ACTIVE
        ]
        grouped: dict[int, dict[int, list[PriceVersion]]] = defaultdict(lambda: defaultdict(list))
        for version in history:
            grouped[version.import_time.year][version.import_time.month].append(version)

        latest_group: tuple[int, int] | None = None
        if history:
            latest_group = (history[0].import_time.year, history[0].import_time.month)
        for year in sorted(grouped, reverse=True):
            year_item = QTreeWidgetItem([f"{year} 年"])
            year_item.setData(0, Qt.ItemDataRole.UserRole, None)
            self.tree_widget.addTopLevelItem(year_item)
            for month in sorted(grouped[year], reverse=True):
                month_item = QTreeWidgetItem([f"{month:02d} 月"])
                month_item.setData(0, Qt.ItemDataRole.UserRole, None)
                year_item.addChild(month_item)
                for version in grouped[year][month]:
                    month_item.addChild(self._version_item(version))
                if latest_group == (year, month):
                    month_item.setExpanded(True)
            if latest_group is not None and latest_group[0] == year:
                year_item.setExpanded(True)

        self.search_edit.clear()
        self._highlight_selected()

    def _version_item(self, version: PriceVersion, *, prefix: str = "") -> QTreeWidgetItem:
        date_text = version.import_time.strftime("%Y-%m-%d %H:%M")
        item = QTreeWidgetItem(
            [f"{prefix}{version.version_id}｜{version.status.value}｜{date_text}"]
        )
        item.setData(0, Qt.ItemDataRole.UserRole, version.version_id)
        item.setData(
            0,
            Qt.ItemDataRole.UserRole + 1,
            f"{version.version_id} {version.source_file} {version.import_time.isoformat()}".casefold(),
        )
        item.setToolTip(0, f"来源：{version.source_file}")
        return item

    def _show_popup(self) -> None:
        if not self._versions:
            return
        width = max(self.width(), 460)
        self.popup.resize(width, 360)
        position = self.button.mapToGlobal(QPoint(0, self.button.height()))
        self.popup.move(position)
        self.popup.show()
        self.search_edit.setFocus()

    def _select_item(self, item: QTreeWidgetItem) -> None:
        version_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not version_id:
            item.setExpanded(not item.isExpanded())
            return
        self.select_version(str(version_id))
        self.popup.hide()

    def _apply_search(self, text: str) -> None:
        needle = text.strip().casefold()
        for index in range(self.tree_widget.topLevelItemCount()):
            top = self.tree_widget.topLevelItem(index)
            self._filter_item(top, needle)

    def _filter_item(self, item: QTreeWidgetItem, needle: str) -> bool:
        version_id = item.data(0, Qt.ItemDataRole.UserRole)
        if version_id:
            haystack = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
            visible = not needle or needle in haystack
            item.setHidden(not visible)
            return visible
        any_visible = False
        for index in range(item.childCount()):
            any_visible = self._filter_item(item.child(index), needle) or any_visible
        item.setHidden(not any_visible)
        if needle and any_visible:
            item.setExpanded(True)
        return any_visible

    def _highlight_selected(self) -> None:
        iterator = self.tree_widget.invisibleRootItem()
        self._highlight_children(iterator)

    def _highlight_children(self, parent: QTreeWidgetItem) -> None:
        for index in range(parent.childCount()):
            item = parent.child(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == self._selected_version_id:
                self.tree_widget.setCurrentItem(item)
            self._highlight_children(item)

    def _update_button(self) -> None:
        version = self.selected_version()
        if version is None:
            self.button.setText("无版本")
            self.button.setEnabled(bool(self._versions))
            return
        self.button.setEnabled(True)
        date_text = version.import_time.strftime("%Y-%m-%d")
        self.button.setText(f"{version.version_id}｜{version.status.value}｜{date_text}")
