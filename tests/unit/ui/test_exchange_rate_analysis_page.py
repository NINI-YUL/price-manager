from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic, sleep

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from src.database.connection import database_session
from src.database.seed import seed_database
from src.main import create_application, create_main_window
from src.models import Channel, ProviderRateBundle, RateFetchTrigger
from src.services import ExchangeRateService, PriceAnalysisService
from src.ui import ApplicationShell, ExchangeRateAnalysisPage


class StaticRateProvider:
    def fetch_latest(self) -> ProviderRateBundle:
        return ProviderRateBundle(
            provider="EXCHANGE_RATE_API",
            base_currency="USD",
            updated_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
            next_update_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            rates={
                "USD": Decimal(1),
                "JPY": Decimal(150),
                "KRW": Decimal(1400),
            },
        )


class NewSnapshotRateProvider:
    def fetch_latest(self) -> ProviderRateBundle:
        return ProviderRateBundle(
            provider="EXCHANGE_RATE_API",
            base_currency="USD",
            updated_at=datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
            next_update_at=datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
            rates={
                "USD": Decimal(1),
                "JPY": Decimal(150),
                "KRW": Decimal(1400),
            },
        )


def test_page_defaults_to_active_versions_and_one_common_snapshot(tmp_path: Path) -> None:
    _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path)

    page.activate()
    assert page.selected_versions == {
        Channel.GOOGLE: "GOOGLE_ACTIVE",
        Channel.IOS: "IOS_ACTIVE",
        Channel.WEB: None,
    }
    assert page.snapshot_combo.currentData() == page.current_snapshot.snapshot_id
    assert page.table.model().rowCount() == 3
    jpy_row = _row_for(page, 3, "JPY")
    assert _cell_text(page, jpy_row, 6) == "298.50"
    assert _cell_text(page, jpy_row, 7) == "+1.50"
    assert _cell_text(page, jpy_row, 8) == "+0.50%"
    assert _cell_text(page, jpy_row, 9) == "偏高"
    assert _cell_text(page, jpy_row, 10) == "正常"
    krw_row = _row_for(page, 3, "KRW")
    assert "超过两位小数" in _cell_tooltip(page, krw_row, 5)
    page.level_combo.setCurrentIndex(page.level_combo.findData("SIGNIFICANT"))
    assert page.table.model().rowCount() == 0
    assert "没有符合当前筛选" in page.status_label.text()

    assert "只读分析" in page.status_label.text()
    page.close()


def test_configuration_dialog_applies_snapshot_and_versions_together(
    tmp_path: Path,
) -> None:
    application = _application()
    database_path = _analysis_database(tmp_path)
    ExchangeRateService(database_path, provider=NewSnapshotRateProvider()).refresh(
        RateFetchTrigger.MANUAL,
        requested_at=datetime(2026, 8, 21, 8, 0, tzinfo=UTC),
    )
    page = ExchangeRateAnalysisPage(database_path)
    page.activate()
    page.show()
    application.processEvents()
    initial_snapshot_id = page.current_snapshot.snapshot_id
    table_model = page.table.model()
    changes: list[str] = []
    table_model.modelReset.connect(lambda: changes.append("reset"))

    assert "Google：活动版本" in page.configuration_versions_summary_label.text()

    page.configure_button.click()
    application.processEvents()
    assert page.configuration_dialog.isVisible()
    older_snapshot_id = page.snapshot_combo.itemData(page.snapshot_combo.count() - 1)
    page.snapshot_combo.setCurrentIndex(page.snapshot_combo.count() - 1)
    page.version_pickers[Channel.GOOGLE].select_version("GOOGLE_HISTORY")

    assert page.selected_versions[Channel.GOOGLE] == "GOOGLE_ACTIVE"
    assert page.current_snapshot.snapshot_id == initial_snapshot_id
    assert changes == []

    page.cancel_configuration_button.click()
    application.processEvents()
    assert not page.configuration_dialog.isVisible()
    assert page.selected_versions[Channel.GOOGLE] == "GOOGLE_ACTIVE"
    assert page.current_snapshot.snapshot_id == initial_snapshot_id

    page.configure_button.click()
    application.processEvents()
    page.snapshot_combo.setCurrentIndex(page.snapshot_combo.count() - 1)
    page.version_pickers[Channel.GOOGLE].select_version("GOOGLE_HISTORY")
    page.apply_configuration_button.click()
    application.processEvents()

    assert not page.configuration_dialog.isVisible()
    assert page.selected_versions[Channel.GOOGLE] == "GOOGLE_HISTORY"
    assert page.current_snapshot.snapshot_id == older_snapshot_id
    assert "Google：历史版本" in page.configuration_versions_summary_label.text()
    assert changes
    jpy_row = _row_for(page, 3, "JPY")
    assert _cell_text(page, jpy_row, 10) == "关注"

    with database_session(database_path) as connection:
        active = connection.execute(
            "SELECT version_id FROM price_versions WHERE channel = 'GOOGLE' AND status = 'ACTIVE'"
        ).fetchone()[0]
    assert active == "GOOGLE_ACTIVE"
    page.close()


