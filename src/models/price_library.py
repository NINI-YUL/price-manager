"""Read-only domain models used by the P1-008 price library."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from src.models.import_preview import AdjustmentMode, Channel


class VersionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True, slots=True)
class Country:
    country_code: str
    name_cn: str
    name_en: str
    default_currency: str


@dataclass(frozen=True, slots=True)
class PriceVersion:
    version_id: str
    channel: Channel
    source_file: str
    import_time: datetime
    status: VersionStatus
    record_count: int


@dataclass(frozen=True, slots=True)
class LibraryPrice:
    channel: Channel
    version_id: str
    version_status: VersionStatus
    version_import_time: datetime
    country: Country
    usd_tier: Decimal
    currency: str
    local_price: Decimal
    adjustment_mode: AdjustmentMode | None
    created_time: datetime


@dataclass(frozen=True, slots=True)
class PriceLibraryCatalog:
    countries: tuple[Country, ...]
    tiers: tuple[Decimal, ...]
    versions: tuple[PriceVersion, ...]

    def versions_for(self, channel: Channel) -> tuple[PriceVersion, ...]:
        return tuple(version for version in self.versions if version.channel is channel)

    def active_for(self, channel: Channel) -> PriceVersion | None:
        return next(
            (
                version
                for version in self.versions
                if version.channel is channel and version.status is VersionStatus.ACTIVE
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class TierViewRow:
    country: Country
    prices: tuple[LibraryPrice, ...]

    def price_for(self, channel: Channel) -> LibraryPrice | None:
        return next((price for price in self.prices if price.channel is channel), None)


@dataclass(frozen=True, slots=True)
class CountryViewRow:
    usd_tier: Decimal
    prices: tuple[LibraryPrice, ...]

    def price_for(self, channel: Channel) -> LibraryPrice | None:
        return next((price for price in self.prices if price.channel is channel), None)


def format_local_price(value: Decimal) -> str:
    """Format a database decimal without exponent notation or extra rounding."""

    return format(value, "f")


def format_usd_tier(value: Decimal) -> str:
    """Format every approved USD tier with exactly two decimal places."""

    return format(value, ".2f")
