from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from src.database.connection import database_session
from src.database.seed import seed_database
from src.main import create_application, create_main_window
from src.models import Channel, ConfirmationResult
from src.services import PriceLibraryError
from src.ui import ApplicationShell, PriceDetailDialog, PriceLibraryPage


def test_page_defaults_formats_missing_cells_filters_and_two_views(tmp_path: Path) -> None:
    _application()
    database_path = _library_database(tmp_path)
    page = PriceLibraryPage(database_path)

    assert page.current_view == "TIER"
    assert page.selected_versions == {
        Channel.GOOGLE: "GOOGLE_V20260818_001",
        Channel.IOS: "IOS_V20260818_001",
        Channel.WEB: None,
    }
    assert page.tier_combo.currentData() == "9.99"
    assert page.table.rowCount() == 3
    assert _column_texts(page, 1) == ["JP", "KR", "US"]
    us_row = _row_for(page, 1, "US")
    assert page.table.item(us_row, 2).text() == "9.99 USD"
    assert page.table.item(us_row, 3).text() == "10.99 USD  [手动调价]"
    assert page.table.item(us_row, 4).text() == "—"
    assert page.table.item(us_row, 2).textAlignment() & int(Qt.AlignmentFlag.AlignHCenter)
    for column in range(page.table.columnCount()):
        assert page.table.horizontalHeaderItem(column).textAlignment() & int(
            Qt.AlignmentFlag.AlignHCenter
        )

    page.show_all_check.setChecked(True)
    assert page.table.rowCount() == 191
    page.search_edit.setText("Japan")
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "JP"

    page.reset_filters()
    assert page.table.rowCount() == 3
    page.view_combo.setCurrentIndex(page.view_combo.findData("COUNTRY"))
    page.country_combo.setCurrentIndex(page.country_combo.findData("US"))
    assert page.table.rowCount() == 14
    tier_row = _row_for(page, 0, "USD 9.99")
    assert page.table.item(tier_row, 1).text() == "9.99 USD"
    assert page.table.item(tier_row, 2).text() == "10.99 USD  [手动调价]"
    assert page.table.item(tier_row, 3).text() == "—"
    page.close()


def test_grouped_version_picker_searches_source_and_selects_history(tmp_path: Path) -> None:
    _application()
    page = PriceLibraryPage(_library_database(tmp_path))
    picker = page.version_pickers[Channel.GOOGLE]

    texts = _tree_texts(picker.tree_widget.invisibleRootItem())
    assert sum("GOOGLE_V20260818_001" in text for text in texts) == 1
    assert "2026 年" in texts
    assert "07 月" in texts
    assert "2025 年" in texts

    picker.search_edit.setText("google-july.xlsx")
    visible = _visible_leaf_texts(picker.tree_widget.invisibleRootItem())
    assert len(visible) == 1
    assert "GOOGLE_V20260705_001" in visible[0]

    picker.select_version("GOOGLE_V20260705_001")
    assert picker.selected_version_id == "GOOGLE_V20260705_001"
    assert page.selected_versions[Channel.IOS] == "IOS_V20260818_001"
    us_row = _row_for(page, 1, "US")
    assert page.table.item(us_row, 2).text() == "8.99 USD"
    page.close()


def test_clicking_price_opens_read_only_detail(tmp_path: Path, monkeypatch) -> None:
    _application()
    page = PriceLibraryPage(_library_database(tmp_path))
    opened: list[str] = []
    monkeypatch.setattr(
        PriceDetailDialog,
        "exec",
        lambda dialog: opened.append(dialog.windowTitle()),
    )

    us_row = _row_for(page, 1, "US")
    page.table.cellClicked.emit(us_row, 2)

    assert opened == ["价格详情（只读）"]
    page.close()


def test_confirmation_refreshes_one_channel_and_preserves_other_selection(tmp_path: Path) -> None:
    _application()
    database_path = _library_database(tmp_path)
    page = PriceLibraryPage(database_path)
    page.version_pickers[Channel.GOOGLE].select_version("GOOGLE_V20260705_001")
    ios_before = page.selected_versions[Channel.IOS]

    with database_session(database_path) as connection:
        connection.execute("UPDATE price_versions SET status = 'ARCHIVED' WHERE channel = 'GOOGLE'")
        _insert_version(
            connection,
            "GOOGLE_V20260818_002",
            "GOOGLE",
            "archive/google-new.xlsx",
            "2026-08-18T12:00:00+08:00",
            "ACTIVE",
            (("US", "9.99", "USD", "11.99", None),),
        )

    page.handle_confirmation(
        ConfirmationResult(
            task_id="TASK-G-NEW",
            channel=Channel.GOOGLE,
            version_id="GOOGLE_V20260818_002",
            archived_version_id="GOOGLE_V20260818_001",
            record_count=1,
            country_count=1,
            archive_path="google/GOOGLE_V20260818_002/source.xlsx",
            completed_time="2026-08-18T12:00:00+08:00",
        )
    )

    assert page.selected_versions[Channel.GOOGLE] == "GOOGLE_V20260818_002"
    assert page.selected_versions[Channel.IOS] == ios_before
    us_row = _row_for(page, 1, "US")
    assert page.table.item(us_row, 2).text() == "11.99 USD"
    page.close()