def test_historical_version_selection_is_read_only_and_level_filterable(
    tmp_path: Path,
) -> None:
    _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path)
    page.activate()

    page.configure_button.click()
    page.version_pickers[Channel.GOOGLE].select_version("GOOGLE_HISTORY")
    page.apply_configuration_button.click()
    assert page.selected_versions[Channel.IOS] == "IOS_ACTIVE"
    jpy_row = _row_for(page, 3, "JPY")
    assert _cell_text(page, jpy_row, 10) == "关注"
    page.level_combo.setCurrentIndex(page.level_combo.findData("ATTENTION"))
    assert page.table.model().rowCount() == 1

    with database_session(database_path) as connection:
        active = connection.execute(
            "SELECT version_id FROM price_versions WHERE channel = 'GOOGLE' AND status = 'ACTIVE'"
        ).fetchone()[0]
    assert active == "GOOGLE_ACTIVE"
    page.close()


def test_main_navigation_opens_exchange_rate_analysis(tmp_path: Path) -> None:
    _application()
    database_path = _analysis_database(tmp_path)
    window = create_main_window(database_path, archives_path=tmp_path / "archives")
    shell = window.centralWidget()
    assert shell.exchange_rate_analysis_page.table.model().rowCount() == 0
    assert isinstance(shell, ApplicationShell)

    shell.analysis_navigation.click()

    assert shell.stack.currentWidget() is shell.exchange_rate_analysis_page
    assert shell.exchange_rate_analysis_page.table.model().rowCount() == 3
    window.close()


def test_manual_refresh_button_uses_snapshot_deduplication(tmp_path: Path) -> None:
    application = _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path, provider=StaticRateProvider())
    page.activate()

    page.refresh_button.click()
    deadline = monotonic() + 3
    while not page.refresh_button.isEnabled():
        application.processEvents()
        if monotonic() >= deadline:
            raise AssertionError("manual exchange-rate refresh did not finish")
        sleep(0.01)
    for _ in range(5):
        application.processEvents()
        sleep(0.01)

    with database_session(database_path) as connection:
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM exchange_rate_snapshots"
        ).fetchone()[0]
    assert snapshot_count == 1
    assert "当前已是最新汇率" in page.status_label.text()
    page.close()


class FailingRateProvider:
    def fetch_latest(self) -> ProviderRateBundle:
        raise RuntimeError("network unavailable")


class CountingRateProvider(StaticRateProvider):
    def __init__(self) -> None:
        self.calls = 0

    def fetch_latest(self) -> ProviderRateBundle:
        self.calls += 1
        return super().fetch_latest()


class SlowRateProvider(StaticRateProvider):
    def fetch_latest(self) -> ProviderRateBundle:
        sleep(0.1)
        return super().fetch_latest()


class BlockingRateProvider(StaticRateProvider):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def fetch_latest(self) -> ProviderRateBundle:
        self.started.set()
        self.release.wait(timeout=3)
        return super().fetch_latest()


