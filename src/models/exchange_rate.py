"""Domain models for Phase2 exchange-rate snapshots and refresh results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class RateFetchTrigger(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class RateRefreshStatus(StrEnum):
    CREATED = "CREATED"
    NOT_MODIFIED = "NOT_MODIFIED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ProviderRateBundle:
    provider: str
    base_currency: str
    updated_at: datetime
    next_update_at: datetime
    rates: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class ExchangeRateSnapshot:
    snapshot_id: str
    provider: str
    base_currency: str
    updated_at: datetime
    next_update_at: datetime
    fetched_at: datetime
    rates: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class ExchangeRateRefreshResult:
    status: RateRefreshStatus
    snapshot: ExchangeRateSnapshot | None
    message: str
