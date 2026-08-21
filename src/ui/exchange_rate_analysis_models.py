"""Efficient table model and filters for Phase2 exchange-rate analysis."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from src.models import (
    DeviationLevel,
    PriceAnalysisRow,
    format_deviation_percent,
    format_exchange_rate,
    format_local_price,
    format_signed_difference,
    format_theoretical_price,
    format_usd_tier,
    has_extra_price_precision,
)
from src.ui.price_library_page import CHANNEL_LABELS

_ROOT_INDEX = QModelIndex()

LEVEL_LABELS = {
    DeviationLevel.NORMAL: "正常",
    DeviationLevel.ATTENTION: "关注",
    DeviationLevel.SIGNIFICANT: "显著偏差",
}


class ExchangeRateAnalysisTableModel(QAbstractTableModel):
    """Expose immutable analysis rows without creating per-cell widgets."""

    HEADERS = (
        "渠道",
        "价格版本",
        "国家/地区",
        "币种",
        "USD 档位",
        "实际本地价",
        "理论本地价",
        "本地价差",
        "偏差",
        "方向",
        "等级",
        "API 汇率",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: tuple[PriceAnalysisRow, ...] = ()

    def set_rows(self, rows: tuple[PriceAnalysisRow, ...]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> PriceAnalysisRow:
        return self._rows[row]

    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return str(section + 1)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(row, column)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == 5
            and has_extra_price_precision(row.actual_local_price)
        ):
            return "原始价格超过两位小数，分析使用完整精度。"
        return None

    @staticmethod
    def _display_value(row: PriceAnalysisRow, column: int) -> str:
        if row.local_difference is None:
            direction = "—"
        elif row.local_difference > 0:
            direction = "偏高"
        elif row.local_difference < 0:
            direction = "偏低"
        else:
            direction = "持平"
        values = (
            CHANNEL_LABELS[row.channel],
            row.version_id,
            f"{row.country.country_code}｜{row.country.name_cn}",
            row.currency,
            format_usd_tier(row.usd_tier),
            format_local_price(row.actual_local_price),
            (
                format_theoretical_price(row.theoretical_local_price)
                if row.theoretical_local_price is not None
                else "—"
            ),
            (
                format_signed_difference(row.local_difference)
                if row.local_difference is not None
                else "—"
            ),
            (
                format_deviation_percent(row.deviation_percent)
                if row.deviation_percent is not None
                else "—"
            ),
            direction,
            LEVEL_LABELS.get(row.level, "缺少汇率"),
            format_exchange_rate(row.exchange_rate) if row.exchange_rate is not None else "—",
        )
        return values[column]


class ExchangeRateAnalysisFilterProxyModel(QSortFilterProxyModel):
    """Filter analysis records without rebuilding thousands of cells."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._filters: tuple[object, ...] = (None,) * 6
        self.setDynamicSortFilter(True)

    def set_filters(
        self,
        *,
        channel: str | None,
        country_code: str | None,
        usd_tier: str | None,
        currency: str | None,
        direction: str | None,
        level: str | None,
    ) -> None:
        filters = (channel, country_code, usd_tier, currency, direction, level)
        if filters == self._filters:
            return
        supports_incremental_change = hasattr(self, "beginFilterChange") and hasattr(
            self, "endFilterChange"
        )
        if supports_incremental_change:
            self.beginFilterChange()
        self._filters = filters
        if supports_incremental_change:
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, ExchangeRateAnalysisTableModel):
            return True
        row = model.row_at(source_row)
        channel, country_code, usd_tier, currency, direction, level = self._filters
        if channel is not None and row.channel.value != channel:
            return False
        if country_code is not None and row.country.country_code != country_code:
            return False
        if usd_tier is not None and str(row.usd_tier) != usd_tier:
            return False
        if currency is not None and row.currency != currency:
            return False
        if level is not None and (row.level is None or row.level.value != level):
            return False
        if direction == "POSITIVE":
            return row.local_difference is not None and row.local_difference > 0
        if direction == "NEGATIVE":
            return row.local_difference is not None and row.local_difference < 0
        return True
