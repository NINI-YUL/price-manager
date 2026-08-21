"""Phase2 read-only exchange-rate deviation analysis page."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.adapters.exchange_rate_api import ExchangeRateApiProvider
from src.config.settings import DATABASE_PATH
from src.models import (
    Channel,
    DeviationLevel,
    ExchangeRateRefreshResult,
    ExchangeRateSnapshot,
    PriceAnalysisRow,
    RateFetchTrigger,
    RateRefreshStatus,
    VersionStatus,
    format_usd_tier,
)
from src.services import ExchangeRateService, PriceAnalysisService, PriceLibraryService
from src.ui.exchange_rate_analysis_models import (
    LEVEL_LABELS,
    ExchangeRateAnalysisFilterProxyModel,
    ExchangeRateAnalysisTableModel,
)
from src.ui.price_library_page import CHANNEL_LABELS
from src.ui.version_picker import VersionPicker

LOGGER = logging.getLogger(__name__)


class _RefreshWorker(QObject):
    finished = Signal(object)

    def __init__(
        self,
        service: ExchangeRateService,
        trigger: RateFetchTrigger,
    ) -> None:
        super().__init__()
        self._service = service
        self._trigger = trigger

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.refresh(self._trigger)
        except Exception as error:  # noqa: BLE001 - worker must always reach a terminal signal
            result = ExchangeRateRefreshResult(
                status=RateRefreshStatus.FAILED,
                snapshot=None,
                message=f"本地汇率存储失败：{error}",
            )
        finally:
            self.finished.emit(result)
            QThread.currentThread().quit()


class ExchangeRateAnalysisPage(QWidget):
    """Bind independent channel versions to one immutable FX snapshot."""

    def __init__(
        self,
        database_path=DATABASE_PATH,
        *,
        provider=None,
        clock: Callable[[], datetime] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._exchange_service = ExchangeRateService(
            database_path,
            provider=provider or ExchangeRateApiProvider(),
            clock=self._clock,
        )
        self._analysis_service = PriceAnalysisService(database_path)
        self._library_service = PriceLibraryService(database_path)
        self._rows: tuple[PriceAnalysisRow, ...] = ()
        self._snapshots: tuple[ExchangeRateSnapshot, ...] = ()
        self._refresh_thread: QThread | None = None
        self._refresh_worker: _RefreshWorker | None = None
        self._updating = False
        self._loaded = False
        self._auto_refresh_enabled = False
        self._next_retry_not_before: datetime | None = None
        self._close_when_refresh_finishes = False
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setSingleShot(True)
        self._selected_versions = {channel: None for channel in Channel}
        self._selected_snapshot_id: str | None = None
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_timeout)
        self.version_pickers: dict[Channel, VersionPicker] = {}

        self._build_ui()
        self._connect_signals()

    @property
    def selected_versions(self) -> dict[Channel, str | None]:
        return dict(self._selected_versions)

    @property
    def current_snapshot(self) -> ExchangeRateSnapshot | None:
        return next(
            (
                snapshot
                for snapshot in self._snapshots
                if snapshot.snapshot_id == self._selected_snapshot_id
            ),
            None,
        )

    @property
    def has_active_refresh(self) -> bool:
        return self._refresh_thread is not None and self._refresh_thread.isRunning()

    def activate(self) -> None:
        self.refresh()
        self.start_auto_refresh()

    def start_auto_refresh(self) -> None:
        if self._auto_refresh_enabled:
            self._schedule_auto_refresh()
            return
        self._auto_refresh_enabled = True
        try:
            self._exchange_service.prune_failed_logs(now=self._clock())
        except Exception as error:  # noqa: BLE001 - maintenance must not block analysis
            LOGGER.warning("Failed to prune expired exchange-rate failure logs: %s", error)
        self._schedule_auto_refresh()

    @Slot()
    def _auto_refresh_timeout(self) -> None:
        if self._auto_refresh_enabled and not self.has_active_refresh:
            self._start_refresh(RateFetchTrigger.AUTO)

    def _schedule_auto_refresh(self) -> None:
        self._auto_refresh_timer.stop()
        if not self._auto_refresh_enabled or self.has_active_refresh:
            return
        now = self._clock()
        try:
            due_at = self._exchange_service.next_refresh_at(now)
        except Exception:  # noqa: BLE001 - retry local read failures without blocking UI
            due_at = now + timedelta(minutes=15)
        if self._next_retry_not_before is not None:
            due_at = max(due_at, self._next_retry_not_before)
        milliseconds = max(
            0,
            int((due_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds() * 1000),
        )
        self._auto_refresh_timer.start(min(milliseconds, 2_147_000_000))

    def shutdown(self, *, wait_timeout_ms: int = 20_000) -> bool:
        self._auto_refresh_enabled = False
        self._auto_refresh_timer.stop()
        thread = self._refresh_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.quit()
            if not thread.wait(wait_timeout_ms):
                self.status_label.setText("正在等待汇率请求安全结束，窗口暂不能关闭…")
                return False
        self._refresh_thread = None
        self._refresh_worker = None
        self.refresh_button.setEnabled(True)
        return True

    def shutdown_for_application_exit(self) -> None:
        """Wait deterministically so Qt never destroys a running child thread."""

        self._auto_refresh_enabled = False
        self._auto_refresh_timer.stop()
        thread = self._refresh_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.quit()
            thread.wait()
        self._refresh_thread = None
        self._refresh_worker = None
        self.refresh_button.setEnabled(True)

    def refresh(self, *, auto_fetch: bool = False) -> None:
        self._loaded = True
        previous_versions = dict(self._selected_versions)
        previous_snapshot = self._selected_snapshot_id
        self._updating = True
        try:
            self._catalog = self._library_service.load_catalog()
            for channel, picker in self.version_pickers.items():
                picker.set_versions(
                    self._catalog.versions_for(channel),
                    preferred_version_id=previous_versions.get(channel),
                )
                self._selected_versions[channel] = picker.selected_version_id
            self._snapshots = self._exchange_service.list_snapshots()
            self.snapshot_combo.clear()
            for snapshot in self._snapshots:
                updated = snapshot.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
                self.snapshot_combo.addItem(
                    f"{updated}｜1 USD = X 本地币种",
                    snapshot.snapshot_id,
                )
            self.load_more_snapshots_button.setVisible(len(self._snapshots) == 100)
            if previous_snapshot is not None:
                index = self.snapshot_combo.findData(previous_snapshot)
                if index >= 0:
                    self.snapshot_combo.setCurrentIndex(index)
            self._selected_snapshot_id = self.snapshot_combo.currentData()
            self._sync_configuration_controls()
            self._populate_filters()
        finally:
            self._updating = False
        self._run_analysis()
        if auto_fetch:
            self.start_auto_refresh()
        elif self._auto_refresh_enabled:
            self._schedule_auto_refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("汇率分析")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        heading.addWidget(title)
        heading.addStretch(1)
        self.source_label = QLabel(
            '主汇率源：<a href="https://www.exchangerate-api.com/">ExchangeRate-API</a>'
        )
        self.source_label.setOpenExternalLinks(True)
        heading.addWidget(self.source_label)
        self.refresh_button = QPushButton("获取最新汇率")
        self.refresh_button.setObjectName("exchangeRateRefreshButton")
        heading.addWidget(self.refresh_button)
        root.addLayout(heading)

        configuration_group = QGroupBox("分析配置")
        configuration_layout = QHBoxLayout(configuration_group)
        configuration_summary = QVBoxLayout()
        self.configuration_snapshot_summary_label = QLabel()
        self.configuration_versions_summary_label = QLabel()
        configuration_summary.addWidget(self.configuration_snapshot_summary_label)
        configuration_summary.addWidget(self.configuration_versions_summary_label)
        configuration_layout.addLayout(configuration_summary, 1)
        self.configure_button = QPushButton("调整分析配置")
        self.configure_button.setObjectName("configureExchangeRateAnalysis")
        configuration_layout.addWidget(self.configure_button)
        root.addWidget(configuration_group)
        self._build_configuration_dialog()

        filter_group = QGroupBox("筛选")
        filters = QHBoxLayout(filter_group)
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("全部渠道", None)
        for channel in Channel:
            self.channel_combo.addItem(CHANNEL_LABELS[channel], channel.value)
        self.country_combo = QComboBox()
        self.tier_combo = QComboBox()
        self.country_combo.setMinimumWidth(184)
        self.tier_combo.setMinimumWidth(140)
        self.tier_combo.view().setMinimumWidth(140)
        self.currency_combo = QComboBox()
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("全部方向", None)
        self.direction_combo.addItem("高于理论价", "POSITIVE")
        self.direction_combo.addItem("低于理论价", "NEGATIVE")
        self.level_combo = QComboBox()
        self.level_combo.addItem("全部等级", None)
        for level in DeviationLevel:
            self.level_combo.addItem(LEVEL_LABELS[level], level.value)
        for label, widget in (
            ("渠道", self.channel_combo),
            ("国家/地区", self.country_combo),
            ("USD 档位", self.tier_combo),
            ("币种", self.currency_combo),
            ("偏差方向", self.direction_combo),
            ("预警等级", self.level_combo),
        ):
            label_widget = QLabel(label)
            label_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            filters.addWidget(label_widget)
            filters.addWidget(widget)
            filters.addSpacing(12)
        self.reset_button = QPushButton("重置筛选")
        filters.addWidget(self.reset_button)
        filters.addStretch(1)
        root.addWidget(filter_group)

        self.status_label = QLabel()
        self.status_label.setObjectName("exchangeRateAnalysisStatus")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self._table_model = ExchangeRateAnalysisTableModel(self)
        self._filter_model = ExchangeRateAnalysisFilterProxyModel(self)
        self._filter_model.setSourceModel(self._table_model)
        self.table = QTableView()
        self.table.setObjectName("exchangeRateAnalysisTable")
        self.table.setModel(self._filter_model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)

    def _build_configuration_dialog(self) -> None:
        self.configuration_dialog = QDialog(self)
        self.configuration_dialog.setModal(True)
        self.configuration_dialog.setWindowTitle("调整分析配置")
        self.configuration_dialog.setMinimumWidth(720)
        dialog_layout = QVBoxLayout(self.configuration_dialog)

        note = QLabel("仅调整本次分析使用的数据，不修改活动版本、历史状态或审计记录。")
        note.setWordWrap(True)
        dialog_layout.addWidget(note)

        snapshot_group = QGroupBox("统一汇率快照")
        snapshot_layout = QGridLayout(snapshot_group)
        snapshot_layout.addWidget(QLabel("本次分析快照"), 0, 0)
        self.snapshot_combo = QComboBox()
        self.snapshot_combo.setObjectName("exchangeRateSnapshot")
        snapshot_layout.addWidget(self.snapshot_combo, 0, 1)
        self.load_more_snapshots_button = QPushButton("加载更早快照")
        self.load_more_snapshots_button.setObjectName("loadMoreExchangeRateSnapshots")
        snapshot_layout.addWidget(self.load_more_snapshots_button, 0, 2)
        self.snapshot_detail_label = QLabel()
        self.snapshot_detail_label.setWordWrap(True)
        snapshot_layout.addWidget(self.snapshot_detail_label, 1, 0, 1, 3)
        dialog_layout.addWidget(snapshot_group)

        version_group = QGroupBox("渠道价格版本（独立只读选择）")
        version_layout = QGridLayout(version_group)
        for row, channel in enumerate(Channel):
            version_layout.addWidget(QLabel(CHANNEL_LABELS[channel]), row, 0)
            picker = VersionPicker()
            picker.setObjectName(f"{channel.value.lower()}AnalysisVersionPicker")
            self.version_pickers[channel] = picker
            version_layout.addWidget(picker, row, 1)
        version_layout.setColumnStretch(1, 1)
        dialog_layout.addWidget(version_group)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_configuration_button = QPushButton("取消")
        self.apply_configuration_button = QPushButton("应用到本次分析")
        self.apply_configuration_button.setDefault(True)
        actions.addWidget(self.cancel_configuration_button)
        actions.addWidget(self.apply_configuration_button)
        dialog_layout.addLayout(actions)

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(lambda: self._start_refresh(RateFetchTrigger.MANUAL))
        self.configure_button.clicked.connect(self._open_configuration_dialog)
        self.snapshot_combo.currentIndexChanged.connect(self._update_draft_snapshot_detail)
        self.load_more_snapshots_button.clicked.connect(self._load_more_snapshots)
        self.cancel_configuration_button.clicked.connect(self.configuration_dialog.reject)
        self.apply_configuration_button.clicked.connect(self._apply_configuration)
        self.reset_button.clicked.connect(self._reset_filters)
        for combo in (
            self.channel_combo,
            self.country_combo,
            self.tier_combo,
            self.currency_combo,
            self.direction_combo,
            self.level_combo,
        ):
            combo.currentIndexChanged.connect(self._render)

    def _snapshot_for_id(self, snapshot_id: str | None) -> ExchangeRateSnapshot | None:
        return next(
            (snapshot for snapshot in self._snapshots if snapshot.snapshot_id == snapshot_id),
            None,
        )

    def _sync_configuration_controls(self) -> None:
        self.snapshot_combo.blockSignals(True)
        try:
            index = self.snapshot_combo.findData(self._selected_snapshot_id)
            self.snapshot_combo.setCurrentIndex(index)
        finally:
            self.snapshot_combo.blockSignals(False)
        for channel, picker in self.version_pickers.items():
            picker.select_version(self._selected_versions[channel], emit=False)
        self._update_draft_snapshot_detail()

    @Slot()
    def _open_configuration_dialog(self) -> None:
        self._sync_configuration_controls()
        self.configuration_dialog.open()

    @Slot()
    def _apply_configuration(self) -> None:
        self._selected_snapshot_id = self.snapshot_combo.currentData()
        self._selected_versions = {
            channel: picker.selected_version_id for channel, picker in self.version_pickers.items()
        }
        self.configuration_dialog.accept()
        self._run_analysis()

    @Slot()
    def _update_draft_snapshot_detail(self) -> None:
        snapshot = self._snapshot_for_id(self.snapshot_combo.currentData())
        if snapshot is None:
            self.snapshot_detail_label.setText("尚无成功汇率快照。")
            return
        self._update_snapshot_detail(snapshot)

    def _update_configuration_summary(self) -> None:
        snapshot = self.current_snapshot
        if snapshot is None:
            snapshot_text = "汇率快照　暂无可用汇率快照"
        else:
            updated = snapshot.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
            _, freshness = self._snapshot_freshness(snapshot)
            provider = (
                "ExchangeRate-API"
                if snapshot.provider == "EXCHANGE_RATE_API"
                else snapshot.provider
            )
            snapshot_text = f"汇率快照　{updated}｜{provider}｜1 USD = X｜{freshness}"
        self.configuration_snapshot_summary_label.setText(snapshot_text)

        compact_channel_labels = {
            Channel.GOOGLE: "Google",
            Channel.IOS: "iOS",
            Channel.WEB: "Web",
        }
        labels: list[str] = []
        version_ids: list[str] = []
        for channel, picker in self.version_pickers.items():
            version_id = self._selected_versions[channel]
            version = next(
                (item for item in picker.versions if item.version_id == version_id),
                None,
            )
            if version is None:
                state = "未选择"
            elif version.status is VersionStatus.ACTIVE:
                state = "活动版本"
            else:
                state = "历史版本"
            labels.append(f"{compact_channel_labels[channel]}：{state}")
            version_ids.append(f"{CHANNEL_LABELS[channel]}：{version_id or '未选择'}")
        self.configuration_versions_summary_label.setText("价格版本　" + "　".join(labels))
        self.configuration_versions_summary_label.setToolTip("；".join(version_ids))

    @Slot()
    def _load_more_snapshots(self) -> None:
        batch = self._exchange_service.list_snapshots(
            limit=100,
            offset=len(self._snapshots),
        )
        if not batch:
            self.load_more_snapshots_button.setVisible(False)
            return
        self._snapshots += batch
        self.snapshot_combo.blockSignals(True)
        try:
            for snapshot in batch:
                updated = snapshot.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
                self.snapshot_combo.addItem(
                    f"{updated}｜1 USD = X 本地币种",
                    snapshot.snapshot_id,
                )
        finally:
            self.snapshot_combo.blockSignals(False)
        self.load_more_snapshots_button.setVisible(len(batch) == 100)

    def _run_analysis(self) -> None:
        if self._updating:
            return
        snapshot = self.current_snapshot
        if snapshot is None:
            self._rows = ()
            self.snapshot_detail_label.setText("尚无成功汇率快照。")
            self._table_model.set_rows(self._rows)
            self._update_configuration_summary()
            self.status_label.setText(
                "尚无汇率数据，请点击“获取最新汇率”。现有价格与版本保持只读。"
            )
            self._render()
            return
        self._rows = self._analysis_service.analyze(
            selections=self.selected_versions,
            snapshot_id=snapshot.snapshot_id,
        )
        self._populate_filters()
        self._table_model.set_rows(self._rows)
        self._update_snapshot_detail(snapshot)
        self._update_configuration_summary()
        self._render()

    def _populate_filters(self) -> None:
        selections = {
            self.country_combo: self.country_combo.currentData(),
            self.tier_combo: self.tier_combo.currentData(),
            self.currency_combo: self.currency_combo.currentData(),
        }
        options = {
            self.country_combo: [
                (
                    f"{row.country.country_code}｜{row.country.name_cn}",
                    row.country.country_code,
                )
                for row in sorted(
                    {row.country.country_code: row for row in self._rows}.values(),
                    key=lambda row: row.country.country_code,
                )
            ],
            self.tier_combo: [
                (f"USD {format_usd_tier(tier)}", str(tier))
                for tier in sorted({row.usd_tier for row in self._rows})
            ],
            self.currency_combo: [
                (currency, currency) for currency in sorted({row.currency for row in self._rows})
            ],
        }
        for combo, values in options.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部", None)
            for label, value in values:
                combo.addItem(label, value)
            index = combo.findData(selections[combo])
            combo.setCurrentIndex(max(index, 0))
            combo.blockSignals(False)

    @Slot()
    def _render(self) -> None:
        if self._updating:
            return
        self._filter_model.set_filters(
            channel=self.channel_combo.currentData(),
            country_code=self.country_combo.currentData(),
            usd_tier=self.tier_combo.currentData(),
            currency=self.currency_combo.currentData(),
            direction=self.direction_combo.currentData(),
            level=self.level_combo.currentData(),
        )
        row_count = self._filter_model.rowCount()
        if self.current_snapshot is None:
            return
        if not any(self.selected_versions.values()):
            message = "尚无正式价格版本可供分析。"
        elif row_count == 0 and self._rows:
            message = "没有符合当前筛选的数据。"
        elif not self._rows:
            message = "所选版本没有可分析价格。"
        else:
            message = f"共显示 {row_count} 条；只读分析，结果动态计算且不反写价格库。"
        if "只读分析" not in message:
            message += "；只读分析，结果动态计算且不反写价格库。"
        self.status_label.setText(message)

    @Slot()
    def _reset_filters(self) -> None:
        for combo in (
            self.channel_combo,
            self.country_combo,
            self.tier_combo,
            self.currency_combo,
            self.direction_combo,
            self.level_combo,
        ):
            combo.setCurrentIndex(0)
        self._render()

    def _snapshot_freshness(self, snapshot: ExchangeRateSnapshot) -> tuple[float, str]:
        now = self._clock().astimezone(UTC)
        age = max(timedelta(0), now - snapshot.updated_at.astimezone(UTC))
        age_days = age.total_seconds() / (24 * 60 * 60)
        if age.total_seconds() > 7 * 24 * 60 * 60:
            freshness = "强提醒：汇率已过期，超过 7 天"
        elif age.total_seconds() > 24 * 60 * 60:
            freshness = "提醒：汇率已过期，超过 24 小时"
        else:
            freshness = "汇率在 24 小时内"
        return age_days, freshness

    def _update_snapshot_detail(self, snapshot: ExchangeRateSnapshot) -> None:
        age_days, freshness = self._snapshot_freshness(snapshot)
        self.snapshot_detail_label.setText(
            f"供应商更新时间：{snapshot.updated_at.astimezone().isoformat(timespec='seconds')}；"
            f"获取时间：{snapshot.fetched_at.astimezone().isoformat(timespec='seconds')}；"
            f"汇率年龄：{age_days:.1f} 天；"
            f"下一更新时间：{snapshot.next_update_at.astimezone().isoformat(timespec='seconds')}；"
            f"{freshness}"
        )

    def _start_refresh(self, trigger: RateFetchTrigger) -> None:
        if self._refresh_thread is not None:
            return
        self.refresh_button.setEnabled(False)
        self.status_label.setText("正在获取 ExchangeRate-API 最新汇率…")
        thread = QThread(self)
        worker = _RefreshWorker(self._exchange_service, trigger)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._refresh_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_refresh_thread)
        self._refresh_thread = thread
        self._refresh_worker = worker
        thread.start()

    @Slot(object)
    def _refresh_finished(self, result: ExchangeRateRefreshResult) -> None:
        self.refresh_button.setEnabled(True)
        if self._loaded and result.status is RateRefreshStatus.CREATED:
            try:
                self.refresh()
            except Exception as error:  # noqa: BLE001 - surface local storage failures in UI
                result = ExchangeRateRefreshResult(
                    status=RateRefreshStatus.FAILED,
                    snapshot=None,
                    message=f"本地汇率存储失败：{error}",
                )
        if result.status is RateRefreshStatus.FAILED:
            self._next_retry_not_before = self._clock() + timedelta(minutes=15)
            if result.snapshot is not None:
                message = f"{result.message}；正在使用缓存"
            else:
                message = f"暂无可用汇率；{result.message}"
        elif result.status is RateRefreshStatus.NOT_MODIFIED:
            self._next_retry_not_before = self._clock() + timedelta(minutes=15)
            message = result.message
        else:
            self._next_retry_not_before = None
            message = result.message
        self.status_label.setText(f"{message}；只读分析，不修改价格、版本或状态。")

    @Slot()
    def _clear_refresh_thread(self) -> None:
        self._refresh_thread = None
        self._refresh_worker = None
        if self._close_when_refresh_finishes:
            self._close_when_refresh_finishes = False
            QTimer.singleShot(0, self.close)
        elif self._auto_refresh_enabled:
            self._schedule_auto_refresh()

    def closeEvent(self, event) -> None:
        if not self.shutdown():
            self._close_when_refresh_finishes = True
            event.ignore()
            return
        self._close_when_refresh_finishes = False
        super().closeEvent(event)
