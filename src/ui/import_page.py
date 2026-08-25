"""Shared three-channel import, inspection, and confirmation page."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import DATABASE_PATH, RUNTIME_PATHS
from src.database.connection import database_session
from src.database.repositories import ReferenceDataRepository
from src.models import Channel, ConfirmationError, ImportPreview, ImportTaskStatus
from src.services import (
    GoogleImportService,
    ImportConfirmationService,
    IosImportService,
    WebImportService,
)
from src.ui.import_table_models import (
    SEVERITY_LABELS,
    PricePreviewFilterProxyModel,
    PricePreviewTableModel,
    localized_confirmation_message,
    localized_issue_message,
)

CHANNEL_LABELS = {
    Channel.GOOGLE: "Google Play",
    Channel.IOS: "iOS App Store",
    Channel.WEB: "三方网页",
}


class _ParseWorker(QObject):
    succeeded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, database_path, channel: Channel, source_path: str, task_id: str) -> None:
        super().__init__()
        self._database_path = database_path
        self._channel = channel
        self._source_path = source_path
        self._task_id = task_id

    @Slot()
    def run(self) -> None:
        try:
            service = {
                Channel.GOOGLE: GoogleImportService,
                Channel.IOS: IosImportService,
                Channel.WEB: WebImportService,
            }[self._channel](self._database_path)
            preview = service.preview(self._source_path, task_id=self._task_id)
        except Exception as error:  # noqa: BLE001 - worker must report every failure
            self.failed.emit(str(error))
            return
        self.succeeded.emit(preview, self._task_id)


class ImportPage(QWidget):
    preview_loaded = Signal(object, str)
    parse_failed = Signal(str)
    confirmation_succeeded = Signal(object)

    def __init__(
        self,
        database_path=DATABASE_PATH,
        *,
        archives_root=RUNTIME_PATHS.archives,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._database_path = database_path
        self._confirmation_service = ImportConfirmationService(
            database_path,
            archives_root=archives_root,
        )
        self._preview: ImportPreview | None = None
        self._task_id: str | None = None
        self._source_path = ""
        self._parse_thread: QThread | None = None
        self._parse_worker: _ParseWorker | None = None
        self._technical_error: str | None = None
        self._country_names = self._load_country_names()

        self._build_ui()
        self._connect_signals()
        self._update_action_state()
        self._report_orphaned_archives()

    @property
    def current_preview(self) -> ImportPreview | None:
        return self._preview

    @property
    def current_task_id(self) -> str | None:
        return self._task_id

    @property
    def selected_channel(self) -> Channel:
        return Channel(str(self.channel_combo.currentData()))

    @property
    def source_selection_mode(self) -> str:
        return "directory" if self.selected_channel is Channel.IOS else "file"

    def set_source_path(self, source_path: str | Path) -> None:
        self._source_path = str(Path(source_path).expanduser().resolve())
        self.path_edit.setText(self._source_path)
        self._clear_preview()
        self._update_action_state()

    def start_parse(self) -> None:
        if self._parse_thread is not None or not self._source_path:
            return
        task_id = self._new_task_id()
        self._set_busy(True)
        self.status_label.setText("正在后台解析，请稍候…")
        thread = QThread(self)
        worker = _ParseWorker(
            self._database_path,
            self.selected_channel,
            self._source_path,
            task_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_parse_succeeded)
        worker.failed.connect(self._on_parse_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_parse_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._parse_thread = thread
        self._parse_worker = worker
        thread.start()

    def show_preview(self, preview: ImportPreview, task_id: str) -> None:
        self._preview = preview
        self._task_id = task_id
        self._source_path = preview.source_path
        self._technical_error = None
        self.path_edit.setText(preview.source_path)
        self._populate_summary()
        self._populate_filter_options()
        self.price_model.set_records(preview.records, self._country_names)
        self._apply_price_filters()
        self._populate_issue_table()
        if preview.status is ImportTaskStatus.FAILED:
            self.status_label.setText("解析失败，请根据问题列表修正来源后重新解析。")
        elif preview.has_blocking_errors:
            self.status_label.setText("检查发现阻断错误，不能确认入库。")
        elif preview.statistics.warning_count:
            self.status_label.setText("检查完成：存在警告，确认入库时需要二次确认。")
        else:
            self.status_label.setText("检查完成：可以确认入库。")
        self._update_action_state()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        title = QLabel("价格导入与检查")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        root.addWidget(title)

        source_group = QGroupBox("1. 选择来源并解析")
        source_layout = QFormLayout(source_group)
        self.channel_combo = QComboBox()
        self.channel_combo.setObjectName("channelCombo")
        self.channel_combo.addItem("Google Play", Channel.GOOGLE)
        self.channel_combo.addItem("iOS App Store", Channel.IOS)
        self.channel_combo.addItem("三方网页", Channel.WEB)
        source_layout.addRow("渠道", self.channel_combo)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("sourcePath")
        self.path_edit.setReadOnly(True)
        self.browse_button = QPushButton("选择文件")
        self.browse_button.setObjectName("browseButton")
        self.parse_button = QPushButton("开始解析")
        self.parse_button.setObjectName("parseButton")
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)
        path_row.addWidget(self.parse_button)
        source_layout.addRow("来源", path_row)
        root.addWidget(source_group)

        self.status_label = QLabel("请选择渠道和来源文件。")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        summary_group = QGroupBox("2. 检查摘要")
        summary_layout = QHBoxLayout(summary_group)
        self.summary_label = QLabel("尚未解析")
        self.summary_label.setObjectName("summaryLabel")
        self.summary_label.setWordWrap(True)
        summary_layout.addWidget(self.summary_label)
        root.addWidget(summary_group)

        self.inspection_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.inspection_splitter.setObjectName("inspectionSplitter")

        self.detail_group = QGroupBox("解析详情")
        detail_layout = QVBoxLayout(self.detail_group)
        price_filters = QHBoxLayout()
        self.country_filter = QComboBox()
        self.country_filter.setObjectName("countryFilter")
        self.tier_filter = QComboBox()
        self.tier_filter.setObjectName("tierFilter")
        price_filters.addWidget(QLabel("国家/地区"))
        price_filters.addWidget(self.country_filter)
        price_filters.addWidget(QLabel("档位"))
        price_filters.addWidget(self.tier_filter)
        price_filters.addStretch(1)
        detail_layout.addLayout(price_filters)

        self.price_model = PricePreviewTableModel(self)
        self.price_proxy = PricePreviewFilterProxyModel(self)
        self.price_proxy.setSourceModel(self.price_model)
        self.price_table = QTableView()
        self.price_table.setObjectName("priceTable")
        self.price_table.setModel(self.price_proxy)
        self.price_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.price_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.price_table.setAlternatingRowColors(True)
        row_header = self.price_table.verticalHeader()
        row_header.setVisible(True)
        row_header.setMinimumWidth(48)
        row_header.setDefaultSectionSize(26)
        row_header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header = self.price_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        for column, width in enumerate((74, 110, 82, 66, 92, 220, 92, 130, 58, 58)):
            self.price_table.setColumnWidth(column, width)
        detail_layout.addWidget(self.price_table, 1)
        self.inspection_splitter.addWidget(self.detail_group)

        self.issue_group = QGroupBox("解析问题（错误/警告）")
        issue_layout = QVBoxLayout(self.issue_group)
        issue_filters = QHBoxLayout()
        issue_filters.addWidget(QLabel("问题级别"))
        self.severity_filter = QComboBox()
        self.severity_filter.setObjectName("severityFilter")
        self.severity_filter.addItem("全部问题", "ALL")
        self.severity_filter.addItem("仅错误", "ERROR")
        self.severity_filter.addItem("仅警告", "WARNING")
        issue_filters.addWidget(self.severity_filter)
        issue_filters.addStretch(1)
        issue_layout.addLayout(issue_filters)
        self.issue_empty_label = QLabel("解析后将在此显示问题")
        self.issue_empty_label.setObjectName("issueEmptyState")
        self.issue_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.issue_empty_label.setStyleSheet("color: #667085; padding: 24px;")
        issue_layout.addWidget(self.issue_empty_label, 1)
        self.issue_table = self._issue_table()
        issue_layout.addWidget(self.issue_table, 1)
        self.issue_table.hide()
        self.inspection_splitter.addWidget(self.issue_group)

        self.inspection_splitter.setStretchFactor(0, 7)
        self.inspection_splitter.setStretchFactor(1, 3)
        self.inspection_splitter.setSizes([820, 350])
        root.addWidget(self.inspection_splitter, 1)

        action_row = QHBoxLayout()
        self.reset_button = QPushButton("重新选择")
        self.reset_button.setObjectName("resetButton")
        self.confirm_button = QPushButton("确认入库")
        self.confirm_button.setObjectName("confirmButton")
        action_row.addStretch(1)
        action_row.addWidget(self.reset_button)
        action_row.addWidget(self.confirm_button)
        root.addLayout(action_row)

    def _connect_signals(self) -> None:
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        self.browse_button.clicked.connect(self._browse)
        self.parse_button.clicked.connect(self.start_parse)
        self.reset_button.clicked.connect(self._reset)
        self.confirm_button.clicked.connect(self._confirm_current)
        self.country_filter.currentIndexChanged.connect(self._apply_price_filters)
        self.tier_filter.currentIndexChanged.connect(self._apply_price_filters)
        self.severity_filter.currentIndexChanged.connect(self._populate_issue_table)

    def _issue_table(self) -> QTableWidget:
        headers = ["级别", "代码", "Sheet/文件", "行", "列", "原值", "说明"]
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((56, 62, 120, 50, 50, 90)):
            table.setColumnWidth(column, width)
        return table

    @Slot()
    def _browse(self) -> None:
        if self.source_selection_mode == "directory":
            selected = QFileDialog.getExistingDirectory(self, "选择 iOS 价格目录")
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "选择价格文件",
                filter="Excel 工作簿 (*.xlsx)",
            )
        if selected:
            self.set_source_path(selected)

    @Slot()
    def _on_channel_changed(self) -> None:
        self.browse_button.setText(
            "选择目录" if self.source_selection_mode == "directory" else "选择文件"
        )
        self._source_path = ""
        self.path_edit.clear()
        self._clear_preview()
        self.status_label.setText("渠道已切换，请重新选择来源。")
        self._update_action_state()

    @Slot(object, str)
    def _on_parse_succeeded(self, preview: ImportPreview, task_id: str) -> None:
        self.show_preview(preview, task_id)
        self.preview_loaded.emit(preview, task_id)

    @Slot(str)
    def _on_parse_failed(self, message: str) -> None:
        self._clear_preview()
        self._technical_error = message
        self.status_label.setText("解析发生异常，请确认来源可访问并符合对应渠道模板。")
        self._show_system_issue()
        self.parse_failed.emit(message)

    @Slot()
    def _on_parse_thread_finished(self) -> None:
        self._parse_thread = None
        self._parse_worker = None
        self._set_busy(False)

    @Slot()
    def _confirm_current(self) -> None:
        if self._preview is None or self._task_id is None:
            return
        try:
            assessment = self._confirmation_service.assess(
                self._preview,
                task_id=self._task_id,
            )
        except ConfirmationError as error:
            QMessageBox.critical(
                self,
                "不能确认入库",
                localized_confirmation_message(error.code, error.existing_version_id),
            )
            return

        accept_warnings = False
        if assessment.warning_count:
            answer = QMessageBox.question(
                self,
                "确认警告",
                f"当前预览有 {assessment.warning_count} 个警告。是否仍要继续入库？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            accept_warnings = True

        accept_coverage = False
        if assessment.coverage_reduced:
            answer = QMessageBox.question(
                self,
                "确认覆盖缩减",
                (
                    "新快照覆盖少于当前活动版本：\n"
                    f"国家 {assessment.previous_country_count} → {assessment.new_country_count}\n"
                    f"记录 {assessment.previous_record_count} → {assessment.new_record_count}\n"
                    "新版本不会合并旧价格。是否继续？"
                ),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            accept_coverage = True

        self._set_busy(True)
        self.status_label.setText("正在归档来源并写入正式版本…")
        try:
            result = self._confirmation_service.confirm(
                self._preview,
                task_id=self._task_id,
                accept_warnings=accept_warnings,
                accept_coverage_reduction=accept_coverage,
            )
        except ConfirmationError as error:
            message = localized_confirmation_message(error.code, error.existing_version_id)
            self.status_label.setText(f"确认失败，可重试：{message}")
            QMessageBox.critical(self, "确认失败", message)
        else:
            self.status_label.setText(
                f"入库成功：{result.version_id}，共 {result.record_count} 条价格。"
            )
            QMessageBox.information(self, "入库成功", f"已生成版本 {result.version_id}。")
            self._task_id = None
            self.confirmation_succeeded.emit(result)
        finally:
            self._set_busy(False)

    @Slot()
    def _reset(self) -> None:
        if self._parse_thread is not None:
            return
        self._source_path = ""
        self.path_edit.clear()
        self._clear_preview()
        self.status_label.setText("请选择渠道和来源文件。")
        self._update_action_state()

    def _clear_preview(self) -> None:
        self._preview = None
        self._task_id = None
        self._technical_error = None
        self.summary_label.setText("尚未解析")
        self.issue_table.setRowCount(0)
        self.issue_table.hide()
        self.issue_empty_label.setText("解析后将在此显示问题")
        self.issue_empty_label.show()
        self.price_model.set_records((), self._country_names)
        self.price_proxy.set_filters(country_code=None, usd_tier=None)
        self.country_filter.clear()
        self.tier_filter.clear()

    def _populate_summary(self) -> None:
        assert self._preview is not None
        stats = self._preview.statistics
        parts = [
            f"渠道：{CHANNEL_LABELS[self._preview.channel]}",
            f"摘要：{(self._preview.source_sha256 or '无')[:12]}…",
            f"国家：{stats.country_count}",
            f"币种：{stats.currency_count}",
            f"档位：{stats.tier_count}",
            f"接受价格：{stats.accepted_record_count}",
            f"错误：{stats.error_count}",
            f"警告：{stats.warning_count}",
        ]
        if self._preview.channel is Channel.IOS:
            parts.extend(
                [
                    f"手动调价：{stats.manual_adjustment_count}",
                    f"自动调价：{stats.automatic_adjustment_count}",
                ]
            )
        self.summary_label.setText("  ｜  ".join(parts))

    def _populate_filter_options(self) -> None:
        assert self._preview is not None
        self.country_filter.blockSignals(True)
        self.tier_filter.blockSignals(True)
        self.country_filter.clear()
        self.tier_filter.clear()
        self.country_filter.addItem("全部国家/地区", None)
        for code in sorted({record.country_code for record in self._preview.records}):
            name = self._country_names.get(code, code)
            self.country_filter.addItem(f"{code}｜{name}", code)
        self.tier_filter.addItem("全部档位", None)
        for tier in sorted({record.usd_tier for record in self._preview.records}):
            self.tier_filter.addItem(f"{tier:.2f}", str(tier))
        for combo in (self.country_filter, self.tier_filter):
            _reserve_combo_width(combo)
        self.country_filter.blockSignals(False)
        self.tier_filter.blockSignals(False)

    @Slot()
    def _apply_price_filters(self) -> None:
        country = self.country_filter.currentData()
        tier = self.tier_filter.currentData()
        self.price_proxy.set_filters(
            country_code=str(country) if country else None,
            usd_tier=Decimal(str(tier)) if tier else None,
        )

    @Slot()
    def _populate_issue_table(self) -> None:
        if self._preview is None:
            self.issue_table.setRowCount(0)
            self.issue_table.hide()
            self.issue_empty_label.setText("解析后将在此显示问题")
            self.issue_empty_label.show()
            return
        issues = self._preview.issues
        severity = self.severity_filter.currentData() or "ALL"
        visible = [
            issue for issue in issues if severity == "ALL" or issue.severity.value == severity
        ]
        if not visible:
            self.issue_table.setRowCount(0)
            self.issue_table.hide()
            self.issue_empty_label.setText(
                "本次解析无异常" if not issues else "当前筛选条件下无问题"
            )
            self.issue_empty_label.show()
            return
        self.issue_empty_label.hide()
        self.issue_table.show()
        self.issue_table.setRowCount(len(visible))
        for row, issue in enumerate(visible):
            values = (
                SEVERITY_LABELS[issue.severity.value],
                issue.code,
                issue.sheet_name or "",
                "" if issue.source_row is None else str(issue.source_row),
                issue.source_column or "",
                issue.source_value or "",
                localized_issue_message(issue),
            )
            self._set_issue_row(row, values)

    def _show_system_issue(self) -> None:
        self.issue_empty_label.hide()
        self.issue_table.show()
        self.issue_table.setRowCount(1)
        self._set_issue_row(
            0,
            (
                "错误",
                "SYS001",
                Path(self._source_path).name if self._source_path else "",
                "",
                "",
                "",
                "解析过程中发生未预期异常，请检查来源是否完整且符合渠道模板。",
            ),
        )
        if self._technical_error:
            self.issue_table.item(0, 6).setToolTip(self._technical_error)

    def _set_issue_row(self, row: int, values: tuple[str, ...]) -> None:
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in {0, 1, 3, 4}:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.issue_table.setItem(row, column, item)

    def _can_confirm(self) -> bool:
        return bool(
            self._preview is not None
            and self._task_id
            and self._preview.status is ImportTaskStatus.CHECKING
            and not self._preview.has_blocking_errors
            and self._preview.records
        )

    def _set_busy(self, busy: bool) -> None:
        self.channel_combo.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.reset_button.setEnabled(not busy)
        self.parse_button.setEnabled(not busy and bool(self._source_path))
        self.confirm_button.setEnabled(not busy and self._can_confirm())

    def _update_action_state(self) -> None:
        busy = self._parse_thread is not None
        self.parse_button.setEnabled(not busy and bool(self._source_path))
        self.confirm_button.setEnabled(not busy and self._can_confirm())

    def _new_task_id(self) -> str:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
        return f"TASK_{self.selected_channel.value}_{timestamp}_{uuid.uuid4().hex[:8]}"

    def _load_country_names(self) -> dict[str, str]:
        try:
            with database_session(self._database_path) as connection:
                rows = ReferenceDataRepository(connection).list_countries()
            return {str(row["country_code"]): str(row["name_cn"]) for row in rows}
        except Exception:  # noqa: BLE001 - missing names must not block the client
            return {}

    def _report_orphaned_archives(self) -> None:
        try:
            orphans = self._confirmation_service.list_orphaned_archives()
        except Exception:  # noqa: BLE001 - startup warning must not prevent opening the UI
            return
        if orphans:
            self.status_label.setText(
                f"发现 {len(orphans)} 个未关联归档，请人工检查；程序不会自动删除。"
            )


def _reserve_combo_width(combo: QComboBox) -> None:
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    text_width = max(
        combo.fontMetrics().horizontalAdvance(combo.itemText(index))
        for index in range(combo.count())
    )
    frame_width = combo.style().pixelMetric(
        QStyle.PixelMetric.PM_ComboBoxFrameWidth,
        None,
        combo,
    )
    arrow_width = combo.style().pixelMetric(
        QStyle.PixelMetric.PM_ScrollBarExtent,
        None,
        combo,
    )
    minimum_width = max(
        combo.sizeHint().width(),
        text_width + frame_width * 2 + arrow_width + 12,
    )
    combo.setMinimumWidth(minimum_width)
    combo.view().setMinimumWidth(minimum_width)
    combo.updateGeometry()
