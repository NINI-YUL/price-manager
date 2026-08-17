"""Immutable preview models shared by channel adapters and services."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Channel(StrEnum):
    GOOGLE = "GOOGLE"
    IOS = "IOS"
    WEB = "WEB"


class IssueSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class ImportTaskStatus(StrEnum):
    PROCESSING = "PROCESSING"
    CHECKING = "CHECKING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AdjustmentMode(StrEnum):
    MANUAL = "MANUAL"
    AUTOMATIC = "AUTOMATIC"


@dataclass(frozen=True, slots=True)
class ImportIssue:
    code: str
    severity: IssueSeverity
    message: str
    sheet_name: str | None = None
    source_row: int | None = None
    source_column: str | None = None
    source_value: str | None = None


@dataclass(frozen=True, slots=True)
class StandardPrice:
    channel: Channel
    country_code: str
    usd_tier: Decimal
    currency: str
    local_price: Decimal
    product_id: str | None
    source_sheet: str
    source_row: int
    source_column: str
    adjustment_mode: AdjustmentMode | None = None

    @property
    def natural_key(self) -> tuple[Channel, str, Decimal, str]:
        return self.channel, self.country_code, self.usd_tier, self.currency


@dataclass(frozen=True, slots=True)
class ImportStatistics:
    source_row_count: int = 0
    product_count: int = 0
    price_cell_count: int = 0
    accepted_record_count: int = 0
    country_count: int = 0
    currency_count: int = 0
    tier_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    manual_adjustment_count: int = 0
    automatic_adjustment_count: int = 0


@dataclass(frozen=True, slots=True)
class ImportPreview:
    channel: Channel
    source_path: str
    source_sha256: str | None
    selected_sheet: str | None
    status: ImportTaskStatus
    records: tuple[StandardPrice, ...]
    issues: tuple[ImportIssue, ...]
    statistics: ImportStatistics

    @property
    def has_blocking_errors(self) -> bool:
        return any(issue.severity is IssueSeverity.ERROR for issue in self.issues)
