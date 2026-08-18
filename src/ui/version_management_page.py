"""P1-009 version management page and confirmation dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import DATABASE_PATH, RUNTIME_PATHS
from src.models import (
    ArchiveInspection,
    ArchiveStatus,
    Channel,
    VersionActivationAssessment,
    VersionDetail,
    VersionManagementError,
    VersionStatus,
)
from src.services import VersionManagementService

CHANNEL_LABELS = {
    Channel.GOOGLE: "Google Play",
    Channel.IOS: "iOS App Store",
    Channel.WEB: "三方网页",
}

ARCHIVE_LABELS = {
    ArchiveStatus.COMPLETE: "完整",
    ArchiveStatus.MISSING: "归档缺失",
    ArchiveStatus.DIGEST_MISMATCH: "摘要不一致",
    ArchiveStatus.DAMAGED: "归档损坏",
    ArchiveStatus.UNREADABLE: "无法校验",
}

REASON_LABELS = {
    "IMPORT_CONFIRMATION": "导入确认",
    "MANUAL_ACTIVATION": "手动启用",
    "MIGRATION_BASELINE": "升级基线",
}


class VersionDetailDialog(QDialog):
    view_in_library = Signal(object, str)
    request_activation = Signal(str)
    open_archive = Signal(str)
    check_archive = Signal(str)

    def __init__(self, detail: VersionDetail, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.detail = detail
        self.setWindowTitle("版本详情")
        self.setModal(True)
        self.resize(760, 560)

        root = QVBoxLayout(self)
        title = QLabel(detail.summary.version_id)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)
        root.addWidget(
            QLabel(
                f"{CHANNEL_LABELS[detail.summary.channel]} · "
                f"{detail.summary.status.value}"
            )
        )

        tabs = QTabWidget()
        tabs.setObjectName("versionDetailTabs")
        tabs.addTab(self._overview_tab(), "版本概览")
        tabs.addTab(self._events_tab(), "状态记录")
        root.addWidget(tabs, 1)

        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        actions.rejected.connect(self.reject)
        self.library_button = actions.addButton(
            "在价格库查看",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.library_button.setObjectName("viewVersionInLibraryButton")
        self.library_button.clicked.connect(
            lambda: self.view_in_library.emit(
                detail.summary.channel,
                detail.summary.version_id,
            )
        )
        self.activate_button = actions.addButton(
            "重新启用此版本",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.activate_button.setObjectName("activateVersionFromDetailButton")
        self.activate_button.setVisible(detail.summary.status is VersionStatus.ARCHIVED)
        self.activate_button.clicked.connect(
            lambda: self.request_activation.emit(detail.summary.version_id)
        )
        root.addWidget(actions)

    def _overview_tab(self) -> QWidget:
        summary = self.detail.summary
        archive = self.detail.archive
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        values = (
            ("渠道", CHANNEL_LABELS[summary.channel]),
            ("状态", summary.status.value),
            ("导入时间", summary.import_time.isoformat()),
            ("记录数", f"{summary.record_count:,}"),
            ("国家/地区数", str(summary.country_count)),
            ("币种数", str(summary.currency_count)),
            ("USD 档位覆盖", f"{summary.tier_count} / 14"),
            ("关联导入任务", summary.task_id or "—"),
            ("导入任务状态", summary.task_status or "—"),
            ("来源文件", summary.source_file),
            ("来源摘要", summary.source_sha256 or "—"),
        )
        for label, value in values:
            field = QLineEdit(value)
            field.setReadOnly(True)
            form.addRow(label, field)
        layout.addLayout(form)

        archive_group = QGroupBox("来源归档")
        archive_layout = QVBoxLayout(archive_group)
        self.archive_status_label = QLabel(_archive_text(archive))
        self.archive_status_label.setWordWrap(True)
        archive_layout.addWidget(self.archive_status_label)
        archive_layout.addWidget(QLabel(str(archive.path)))
        archive_actions = QHBoxLayout()
        open_button = QPushButton("打开归档文件夹")
        open_button.setObjectName("openArchiveFolderButton")
        open_button.clicked.connect(
            lambda: self.open_archive.emit(summary.version_id)
        )
        check_button = QPushButton("校验归档")
        check_button.setObjectName("checkArchiveButton")
        check_button.clicked.connect(
            lambda: self.check_archive.emit(summary.version_id)
        )
        archive_actions.addWidget(open_button)
        archive_actions.addWidget(check_button)
        archive_actions.addStretch(1)
        archive_layout.addLayout(archive_actions)
        layout.addWidget(archive_group)
        return tab

    def _events_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        table = QTableWidget()
        table.setObjectName("versionEventTable")
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(
            ["时间", "原因", "状态变化", "替换版本", "备注", "操作人"]
        )
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(self.detail.events))
        for row, event in enumerate(self.detail.events):
            change = (
                f"{event.from_status.value if event.from_status is not None else '—'}"
                f" → {event.to_status.value}"
            )
            values = (
                event.created_time.isoformat(),
                REASON_LABELS.get(event.reason.value, event.reason.value),
                change,
                event.replaced_version_id or "—",
                event.note or "—",
                event.actor,
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(table)
        if not self.detail.events:
            layout.addWidget(QLabel("该版本没有可用的状态记录。"))
        return tab


class VersionActivationDialog(QDialog):
    def __init__(
        self,
        assessment: VersionActivationAssessment,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.assessment = assessment
        self.setWindowTitle("确认重新启用历史版本")
        self.setModal(True)
        self.resize(660, 450)

        root = QVBoxLayout(self)
        heading = QLabel("确认重新启用历史版本")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(heading)
        root.addWidget(QLabel("当前版本将自动归档，所有价格快照保持不变。"))

        compare = QGridLayout()
        compare.addWidget(QLabel("当前 ACTIVE"), 0, 0)
        compare.addWidget(QLabel("目标历史版本"), 0, 1)
        compare.addWidget(
            self._version_panel(assessment.current, empty_text="当前无生效版本"),
            1,
            0,
        )
        compare.addWidget(self._version_panel(assessment.target), 1, 1)
        root.addLayout(compare)

        warning_text = self._warning_text()
        warning = QLabel(warning_text)
        warning.setObjectName("activationWarning")
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "padding: 10px; background: #fff2d8; color: #7a4b0d; border-radius: 6px;"
        )
        root.addWidget(warning)

        self.note_edit = QLineEdit()
        self.note_edit.setObjectName("activationNote")
        self.note_edit.setMaxLength(200)
        self.note_edit.setPlaceholderText("操作备注（选填，最多 200 字）")
        root.addWidget(self.note_edit)

        self.acknowledge_check = QCheckBox(
            "我已核对目标版本，并了解当前 ACTIVE 版本将被归档"
        )
        self.acknowledge_check.setObjectName("activationAcknowledge")
        root.addWidget(self.acknowledge_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认启用")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "confirmActivationButton"
        )
        self.acknowledge_check.toggled.connect(
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @property
    def note(self) -> str:
        return self.note_edit.text().strip()

    @staticmethod
    def _version_panel(summary, *, empty_text: str = "") -> QGroupBox:
        panel = QGroupBox()
        layout = QFormLayout(panel)
        if summary is None:
            layout.addRow(QLabel(empty_text))
            return panel
        values = (
            ("版本", summary.version_id),
            ("导入日期", summary.import_time.date().isoformat()),
            ("记录数", f"{summary.record_count:,}"),
            ("国家/地区", str(summary.country_count)),
            ("币种", str(summary.currency_count)),
            ("档位", f"{summary.tier_count} / 14"),
        )
        for label, value in values:
            layout.addRow(label, QLabel(value))
        return panel

    def _warning_text(self) -> str:
        target = self.assessment.target
        current = self.assessment.current
        parts: list[str] = []
        if current is not None:
            record_delta = target.record_count - current.record_count
            country_delta = target.country_count - current.country_count
            parts.append(
                f"记录数变化 {record_delta:+,}，国家/地区变化 {country_delta:+}。"
            )
            if target.tier_count < current.tier_count:
                parts.append("目标版本的档位覆盖更少。")
        if self.assessment.archive.has_issue:
            parts.append(
                f"归档状态：{ARCHIVE_LABELS[self.assessment.archive.status]}；"
                "数据库价格快照仍可启用。"
            )
        if not parts:
            parts.append("目标版本覆盖未低于当前版本。")
        return "".join(parts)


class VersionManagementPage(QWidget):
    view_in_library = Signal(object, str)
    version_activated = Signal(object)

    def __init__(
        self,
        database_path=DATABASE_PATH,
        *,
        archives_root=RUNTIME_PATHS.archives,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = VersionManagementService(
            database_path,
            archives_root=archives_root,
        )
        self._versions: tuple[VersionDetail, ...] = ()
        self._row_versions: dict[int, VersionDetail] = {}
        self._build_ui()
        self._connect_signals()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("版本管理")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        heading.addWidget(title)
        heading.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("versionRefreshButton")
        heading.addWidget(self.refresh_button)
        root.addLayout(heading)
        root.addWidget(QLabel("查看正式版本、来源归档和状态变更记录"))

        summary = QHBoxLayout()
        self.active_count_box, self.active_count_label = self._summary_box(
            "当前生效版本", "0"
        )
        self.archived_count_box, self.archived_count_label = self._summary_box(
            "历史归档版本", "0"
        )
        self.issue_count_box, self.issue_count_label = self._summary_box(
            "归档异常", "0"
        )
        summary.addWidget(self.active_count_box)
        summary.addWidget(self.archived_count_box)
        summary.addWidget(self.issue_count_box)
        root.addLayout(summary)

        filters = QGroupBox("版本筛选")
        filter_layout = QHBoxLayout(filters)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("versionSearch")
        self.search_edit.setPlaceholderText("搜索版本号或来源文件")
        self.search_edit.setClearButtonEnabled(True)
        self.channel_combo = QComboBox()
        self.channel_combo.setObjectName("versionChannelFilter")
        self.channel_combo.addItem("全部渠道", None)
        for channel in Channel:
            self.channel_combo.addItem(CHANNEL_LABELS[channel], channel)
        self.status_combo = QComboBox()
        self.status_combo.setObjectName("versionStatusFilter")
        self.status_combo.addItem("全部状态", None)
        self.status_combo.addItem("ACTIVE", VersionStatus.ACTIVE)
        self.status_combo.addItem("ARCHIVED", VersionStatus.ARCHIVED)
        self.year_combo = QComboBox()
        self.year_combo.setObjectName("versionYearFilter")
        self.month_combo = QComboBox()
        self.month_combo.setObjectName("versionMonthFilter")
        self.reset_button = QPushButton("重置")
        self.reset_button.setObjectName("versionResetButton")
        filter_layout.addWidget(self.search_edit, 2)
        filter_layout.addWidget(self.channel_combo)
        filter_layout.addWidget(self.status_combo)
        filter_layout.addWidget(self.year_combo)
        filter_layout.addWidget(self.month_combo)
        filter_layout.addWidget(self.reset_button)
        root.addWidget(filters)

        self.status_label = QLabel()
        self.status_label.setObjectName("versionStatusLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.table = QTableWidget()
        self.table.setObjectName("versionTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(
            [
                "版本号",
                "渠道",
                "状态",
                "导入时间",
                "来源文件",
                "记录数",
                "国家/币种",
                "归档状态",
                "操作",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.refresh)
        self.reset_button.clicked.connect(self.reset_filters)
        self.search_edit.textChanged.connect(self._render)
        self.channel_combo.currentIndexChanged.connect(self._render)
        self.status_combo.currentIndexChanged.connect(self._render)
        self.year_combo.currentIndexChanged.connect(self._render)
        self.month_combo.currentIndexChanged.connect(self._render)
        self.table.cellDoubleClicked.connect(self._open_row_detail)

    @Slot()
    def refresh(self) -> None:
        try:
            self._versions = self._service.list_versions()
            self._populate_period_filters()
            self._render()
        except VersionManagementError as error:
            self._versions = ()
            self.table.setRowCount(0)
            self.status_label.setText(f"版本列表读取失败：{error}")

    @Slot()
    def reset_filters(self) -> None:
        self.search_edit.clear()
        self.channel_combo.setCurrentIndex(0)
        self.status_combo.setCurrentIndex(0)
        self.year_combo.setCurrentIndex(0)
        self.month_combo.setCurrentIndex(0)
        self._render()

    def _populate_period_filters(self) -> None:
        selected_year = self.year_combo.currentData()
        selected_month = self.month_combo.currentData()
        years = sorted(
            {detail.summary.import_time.year for detail in self._versions},
            reverse=True,
        )
        months = sorted(
            {detail.summary.import_time.month for detail in self._versions},
            reverse=True,
        )
        self.year_combo.blockSignals(True)
        self.month_combo.blockSignals(True)
        self.year_combo.clear()
        self.month_combo.clear()
        self.year_combo.addItem("全部年份", None)
        self.month_combo.addItem("全部月份", None)
        for year in years:
            self.year_combo.addItem(f"{year} 年", year)
        for month in months:
            self.month_combo.addItem(f"{month:02d} 月", month)
        self._set_combo_data(self.year_combo, selected_year)
        self._set_combo_data(self.month_combo, selected_month)
        self.year_combo.blockSignals(False)
        self.month_combo.blockSignals(False)

    @Slot()
    def _render(self) -> None:
        filtered = self._filtered_versions()
        self._row_versions.clear()
        self.table.clearContents()
        self.table.setRowCount(len(filtered))
        for row, detail in enumerate(filtered):
            self._row_versions[row] = detail
            summary = detail.summary
            values = (
                summary.version_id,
                CHANNEL_LABELS[summary.channel],
                summary.status.value,
                summary.import_time.isoformat(sep=" ", timespec="minutes"),
                summary.source_file,
                f"{summary.record_count:,}",
                f"{summary.country_count} / {summary.currency_count}",
                ARCHIVE_LABELS[detail.archive.status],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {2, 5, 6, 7}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
            self.table.setCellWidget(row, 8, self._row_actions(detail))
        active = sum(
            detail.summary.status is VersionStatus.ACTIVE for detail in self._versions
        )
        archived = sum(
            detail.summary.status is VersionStatus.ARCHIVED for detail in self._versions
        )
        issues = sum(detail.archive.has_issue for detail in self._versions)
        self.active_count_label.setText(str(active))
        self.archived_count_label.setText(str(archived))
        self.issue_count_label.setText(str(issues))
        self.status_label.setText(
            "尚无正式版本，请先完成价格导入确认。"
            if not self._versions
            else f"共显示 {len(filtered)} 个版本；历史价格快照只读保留。"
        )

    def _filtered_versions(self) -> tuple[VersionDetail, ...]:
        query = self.search_edit.text().strip().casefold()
        channel = self.channel_combo.currentData()
        channel_value = str(channel) if channel is not None else None
        status = self.status_combo.currentData()
        status_value = str(status) if status is not None else None
        year = self.year_combo.currentData()
        month = self.month_combo.currentData()
        return tuple(
            detail
            for detail in self._versions
            if (
                not query
                or query in detail.summary.version_id.casefold()
                or query in detail.summary.source_file.casefold()
            )
            and (
                channel_value is None or detail.summary.channel.value == channel_value
            )
            and (
                status_value is None or detail.summary.status.value == status_value
            )
            and (year is None or detail.summary.import_time.year == year)
            and (month is None or detail.summary.import_time.month == month)
        )

    def _row_actions(self, detail: VersionDetail) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        detail_button = QPushButton("详情")
        detail_button.setObjectName(f"detail_{detail.summary.version_id}")
        detail_button.clicked.connect(
            lambda _checked=False, version_id=detail.summary.version_id: self._show_detail(
                version_id
            )
        )
        layout.addWidget(detail_button)
        if detail.summary.status is VersionStatus.ARCHIVED:
            activate_button = QPushButton("重新启用")
            activate_button.setObjectName(f"activate_{detail.summary.version_id}")
            activate_button.clicked.connect(
                lambda _checked=False, version_id=detail.summary.version_id: self._activate(
                    version_id
                )
            )
            layout.addWidget(activate_button)
        layout.addStretch(1)
        return widget

    @Slot(int, int)
    def _open_row_detail(self, row: int, _column: int) -> None:
        detail = self._row_versions.get(row)
        if detail is not None:
            self._show_detail(detail.summary.version_id)

    def _show_detail(self, version_id: str) -> None:
        try:
            detail = self._service.get_detail(version_id)
        except VersionManagementError as error:
            QMessageBox.critical(self, "版本详情", str(error))
            return
        dialog = VersionDetailDialog(detail, self)
        dialog.view_in_library.connect(self._view_in_library)
        dialog.request_activation.connect(self._activate_from_detail)
        dialog.open_archive.connect(self._open_archive)
        dialog.check_archive.connect(self._check_archive)
        dialog.exec()

    @Slot(object, str)
    def _view_in_library(self, channel: Channel, version_id: str) -> None:
        self.view_in_library.emit(channel, version_id)

    @Slot(str)
    def _activate_from_detail(self, version_id: str) -> None:
        sender = self.sender()
        if isinstance(sender, QDialog):
            sender.accept()
        self._activate(version_id)

    def _activate(self, version_id: str) -> None:
        try:
            assessment = self._service.assess_activation(version_id)
            dialog = VersionActivationDialog(assessment, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            result = self._service.activate(version_id, note=dialog.note)
        except VersionManagementError as error:
            QMessageBox.critical(self, "版本切换失败", str(error))
            return
        self.refresh()
        self.version_activated.emit(result)
        QMessageBox.information(
            self,
            "版本已启用",
            f"{result.activated_version_id} 已成为 "
            f"{CHANNEL_LABELS[result.channel]} 的 ACTIVE 版本。",
        )

    @Slot(str)
    def _open_archive(self, version_id: str) -> None:
        try:
            folder = self._service.archive_folder(version_id)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise VersionManagementError("V006", "系统未能打开归档目录")
        except VersionManagementError as error:
            QMessageBox.warning(self, "打开归档目录", str(error))

    @Slot(str)
    def _check_archive(self, version_id: str) -> None:
        try:
            detail = self._service.get_detail(version_id)
            self.refresh()
            QMessageBox.information(
                self,
                "归档校验",
                _archive_text(detail.archive),
            )
        except VersionManagementError as error:
            QMessageBox.warning(self, "归档校验", str(error))

    @staticmethod
    def _summary_box(title: str, value: str) -> tuple[QGroupBox, QLabel]:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        label = QLabel(value)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(label)
        return box, label

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(index, 0))


def _archive_text(archive: ArchiveInspection) -> str:
    size = f"{archive.size_bytes:,} 字节" if archive.size_bytes is not None else "—"
    modified = (
        archive.modified_time.isoformat(sep=" ", timespec="seconds")
        if archive.modified_time is not None
        else "—"
    )
    return (
        f"{ARCHIVE_LABELS[archive.status]}；{archive.detail}；"
        f"大小：{size}；修改时间：{modified}"
    )
