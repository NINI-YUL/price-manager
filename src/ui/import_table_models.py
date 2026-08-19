"""Efficient, localized table models for the import inspection page."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from src.models import (
    AdjustmentMode,
    ImportIssue,
    StandardPrice,
    format_local_price,
    format_usd_tier,
    has_extra_price_precision,
)

_ROOT_INDEX = QModelIndex()

ADJUSTMENT_LABELS = {
    AdjustmentMode.MANUAL: "手动调价",
    AdjustmentMode.AUTOMATIC: "自动调价",
}

SEVERITY_LABELS = {
    "ERROR": "错误",
    "WARNING": "警告",
}

ISSUE_MESSAGES_CN = {
    "G001": "无法读取 Google 价格文件，请确认文件存在、未被占用且为 .xlsx 格式。",
    "G002": "工作表缺少或重复必需表头，请检查 Google 模板结构。",
    "G003": "商品 ID 无法识别 USD 档位，请检查商品 ID 后缀或档位配置。",
    "G004": "国家码为空、格式错误或未配置。",
    "G005": "币种为空、格式错误或不受支持。",
    "G006": "本地价格为空、不是有效数字或不大于 0。",
    "G007": "同一国家、档位和币种出现不同价格，无法确定唯一值。",
    "G008": "文件没有完整包含全部 14 个档位。",
    "G101": "国家值已通过唯一别名自动匹配，请核对匹配结果。",
    "G102": "发现完全相同的重复记录，系统已保留一条。",
    "I001": "iOS 目录或 CSV 无法读取，请检查目录权限、文件编码和文件大小。",
    "I002": "档位目录、CSV 文件名或表头结构不符合要求。",
    "I003": "国家/地区名称无法匹配基础国家表。",
    "I004": "币种为空、格式错误或不受支持。",
    "I005": "本地价格为空、不是有效数字或不大于 0。",
    "I006": "调价方式只能填写 N 或 Y。",
    "I007": "同一国家和档位出现不同价格或多个币种。",
    "I008": "iOS 目录没有完整包含全部 14 个档位。",
    "I102": "发现完全相同的重复记录，系统已保留一条。",
    "W001": "文件不存在、类型不支持、文件过大或无法读取。",
    "W002": "无法找到唯一有效工作表，或两行表头结构不符合要求。",
    "W003": "国家/地区名称未知、存在歧义或未配置。",
    "W004": "币种为空、格式错误或不受支持。",
    "W005": "USD 档位为空、非法、不在 14 个档位中，或公式没有缓存值。",
    "W006": "本地价格为空、非法、不大于 0，或公式没有缓存值。",
    "W007": "国家列重复，或同一国家、档位出现冲突价格或多个币种。",
    "W008": "文件没有完整覆盖全部 14 个档位。",
    "W102": "发现完全相同的重复记录，系统已保留一条。",
}

CONFIRMATION_MESSAGES_CN = {
    "C001": "当前导入任务状态已变化，请重新解析后再确认。",
    "C002": "解析结果存在阻断错误、重复数据或没有可入库价格。",
    "C003": "当前存在警告，需要明确确认后才能入库。",
    "C004": "新版本覆盖范围减少，需要明确确认后才能入库。",
    "C005": "该来源文件已经生成过正式版本。",
    "C006": "解析后来源文件被移动、删除或修改，请重新解析。",
    "C007": "来源文件归档或归档校验失败，请检查目录权限和磁盘空间。",
    "C008": "数据库写入失败，所有变化已回滚，可以重试。",
}


def localized_issue_message(issue: ImportIssue) -> str:
    """Return the approved Chinese explanation while retaining the stable issue code."""

    return ISSUE_MESSAGES_CN.get(issue.code, "解析数据不符合要求，请核对来源内容。")


def localized_confirmation_message(code: str, existing_version_id: str | None = None) -> str:
    """Return a user-facing Chinese confirmation error."""

    message = CONFIRMATION_MESSAGES_CN.get(code, "确认入库失败，所有变化已回滚。")
    if code == "C005" and existing_version_id:
        message += f" 已有版本：{existing_version_id}。"
    return f"{code}：{message}"


class PricePreviewTableModel(QAbstractTableModel):
    """Lazy table model; filtering never recreates thousands of cell widgets."""

    HEADERS = (
        "国家码",
        "国家/地区",
        "USD档位",
        "币种",
        "本地价格",
        "商品ID",
        "调价方式",
        "Sheet/文件",
        "行",
        "列",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._records: tuple[StandardPrice, ...] = ()
        self._country_names: dict[str, str] = {}

    def set_records(
        self,
        records: tuple[StandardPrice, ...],
        country_names: dict[str, str],
    ) -> None:
        self.beginResetModel()
        self._records = records
        self._country_names = dict(country_names)
        self.endResetModel()

    def record_at(self, row: int) -> StandardPrice:
        return self._records[row]

    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return str(section + 1)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._records):
            return None
        record = self._records[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(record, column)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                if column in {1, 5, 7}
                else Qt.AlignmentFlag.AlignCenter
            )
        if (
            role == Qt.ItemDataRole.ToolTipRole
            and column == 4
            and has_extra_price_precision(record.local_price)
        ):
            return "价格超过两位小数，界面已保留原值，请核对。"
        return None

    def _display_value(self, record: StandardPrice, column: int) -> str:
        values = (
            record.country_code,
            self._country_names.get(record.country_code, record.country_code),
            format_usd_tier(record.usd_tier),
            record.currency,
            format_local_price(record.local_price),
            record.product_id or "",
            ADJUSTMENT_LABELS.get(record.adjustment_mode, ""),
            record.source_sheet,
            str(record.source_row),
            record.source_column,
        )
        return values[column]


class PricePreviewFilterProxyModel(QSortFilterProxyModel):
    """Country/tier filtering over immutable preview records."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._country_code: str | None = None
        self._usd_tier: Decimal | None = None
        self.setDynamicSortFilter(True)

    def set_filters(
        self,
        *,
        country_code: str | None,
        usd_tier: Decimal | None,
    ) -> None:
        if self._country_code == country_code and self._usd_tier == usd_tier:
            return
        supports_incremental_change = hasattr(self, "beginFilterChange") and hasattr(
            self, "endFilterChange"
        )
        if supports_incremental_change:
            self.beginFilterChange()
        self._country_code = country_code
        self._usd_tier = usd_tier
        if supports_incremental_change:
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:  # PySide6 6.7/6.8 compatibility
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, PricePreviewTableModel):
            return True
        record = model.record_at(source_row)
        return (self._country_code is None or record.country_code == self._country_code) and (
            self._usd_tier is None or record.usd_tier == self._usd_tier
        )