def test_page_construction_is_lazy_until_analysis_is_opened(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _application()
    database_path = _analysis_database(tmp_path)
    calls: list[object] = []
    original = PriceAnalysisService.analyze

    def track_analysis(service, **kwargs):
        calls.append(kwargs)
        return original(service, **kwargs)

    monkeypatch.setattr(PriceAnalysisService, "analyze", track_analysis)

    page = ExchangeRateAnalysisPage(database_path)

    assert calls == []
    assert page.table.model().rowCount() == 0
    page.activate()
    assert len(calls) == 1
    page.close()


def test_failed_refresh_explicitly_uses_cache_and_shows_snapshot_age(tmp_path: Path) -> None:
    application = _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(
        database_path,
        provider=FailingRateProvider(),
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )
    page.activate()
    table_model = page.table.model()
    changes: list[str] = []
    table_model.modelReset.connect(lambda: changes.append("reset"))
    table_model.dataChanged.connect(lambda *_: changes.append("data"))
    table_model.rowsInserted.connect(lambda *_: changes.append("insert"))
    table_model.rowsRemoved.connect(lambda *_: changes.append("remove"))

    page.refresh_button.click()
    _wait_for_refresh(application, page)

    assert changes == []
    assert "正在使用缓存" in page.status_label.text()
    assert "获取时间" in page.snapshot_detail_label.text()
    assert "8.0 天" in page.snapshot_detail_label.text()
    assert "强提醒" in page.snapshot_detail_label.text()
    page.close()


def test_failed_refresh_without_snapshot_has_explicit_empty_state(tmp_path: Path) -> None:
    application = _application()
    database_path = tmp_path / "no-cached-rates.db"
    seed_database(database_path)
    page = ExchangeRateAnalysisPage(database_path, provider=FailingRateProvider())
    page.activate()

    page.refresh_button.click()
    _wait_for_refresh(application, page)

    assert "暂无可用汇率" in page.status_label.text()
    page.close()


def test_runtime_timer_fetches_without_loading_hidden_analysis_table(tmp_path: Path) -> None:
    application = _application()
    database_path = tmp_path / "automatic-rates.db"
    seed_database(database_path)
    provider = CountingRateProvider()
    page = ExchangeRateAnalysisPage(database_path, provider=provider)

    page.start_auto_refresh()
    _wait_until(application, lambda: provider.calls == 1 and page.refresh_button.isEnabled())

    with database_session(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM exchange_rate_snapshots").fetchone()[0]
    assert count == 1
    assert page.table.model().rowCount() == 0
    page.close()


def test_worker_reenables_button_when_local_database_write_fails(tmp_path: Path) -> None:
    application = _application()
    database_path = tmp_path / "missing-schema.db"
    page = ExchangeRateAnalysisPage(database_path, provider=StaticRateProvider())

    page.refresh_button.click()
    _wait_for_refresh(application, page)

    assert "本地汇率存储失败" in page.status_label.text()
    assert page.has_active_refresh is False
    page.close()


def test_close_waits_for_in_flight_refresh_thread(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "close-during-refresh.db"
    seed_database(database_path)
    page = ExchangeRateAnalysisPage(database_path, provider=SlowRateProvider())

    page.refresh_button.click()
    page.close()

    assert page.has_active_refresh is False


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 8, 21, 0, 0, tzinfo=UTC), "汇率在 24 小时内"),
        (datetime(2026, 8, 21, 0, 30, tzinfo=UTC), "超过 24 小时"),
    ],
)
def test_snapshot_freshness_uses_strict_twenty_four_hour_boundary(
    tmp_path: Path,
    now: datetime,
    expected: str,
) -> None:
    _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path, clock=lambda: now)

    page.activate()

    assert expected in page.snapshot_detail_label.text()
    page.close()


