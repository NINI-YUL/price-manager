"""Read-only P1-008 price library page."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import DATABASE_PATH
from src.models import (
    AdjustmentMode,
    Channel,
    ConfirmationResult,
    LibraryPrice,
    PriceLibraryCatalog,
    format_local_price,
    format_usd_tier,
)
from src.services import PriceLibraryError, PriceLibraryService
from src.ui.version_picker import VersionPicker

CHANNEL_LABELS = {
    Channel.GOOGLE: "Google Play",
    Channel.IOS: "iOS App Store",
    Channel.WEB: "三方网页",
}


class PriceDetailDialog(QDialog):
    def __init__(self, price: LibraryPrice, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("价格详情（只读）")
        self.setModal(True)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        adjustment = ""
        if price.adjustment_mode is AdjustmentMode.MANUAL:
            adjustment = "手动"
        elif price.adjustment_mode is AdjustmentMode.AUTOMATIC:
            adjustment = "自动"
        values = (
            ("渠道", CHANNEL_LABELS[price.channel]),
            ("版本", price.version_id),
            ("版本状态", price.version_status.value),
            ("国家/地区", f"{price.country.name_cn} / {price.country.name_en}"),
            ("国家编码", price.country.country_code),
            ("USD 档位", format_usd_tier(price.usd_tier)),
            ("本地价格", f"{format_local_price(price.local_price)} {price.currency}"),
            ("调价方式", adjustment),
            ("版本导入时间", price.version_import_time.isoformat()),
            ("价格创建时间", price.created_time.isoformat()),
        )
        for label, value in values:
            field = QLineEdit(value)
            field.setReadOnly(True)
            form.addRow(label, field)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 420)


class PriceLibraryPage(QWidget):
    navigate_to_import = Signal()

    def __init__(
        self,
        database_path=DATABASE_PATH,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = PriceLibraryService(database_path)
        self._catalog = PriceLibraryCatalog(countries=(), tiers=(), versions=())
        self._prices: tuple[LibraryPrice, ...] = ()
        self._cell_prices: dict[tuple[int, int], LibraryPrice] = {}
        self._updating = False
        self._query_error: str | None = None
        self.version_pickers: dict[Channel, VersionPicker] = {}
        self.channel_checks: dict[Channel, QCheckBox] = {}

        self._build_ui()
        self._connect_signals()
        self.refresh()

    @property
    def selected_versions(self) -> dict[Channel, str | None]:
        return {
            channel: picker.selected_version_id
            for channel, picker in self.version_pickers.items()
        }

    @property
    def current_view(self) -> str:
        return str(self.view_combo.currentData())

    def refresh(
        self,
        *,
        preferred_versions: dict[Channel, str | None] | None = None,
        select_active: bool = False,
    ) -> None:
        previous = preferred_versions or self.selected_versions
        self._updating = True
        self._query_error = None
        try:
            self._catalog = self._service.load_catalog()
            for channel, picker in self.version_pickers.items():
                picker.set_versions(
                    self._catalog.versions_for(channel),
                    preferred_version_id=previous.get(channel),
                    select_active=select_active,
                )
            self._populate_reference_options()
            self._load_selected_prices()
            self.status_label.setText("价格库已刷新。")
        except PriceLibraryError as error:
            self._query_error = f"查询失败：{error}"
            self._prices = ()
            self.status_label.setText(self._query_error)
        finally:
            self._updating = False
        self._render()

    @Slot(object)
    def handle_confirmation(self, result: ConfirmationResult) -> None:
        preferred = self.selected_versions
        preferred[result.channel] = result.version_id
        self.refresh(preferred_versions=preferred)

    def reset_filters(self) -> None:
        self._updating = True
        try:
            self.view_combo.setCurrentIndex(self.view_combo.findData("TIER"))
            for channel, picker in self.version_pickers.items():
                active = self._catalog.active_for(channel)
                picker.select_version(active.version_id if active is not None else None, emit=False)
            self._set_combo_data(self.tier_combo, "9.99")
            for checkbox in self.channel_checks.values():
                checkbox.setChecked(True)
            self.currency_combo.setCurrentIndex(0)
            self.search_edit.clear()
            self.show_all_check.setChecked(False)
        finally:
            self._updating = False
        self._load_selected_prices()
        self._update_view_controls()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("价格库")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        heading.addWidget(title)
        heading.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("libraryRefreshButton")
        heading.addWidget(self.refresh_button)
        root.addLayout(heading)

        version_group = QGroupBox("渠道版本（独立选择）")
        version_layout = QHBoxLayout(version_group)
        for channel in Channel:
            column = QVBoxLayout()
            label = QLabel(CHANNEL_LABELS[channel])
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            picker = VersionPicker()
            picker.setObjectName(f"{channel.value.lower()}VersionPicker")
            self.version_pickers[channel] = picker
            column.addWidget(label)
            column.addWidget(picker)
            version_layout.addLayout(column, 1)
        root.addWidget(version_group)

        filter_group = QGroupBox("查看与筛选")
        filter_layout = QHBoxLayout(filter_group)
        self.view_combo = QComboBox()
        self.view_combo.setObjectName("libraryView")
        self.view_combo.addItem("按档位查看", "TIER")
        self.view_combo.addItem("按国家/地区查看", "COUNTRY")
        self.tier_combo = QComboBox()
        self.tier_combo.setObjectName("tierSelector")
        self.country_combo = QComboBox()
        self.country_combo.setObjectName("countrySelector")
        self.country_combo.setMinimumWidth(220)
        self.currency_combo = QComboBox()
        self.currency_combo.setObjectName("currencyFilter")
        self.currency_combo.addItem("全部币种", None)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("librarySearch")
        self.search_edit.setClearButtonEnabled(True)
        self.show_all_check = QCheckBox("显示全部 191 个国家/地区")
        self.show_all_check.setObjectName("showAllCountries")
        self.reset_button = QPushButton("重置筛选")
        self.reset_button.setObjectName("libraryResetButton")

        filter_layout.addWidget(QLabel("视角"))
        filter_layout.addWidget(self.view_combo)
        self.selector_label = QLabel("USD 档位")
        filter_layout.addWidget(self.selector_label)
        filter_layout.addWidget(self.tier_combo)
        filter_layout.addWidget(self.country_combo)
        filter_layout.addWidget(QLabel("币种"))
        filter_layout.addWidget(self.currency_combo)
        filter_layout.addWidget(self.search_edit, 1)
        filter_layout.addWidget(self.show_all_check)
        filter_layout.addWidget(self.reset_button)
        root.addWidget(filter_group)

        channel_row = QHBoxLayout()
        channel_row.addWidget(QLabel("显示渠道"))
        for channel in Channel:
            checkbox = QCheckBox(CHANNEL_LABELS[channel])
            checkbox.setObjectName(f"{channel.value.lower()}ChannelCheck")
            checkbox.setChecked(True)
            self.channel_checks[channel] = checkbox
            channel_row.addWidget(checkbox)
        channel_row.addStretch(1)
        root.addLayout(channel_row)

        self.status_label = QLabel()
        self.status_label.setObjectName("libraryStatus")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.table = QTableWidget()
        self.table.setObjectName("libraryTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        self.import_button = QPushButton("返回价格导入")
        self.import_button.setObjectName("returnToImportButton")
        self.import_button.setVisible(False)
        root.addWidget(self.import_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self._update_view_controls()

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.refresh)
        self.reset_button.clicked.connect(self.reset_filters)
        self.view_combo.currentIndexChanged.connect(self._on_view_changed)
        self.tier_combo.currentIndexChanged.connect(self._render)
        self.country_combo.currentIndexChanged.connect(self._render)
        self.currency_combo.currentIndexChanged.connect(self._render)
        self.search_edit.textChanged.connect(self._render)
        self.show_all_check.toggled.connect(self._render)
        self.import_button.clicked.connect(self.navigate_to_import)
        self.table.cellClicked.connect(self._show_detail)
        for picker in self.version_pickers.values():
            picker.version_changed.connect(self._on_version_changed)
        for checkbox in self.channel_checks.values():
            checkbox.toggled.connect(self._on_channels_changed)

    @Slot()
    def _on_view_changed(self) -> None:
        if self._updating:
            return
        self._update_view_controls()
        self._render()

    @Slot()
    def _on_version_changed(self) -> None:
        if self._updating:
            return
        self._load_selected_prices()
        self._render()

    @Slot()
    def _on_channels_changed(self) -> None:
        if self._updating:
            return
        self._populate_currency_options()
        self._render()

    def _populate_reference_options(self) -> None:
        selected_tier = self.tier_combo.currentData()
        selected_country = self.country_combo.currentData()
        self.tier_combo.clear()
        for tier in self._catalog.tiers:
            self.tier_combo.addItem(f"USD {format_usd_tier(tier)}", str(tier))
        tier_values = {str(tier) for tier in self._catalog.tiers}
        target_tier = selected_tier if selected_tier in tier_values else "9.99"
        self._set_combo_data(self.tier_combo, target_tier)

        self.country_combo.clear()
        for country in self._catalog.countries:
            self.country_combo.addItem(
                f"{country.country_code}｜{country.name_cn}｜{country.name_en}",
                country.country_code,
            )
        country_codes = {country.country_code for country in self._catalog.countries}
        target_country = selected_country if selected_country in country_codes else "US"
        self._set_combo_data(self.country_combo, target_country)

    def _load_selected_prices(self) -> None:
        self._query_error = None
        try:
            self._prices = self._service.load_prices(self.selected_versions)
        except PriceLibraryError as error:
            self._query_error = f"查询失败：{error}"
            self._prices = ()
            self.status_label.setText(self._query_error)
        self._populate_currency_options()

    def _populate_currency_options(self) -> None:
        selected = self.currency_combo.currentData()
        enabled = self._enabled_channels()
        currencies = sorted(
            {price.currency for price in self._prices if price.channel in enabled}
        )
        self.currency_combo.blockSignals(True)
        self.currency_combo.clear()
        self.currency_combo.addItem("全部币种", None)
        for currency in currencies:
            self.currency_combo.addItem(currency, currency)
        index = self.currency_combo.findData(selected)
        self.currency_combo.setCurrentIndex(max(index, 0))
        self.currency_combo.blockSignals(False)

    @Slot()
    def _render(self) -> None:
        if self._updating:
            return
        self._cell_prices.clear()
        channels = self._enabled_channels()
        headers = (
            ["国家/地区", "国家编码"]
            if self.current_view == "TIER"
            else ["USD 档位"]
        ) + [CHANNEL_LABELS[channel] for channel in channels]
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        for column in range(len(headers)):
            header = self.table.horizontalHeaderItem(column)
            if header is not None:
                header.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._query_error is not None:
            self.table.setRowCount(0)
            self.import_button.setVisible(False)
            self.status_label.setText(self._query_error)
            return


        all_versions_missing = not any(self.selected_versions.values())
        self.import_button.setVisible(all_versions_missing)
        if all_versions_missing:
            self.table.setRowCount(0)
            self.status_label.setText("尚无正式价格，请先在价格导入页完成确认入库。")
            return

        currency = self.currency_combo.currentData()
        if self.current_view == "TIER":
            tier = self.tier_combo.currentData()
            if tier is None:
                self.table.setRowCount(0)
                return
            rows = self._service.tier_view(
                catalog=self._catalog,
                prices=self._prices,
                usd_tier=Decimal(str(tier)),
                channels=channels,
                currency=str(currency) if currency else None,
                search=self.search_edit.text(),
                show_all_countries=self.show_all_check.isChecked(),
            )
            self.table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                self._set_item(row_index, 0, row.country.name_cn, Qt.AlignmentFlag.AlignLeft)
                self._set_item(
                    row_index,
                    1,
                    row.country.country_code,
                    Qt.AlignmentFlag.AlignCenter,
                )
                for offset, channel in enumerate(channels, start=2):
                    self._set_price(row_index, offset, row.price_for(channel))
        else:
            country_code = self.country_combo.currentData()
            if country_code is None:
                self.table.setRowCount(0)
                return
            rows = self._service.country_view(
                catalog=self._catalog,
                prices=self._prices,
                country_code=str(country_code),
                channels=channels,
                currency=str(currency) if currency else None,
                search=self.search_edit.text(),
            )
            self.table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                self._set_item(
                    row_index,
                    0,
                    f"USD {format_usd_tier(row.usd_tier)}",
                    Qt.AlignmentFlag.AlignCenter,
                )
                for offset, channel in enumerate(channels, start=1):
                    self._set_price(row_index, offset, row.price_for(channel))

        if self.table.rowCount() == 0:
            self.status_label.setText("没有符合当前筛选的数据。")
        else:
            self.status_label.setText(f"共显示 {self.table.rowCount()} 行，只读查询。")

    def _set_price(self, row: int, column: int, price: LibraryPrice | None) -> None:
        if price is None:
            self._set_item(row, column, "—", Qt.AlignmentFlag.AlignCenter)
            return
        text = f"{format_local_price(price.local_price)} {price.currency}"
        if price.adjustment_mode is AdjustmentMode.MANUAL:
            text += "  [手动]"
        elif price.adjustment_mode is AdjustmentMode.AUTOMATIC:
            text += "  [自动]"
        self._set_item(row, column, text, Qt.AlignmentFlag.AlignCenter)
        self._cell_prices[(row, column)] = price

    def _set_item(
        self,
        row: int,
        column: int,
        text: str,
        alignment: Qt.AlignmentFlag,
    ) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(alignment)
        self.table.setItem(row, column, item)

    @Slot(int, int)
    def _show_detail(self, row: int, column: int) -> None:
        price = self._cell_prices.get((row, column))
        if price is not None:
            PriceDetailDialog(price, self).exec()

    def _update_view_controls(self) -> None:
        tier_view = self.current_view == "TIER"
        self.selector_label.setText("USD 档位" if tier_view else "国家/地区")
        self.tier_combo.setVisible(tier_view)
        self.country_combo.setVisible(not tier_view)
        self.show_all_check.setVisible(tier_view)
        self.search_edit.setPlaceholderText(
            "搜索国家编码、中文名或英文名" if tier_view else "搜索 USD 档位"
        )

    def _enabled_channels(self) -> tuple[Channel, ...]:
        return tuple(
            channel for channel in Channel if self.channel_checks[channel].isChecked()
        )

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
