"""Main navigation shell shared by all Phase1 pages."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import DATABASE_PATH, RUNTIME_PATHS
from src.models import Channel, ConfirmationResult, VersionActivationResult
from src.ui.import_page import ImportPage
from src.ui.price_library_page import PriceLibraryPage
from src.ui.version_management_page import VersionManagementPage


class ApplicationShell(QWidget):
    IMPORT_INDEX = 0
    LIBRARY_INDEX = 1
    VERSION_INDEX = 2

    def __init__(
        self,
        database_path=DATABASE_PATH,
        *,
        archives_root: str | Path = RUNTIME_PATHS.archives,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        navigation = QHBoxLayout()
        self.navigation_group = QButtonGroup(self)
        self.navigation_group.setExclusive(True)
        self.import_navigation = self._navigation_button("价格导入", "importNavigation")
        self.library_navigation = self._navigation_button("价格库", "libraryNavigation")
        self.version_navigation = self._navigation_button("版本管理", "versionNavigation")
        navigation.addWidget(self.import_navigation)
        navigation.addWidget(self.library_navigation)
        navigation.addWidget(self.version_navigation)
        navigation.addStretch(1)
        root.addLayout(navigation)

        self.stack = QStackedWidget()
        self.stack.setObjectName("mainPageStack")
        self.import_page = ImportPage(
            database_path,
            archives_root=archives_root,
            parent=self.stack,
        )
        self.price_library_page = PriceLibraryPage(database_path, parent=self.stack)
        self.version_management_page = VersionManagementPage(
            database_path,
            archives_root=archives_root,
            parent=self.stack,
        )
        self.stack.addWidget(self.import_page)
        self.stack.addWidget(self.price_library_page)
        self.stack.addWidget(self.version_management_page)
        root.addWidget(self.stack, 1)

        self.import_navigation.clicked.connect(self.show_import)
        self.library_navigation.clicked.connect(self.show_library)
        self.version_navigation.clicked.connect(self.show_versions)
        self.import_page.confirmation_succeeded.connect(self._handle_confirmation)
        self.price_library_page.navigate_to_import.connect(self.show_import)
        self.version_management_page.view_in_library.connect(self._show_library_version)
        self.version_management_page.version_activated.connect(self._handle_activation)
        self.show_import()

    def show_import(self) -> None:
        self.stack.setCurrentIndex(self.IMPORT_INDEX)
        self.import_navigation.setChecked(True)

    def show_library(self) -> None:
        self.price_library_page.refresh()
        self.stack.setCurrentIndex(self.LIBRARY_INDEX)
        self.library_navigation.setChecked(True)

    def show_versions(self) -> None:
        self.version_management_page.refresh()
        self.stack.setCurrentIndex(self.VERSION_INDEX)
        self.version_navigation.setChecked(True)

    @Slot(object)
    def _handle_confirmation(self, result: ConfirmationResult) -> None:
        self.price_library_page.handle_confirmation(result)
        self.version_management_page.refresh()

    @Slot(object)
    def _handle_activation(self, result: VersionActivationResult) -> None:
        preferred = self.price_library_page.selected_versions
        preferred[result.channel] = result.activated_version_id
        self.price_library_page.refresh(preferred_versions=preferred)

    @Slot(object, str)
    def _show_library_version(self, channel: Channel, version_id: str) -> None:
        preferred = self.price_library_page.selected_versions
        preferred[channel] = version_id
        self.price_library_page.refresh(preferred_versions=preferred)
        self.stack.setCurrentIndex(self.LIBRARY_INDEX)
        self.library_navigation.setChecked(True)

    def _navigation_button(self, text: str, object_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setCheckable(True)
        button.setMinimumWidth(120)
        self.navigation_group.addButton(button)
        return button