def test_no_selected_versions_has_specific_empty_state(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "no-price-versions.db"
    seed_database(database_path)
    ExchangeRateService(database_path, provider=StaticRateProvider()).refresh(
        RateFetchTrigger.MANUAL
    )
    page = ExchangeRateAnalysisPage(database_path)

    page.activate()

    assert page.table.model().rowCount() == 0
    assert "尚无正式价格版本" in page.status_label.text()
    page.close()


def test_shutdown_timeout_retains_running_thread_until_worker_finishes(tmp_path: Path) -> None:
    application = _application()
    database_path = tmp_path / "shutdown-timeout.db"
    seed_database(database_path)
    provider = BlockingRateProvider()
    page = ExchangeRateAnalysisPage(database_path, provider=provider)
    page.refresh_button.click()
    _wait_until(application, provider.started.is_set)

    completed = page.shutdown(wait_timeout_ms=1)

    assert completed is False
    assert page.has_active_refresh is True
    assert page.refresh_button.isEnabled() is False

    provider.release.set()
    _wait_for_refresh(application, page)

    assert page.has_active_refresh is False
    page.close()


def _wait_for_refresh(application, page: ExchangeRateAnalysisPage) -> None:
    _wait_until(
        application,
        lambda: page.refresh_button.isEnabled() and not page.has_active_refresh,
    )


def _wait_until(application, condition) -> None:
    deadline = monotonic() + 3
    while not condition():
        application.processEvents()
        if monotonic() >= deadline:
            raise AssertionError("exchange-rate background operation did not finish")
        sleep(0.01)
    for _ in range(5):
        application.processEvents()
        sleep(0.01)


def _application():
    return create_application(["exchange-rate-analysis-test"])


def _cell_text(page: ExchangeRateAnalysisPage, row: int, column: int) -> str:
    return str(page.table.model().index(row, column).data())


def _cell_tooltip(page: ExchangeRateAnalysisPage, row: int, column: int) -> str:
    index = page.table.model().index(row, column)
    return str(index.data(Qt.ItemDataRole.ToolTipRole) or "")


def _row_for(page: ExchangeRateAnalysisPage, column: int, text: str) -> int:
    return next(
        row for row in range(page.table.model().rowCount()) if _cell_text(page, row, column) == text
    )


def _analysis_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "exchange-rate-analysis-ui.db"
    seed_database(database_path)
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_HISTORY",
            "GOOGLE",
            "ARCHIVED",
            (("JP", "1.99", "JPY", "330"),),
        )
        _insert_version(
            connection,
            "GOOGLE_ACTIVE",
            "GOOGLE",
            "ACTIVE",
            (
                ("JP", "1.99", "JPY", "300"),
                ("US", "1.99", "USD", "1.99"),
            ),
        )
        _insert_version(
            connection,
            "IOS_ACTIVE",
            "IOS",
            "ACTIVE",
            (("KR", "1.99", "KRW", "2786.123"),),
        )
    ExchangeRateService(database_path, provider=StaticRateProvider()).refresh(
        RateFetchTrigger.MANUAL,
        requested_at=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
    )
    return database_path


