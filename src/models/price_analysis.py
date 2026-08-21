"""Read-only Phase2 price-deviation analysis models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from src.models.import_preview import AdjustmentMode, Channel
from src.models.price_library import Country


class DeviationLevel(StrEnum):
    NORMAL = "NORMAL"
    ATTENTION = "ATTENTION"
    SIGNIFICANT = "SIGNIFICANT"


@dataclass(frozen=True, slots=True)
class PriceAnalysisRow:
    channel: Channel
    version_id: str
    country: Country
    usd_tier: Decimal
    currency: str
    actual_local_price: Decimal
    exchange_rate: Decimal | None
    theoretical_local_price: Decimal | None
    local_difference: Decimal | None
    deviation_percent: Decimal | None
    level: DeviationLevel | None
    adjustment_mode: AdjustmentMode | None


def format_exchange_rate(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return format(rounded, "f").rstrip("0").rstrip(".")


def format_theoretical_price(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded, ".2f")


def format_signed_difference(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:+.2f}"


def format_deviation_percent(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:+.2f}%"