def test_main_navigation_opens_library_and_returns_to_import(tmp_path: Path) -> None:
    _application()
    database_path = _library_database(tmp_path)
    window = create_main_window(database_path, archives_path=tmp_path / "archives")
    shell = window.centralWidget()
    assert isinstance(shell, ApplicationShell)

    shell.library_navigation.click()
    assert shell.stack.currentWidget() is shell.price_library_page
    shell.price_library_page.navigate_to_import.emit()
    assert shell.stack.currentWidget() is shell.import_page
    window.close()


def test_manual_refresh_keeps_query_failure_visible(tmp_path: Path, monkeypatch) -> None:
    _application()
    page = PriceLibraryPage(_library_database(tmp_path))

    def fail(_selections):
        raise PriceLibraryError("synthetic read failure")

    monkeypatch.setattr(page._service, "load_prices", fail)
    page.refresh_button.click()

    assert page.table.rowCount() == 0
    assert "查询失败" in page.status_label.text()
    assert not page.import_button.isVisible()
    page.close()


def _application():
    return create_application(["price-library-test"])


def _column_texts(page: PriceLibraryPage, column: int) -> list[str]:
    return [page.table.item(row, column).text() for row in range(page.table.rowCount())]


def _row_for(page: PriceLibraryPage, column: int, text: str) -> int:
    return next(
        row for row in range(page.table.rowCount()) if page.table.item(row, column).text() == text
    )


def _tree_texts(parent) -> list[str]:
    texts: list[str] = []
    for index in range(parent.childCount()):
        item = parent.child(index)
        texts.append(item.text(0))
        texts.extend(_tree_texts(item))
    return texts


def _visible_leaf_texts(parent) -> list[str]:
    texts: list[str] = []
    for index in range(parent.childCount()):
        item = parent.child(index)
        if item.childCount():
            texts.extend(_visible_leaf_texts(item))
        elif not item.isHidden():
            texts.append(item.text(0))
    return texts


def _library_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "price-library-ui.db"
    seed_database(database_path)
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_V20251201_001",
            "GOOGLE",
            "archive/google-2025.xlsx",
            "2025-12-01T09:00:00+08:00",
            "ARCHIVED",
            (("US", "9.99", "USD", "7.99", None),),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260705_001",
            "GOOGLE",
            "archive/google-july.xlsx",
            "2026-07-05T09:00:00+08:00",
            "ARCHIVED",
            (("US", "9.99", "USD", "8.99", None),),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260818_001",
            "GOOGLE",
            "archive/google-active.xlsx",
            "2026-08-18T09:00:00+08:00",
            "ACTIVE",
            (
                ("US", "9.99", "USD", "9.99", None),
                ("JP", "9.99", "JPY", "1500", None),
            ),
        )
        _insert_version(
            connection,
            "IOS_V20260818_001",
            "IOS",
            "archive/ios-active.zip",
            "2026-08-18T10:00:00+08:00",
            "ACTIVE",
            (
                ("US", "9.99", "USD", "10.99", "MANUAL"),
                ("KR", "9.99", "KRW", "14000", "AUTOMATIC"),
            ),
        )
    return database_path


def _insert_version(
    connection,
    version_id: str,
    channel: str,
    source_file: str,
    import_time: str,
    status: str,
    prices: tuple[tuple[str, str, str, str, str | None], ...],
) -> None:
    connection.execute(
        """
        INSERT INTO price_versions
            (version_id, channel, source_file, source_sha256,
             import_time, status, record_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (version_id, channel, source_file, version_id.lower(), import_time, status, len(prices)),
    )
    connection.executemany(
        """
        INSERT INTO channel_prices
            (channel, country_code, usd_tier, currency, local_price,
             adjustment_mode, version_id, created_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (channel, country, tier, currency, price, mode, version_id, import_time)
            for country, tier, currency, price, mode in prices
        ),
    )