def _insert_version(
    connection,
    version_id: str,
    channel: str,
    status: str,
    prices: tuple[tuple[str, str, str, str], ...],
) -> None:
    imported_at = "2026-08-20T08:00:00+08:00"
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
            f"{version_id}.xlsx",
            version_id.lower(),
            imported_at,
            status,
            len(prices),
        ),
    )
    connection.executemany(
        """
        INSERT INTO channel_prices
            (channel, country_code, usd_tier, currency, local_price,
             adjustment_mode, version_id, created_time)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            (channel, country, tier, currency, price, version_id, imported_at)
            for country, tier, currency, price in prices
        ),
    )


def test_unchanged_manual_refresh_does_not_rebuild_analysis_table(
    tmp_path: Path,
) -> None:
    application = _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path, provider=StaticRateProvider())
    page.activate()
    table_model = page.table.model()
    changes: list[str] = []
    table_model.modelReset.connect(lambda: changes.append("reset"))
    table_model.dataChanged.connect(lambda *_: changes.append("data"))
    table_model.rowsInserted.connect(lambda *_: changes.append("insert"))
    table_model.rowsRemoved.connect(lambda *_: changes.append("remove"))

    page.refresh_button.click()
    _wait_for_refresh(application, page)

    assert changes == []
    assert page.table.model().rowCount() == 3
    assert "当前已是最新汇率" in page.status_label.text()
    page.close()


def test_new_snapshot_refreshes_analysis_table(tmp_path: Path) -> None:
    application = _application()
    database_path = _analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path, provider=NewSnapshotRateProvider())
    page.activate()
    table_model = page.table.model()
    changes: list[str] = []
    table_model.modelReset.connect(lambda: changes.append("reset"))
    table_model.dataChanged.connect(lambda *_: changes.append("data"))
    table_model.rowsInserted.connect(lambda *_: changes.append("insert"))
    table_model.rowsRemoved.connect(lambda *_: changes.append("remove"))

    page.refresh_button.click()
    _wait_for_refresh(application, page)

    assert changes
    assert page.snapshot_combo.count() == 2
    assert page.table.model().rowCount() == 3
    assert "已获取最新汇率" in page.status_label.text()
    page.close()


def test_wide_filter_layout_keeps_labels_next_to_controls(tmp_path: Path) -> None:
    application = _application()
    page = ExchangeRateAnalysisPage(_analysis_database(tmp_path))
    page.activate()
    page.resize(2_000, 900)
    page.show()
    application.processEvents()

    labels = {label.text(): label for label in page.findChildren(QLabel)}
    pairs = (
        ("渠道", page.channel_combo),
        ("国家/地区", page.country_combo),
        ("USD 档位", page.tier_combo),
        ("币种", page.currency_combo),
        ("偏差方向", page.direction_combo),
        ("预警等级", page.level_combo),
    )
    for text, combo in pairs:
        label = labels[text]
        assert label.width() <= label.sizeHint().width() + 2
        assert combo.geometry().left() - label.geometry().right() <= 8
        assert combo.width() <= max(combo.sizeHint().width(), combo.minimumWidth()) + 2

    filter_group = page.reset_button.parentWidget()
    assert filter_group.width() - page.reset_button.geometry().right() >= 400

    page.close()


def test_acceptance_scale_filtering_stays_responsive(tmp_path: Path) -> None:
    application = _application()
    database_path = _acceptance_scale_analysis_database(tmp_path)
    page = ExchangeRateAnalysisPage(database_path)

    started = monotonic()
    page.activate()
    page.show()
    application.processEvents()
    initial_elapsed = monotonic() - started

    assert page.table.model().rowCount() == 5_348
    assert initial_elapsed < 1.0
    assert page.tier_combo.findText("USD 99.99") >= 0
    assert page.tier_combo.minimumWidth() >= 140
    assert page.tier_combo.view().minimumWidth() >= 140

    started = monotonic()
    page.channel_combo.setCurrentIndex(page.channel_combo.findData("GOOGLE"))
    elapsed = monotonic() - started

    assert page.table.model().rowCount() == 2_674
    assert elapsed < 0.15
    assert "共显示 2674 条" in page.status_label.text()

    filter_cases = (
        (page.country_combo, "AE", 28),
        (page.tier_combo, "0.99", 382),
        (page.currency_combo, "JPY", 2_674),
        (page.direction_combo, "NEGATIVE", 2_674),
        (page.level_combo, "SIGNIFICANT", 2_674),
    )
    for combo, value, expected_count in filter_cases:
        page.reset_button.click()
        combo.setCurrentIndex(combo.findData(value))
        assert page.table.model().rowCount() == expected_count
    page.close()


def _acceptance_scale_analysis_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "acceptance-scale-analysis-ui.db"
    seed_database(database_path)
    with database_session(database_path) as connection:
        countries = tuple(
            row[0]
            for row in connection.execute(
                "SELECT country_code FROM countries ORDER BY country_code"
            )
        )
        tiers = tuple(
            str(row[0]) for row in connection.execute("SELECT usd_price FROM price_tiers")
        )
        google_prices = tuple(
            (country_code, tier, "USD", tier) for country_code in countries for tier in tiers
        )
        ios_prices = tuple(
            (country_code, tier, "JPY", tier) for country_code in countries for tier in tiers
        )
        _insert_version(
            connection,
            "GOOGLE_SCALE_ACTIVE",
            "GOOGLE",
            "ACTIVE",
            google_prices,
        )
        _insert_version(
            connection,
            "IOS_SCALE_ACTIVE",
            "IOS",
            "ACTIVE",
            ios_prices,
        )
    ExchangeRateService(database_path, provider=StaticRateProvider()).refresh(
        RateFetchTrigger.MANUAL,
        requested_at=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
    )
    return database_path
