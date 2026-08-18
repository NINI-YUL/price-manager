from __future__ import annotations

import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QLabel, QPushButton

from src.database.connection import database_session
from src.database.seed import seed_database
from src.main import create_application, create_main_window
from src.models import Channel
from src.ui import (
    ApplicationShell,
    VersionActivationDialog,
    VersionDetailDialog,
    VersionManagementPage,
)
from src.utils.source_hash import file_sha256

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def test_page_lists_filters_counts_and_has_no_delete_or_export(tmp_path: Path) -> None:
    _application()
    database_path, archives = _version_database(tmp_path)
    page = VersionManagementPage(database_path, archives_root=archives)

    assert page.table.rowCount() == 3
    assert page.active_count_label.text() == "2"
    assert page.archived_count_label.text() == "1"
    assert page.issue_count_label.text() == "0"
    assert page.table.item(0, 2).text() == "ACTIVE"

    page.channel_combo.setCurrentIndex(page.channel_combo.findData(Channel.GOOGLE))
    assert page.table.rowCount() == 2
    page.status_combo.setCurrentIndex(page.status_combo.findData("ARCHIVED"))
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "GOOGLE_V20260817_001"
    page.reset_filters()
    page.search_edit.setText("google-history.xlsx")
    assert page.table.rowCount() == 1

    button_texts = {button.text() for button in page.findChildren(QPushButton)}
    assert "删除" not in button_texts
    assert "导出" not in button_texts
    page.close()


def test_detail_and_activation_dialogs_show_audit_and_require_acknowledgement(
    tmp_path: Path,
) -> None:
    _application()
    database_path, archives = _version_database(tmp_path)
    page = VersionManagementPage(database_path, archives_root=archives)
    detail = page._service.get_detail("GOOGLE_V20260817_001")
    dialog = VersionDetailDialog(detail)

    assert dialog.activate_button.isVisible() is False
    dialog.show()
    assert dialog.activate_button.isVisible()
    assessment = page._service.assess_activation("GOOGLE_V20260817_001")
    activation = VersionActivationDialog(assessment)
    confirm = activation.findChild(QPushButton, "confirmActivationButton")
    assert confirm is not None
    assert not confirm.isEnabled()
    activation.acknowledge_check.setChecked(True)
    assert confirm.isEnabled()
    warning = activation.findChild(QLabel, "activationWarning")
    assert warning is not None
    assert "记录数变化" in warning.text()
    dialog.close()
    activation.close()
    page.close()


def test_page_activation_refreshes_and_emits_result(tmp_path: Path, monkeypatch) -> None:
    _application()
    database_path, archives = _version_database(tmp_path)
    page = VersionManagementPage(database_path, archives_root=archives)
    emitted = []
    page.version_activated.connect(emitted.append)
    monkeypatch.setattr(
        VersionActivationDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr("src.ui.version_management_page.QMessageBox.information", lambda *a: None)

    page._activate("GOOGLE_V20260817_001")

    assert len(emitted) == 1
    assert emitted[0].activated_version_id == "GOOGLE_V20260817_001"
    google_rows = [
        row
        for row in range(page.table.rowCount())
        if page.table.item(row, 1).text() == "Google Play"
    ]
    statuses = {
        page.table.item(row, 0).text(): page.table.item(row, 2).text()
        for row in google_rows
    }
    assert statuses == {
        "GOOGLE_V20260817_001": "ACTIVE",
        "GOOGLE_V20260818_001": "ARCHIVED",
    }
    page.close()


def test_application_shell_navigation_and_price_library_linkage(tmp_path: Path) -> None:
    _application()
    database_path, archives = _version_database(tmp_path)
    window = create_main_window(database_path, archives_path=archives)
    shell = window.centralWidget()
    assert isinstance(shell, ApplicationShell)

    shell.version_navigation.click()
    assert shell.stack.currentWidget() is shell.version_management_page
    ios_before = shell.price_library_page.selected_versions[Channel.IOS]
    result = shell.version_management_page._service.activate(
        "GOOGLE_V20260817_001",
        note="UI linkage",
    )
    shell.version_management_page.version_activated.emit(result)
    assert (
        shell.price_library_page.selected_versions[Channel.GOOGLE]
        == "GOOGLE_V20260817_001"
    )
    assert shell.price_library_page.selected_versions[Channel.IOS] == ios_before

    shell.version_management_page.view_in_library.emit(
        Channel.GOOGLE,
        "GOOGLE_V20260818_001",
    )
    assert shell.stack.currentWidget() is shell.price_library_page
    assert (
        shell.price_library_page.selected_versions[Channel.GOOGLE]
        == "GOOGLE_V20260818_001"
    )
    window.close()


def _application():
    return create_application(["version-management-test"])


def _version_database(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "versions-ui.db"
    archives = tmp_path / "archives"
    seed_database(database_path)
    active_source = _archive_file(
        archives,
        "google/GOOGLE_V20260818_001/google-active.xlsx",
        b"google-active",
    )
    history_source = _archive_file(
        archives,
        "google/GOOGLE_V20260817_001/google-history.xlsx",
        b"google-history",
    )
    ios_source = _archive_zip(
        archives,
        "ios/IOS_V20260818_001/source.zip",
    )
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_V20260818_001",
            "GOOGLE",
            active_source,
            file_sha256(archives / active_source),
            "ACTIVE",
            (("US", "9.99", "USD", "9.99"), ("JP", "9.99", "JPY", "1500")),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260817_001",
            "GOOGLE",
            history_source,
            file_sha256(archives / history_source),
            "ARCHIVED",
            (("US", "9.99", "USD", "8.99"),),
        )
        _insert_version(
            connection,
            "IOS_V20260818_001",
            "IOS",
            ios_source,
            "ios-directory-digest",
            "ACTIVE",
            (("US", "9.99", "USD", "10.99"),),
        )
    return database_path, archives


def _archive_file(archives: Path, relative: str, content: bytes) -> str:
    path = archives / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return relative


def _archive_zip(archives: Path, relative: str) -> str:
    path = archives / relative
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0.99/prices.csv", "country,price\nUS,0.99\n")
    return relative


def _insert_version(
    connection,
    version_id: str,
    channel: str,
    source_file: str,
    source_sha256: str,
    status: str,
    prices: tuple[tuple[str, str, str, str], ...],
) -> None:
    connection.execute(
        """
        INSERT INTO price_versions
            (version_id, channel, source_file, source_sha256,
             import_time, status, record_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            channel,
            source_file,
            source_sha256,
            NOW.isoformat(),
            status,
            len(prices),
        ),
    )
    connection.executemany(
        """
        INSERT INTO channel_prices
            (channel, country_code, usd_tier, currency, local_price,
             version_id, created_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (channel, country, tier, currency, price, version_id, NOW.isoformat())
            for country, tier, currency, price in prices
        ),
    )
