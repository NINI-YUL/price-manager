"""Read-only catalog, version selection, and view queries for P1-008."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal

from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, open_database
from src.database.repositories import PriceLibraryRepository
from src.models import (
    Channel,
    CountryViewRow,
    LibraryPrice,
    PriceLibraryCatalog,
    TierViewRow,
)


class PriceLibraryError(RuntimeError):
    """Raised when a read-only price library query cannot be completed safely."""


class PriceLibraryService:
    def __init__(self, database_path: DatabasePath = DATABASE_PATH) -> None:
        self._database_path = database_path

    def load_catalog(self) -> PriceLibraryCatalog:
        try:
            connection = open_database(self._database_path)
            connection.execute("PRAGMA query_only = ON")
            try:
                repository = PriceLibraryRepository(connection)
                return PriceLibraryCatalog(
                    countries=repository.list_countries(),
                    tiers=repository.list_tiers(),
                    versions=repository.list_versions(),
                )
            finally:
                connection.close()
        except Exception as error:
            raise PriceLibraryError(f"无法读取价格库基础数据：{error}") from error

    def load_prices(
        self,
        selections: Mapping[Channel, str | None],
    ) -> tuple[LibraryPrice, ...]:
        normalised = {channel: selections.get(channel) for channel in Channel}
        try:
            connection = open_database(self._database_path)
            connection.execute("PRAGMA query_only = ON")
            try:
                repository = PriceLibraryRepository(connection)
                for channel, version_id in normalised.items():
                    if version_id is None:
                        continue
                    version = repository.get_version(version_id)
                    if version is None:
                        raise PriceLibraryError(f"版本 {version_id} 已不存在，请刷新后重试")
                    if version.channel is not channel:
                        raise PriceLibraryError(
                            f"版本 {version_id} 不属于 {channel.value}，已阻止跨渠道串数"
                        )
                prices = repository.list_prices(normalised)
            finally:
                connection.close()
        except PriceLibraryError:
            raise
        except Exception as error:
            raise PriceLibraryError(f"无法读取正式价格：{error}") from error
        self._assert_unique_cells(prices)
        return prices

    def tier_view(
        self,
        *,
        catalog: PriceLibraryCatalog,
        prices: Iterable[LibraryPrice],
        usd_tier: Decimal,
        channels: Iterable[Channel] = tuple(Channel),
        currency: str | None = None,
        search: str = "",
        show_all_countries: bool = False,
    ) -> tuple[TierViewRow, ...]:
        enabled = frozenset(channels)
        relevant = tuple(
            price for price in prices if price.channel in enabled and price.usd_tier == usd_tier
        )
        codes = (
            {country.country_code for country in catalog.countries}
            if show_all_countries
            else {price.country.country_code for price in relevant}
        )
        countries = [country for country in catalog.countries if country.country_code in codes]
        needle = search.strip().casefold()
        if needle:
            countries = [
                country
                for country in countries
                if needle in country.country_code.casefold()
                or needle in country.name_cn.casefold()
                or needle in country.name_en.casefold()
            ]
        rows = tuple(
            TierViewRow(
                country=country,
                prices=tuple(
                    price
                    for price in relevant
                    if price.country.country_code == country.country_code
                ),
            )
            for country in countries
        )
        return self._filter_tier_currency(rows, currency)

    def country_view(
        self,
        *,
        catalog: PriceLibraryCatalog,
        prices: Iterable[LibraryPrice],
        country_code: str,
        channels: Iterable[Channel] = tuple(Channel),
        currency: str | None = None,
        search: str = "",
    ) -> tuple[CountryViewRow, ...]:
        enabled = frozenset(channels)
        relevant = tuple(
            price
            for price in prices
            if price.channel in enabled and price.country.country_code == country_code
        )
        needle = search.strip().casefold()
        tiers = [
            tier
            for tier in catalog.tiers
            if not needle
            or needle in format(tier, "f").casefold()
            or needle in format(tier, ".2f").casefold()
        ]
        rows = tuple(
            CountryViewRow(
                usd_tier=tier,
                prices=tuple(price for price in relevant if price.usd_tier == tier),
            )
            for tier in tiers
        )
        return self._filter_country_currency(rows, currency)

    @staticmethod
    def _filter_tier_currency(
        rows: tuple[TierViewRow, ...], currency: str | None
    ) -> tuple[TierViewRow, ...]:
        if currency is None:
            return rows
        return tuple(row for row in rows if any(price.currency == currency for price in row.prices))

    @staticmethod
    def _filter_country_currency(
        rows: tuple[CountryViewRow, ...], currency: str | None
    ) -> tuple[CountryViewRow, ...]:
        if currency is None:
            return rows
        return tuple(row for row in rows if any(price.currency == currency for price in row.prices))

    @staticmethod
    def _assert_unique_cells(prices: tuple[LibraryPrice, ...]) -> None:
        keys = [
            (price.channel, price.version_id, price.country.country_code, price.usd_tier)
            for price in prices
        ]
        if len(keys) != len(set(keys)):
            raise PriceLibraryError("同一版本的国家和档位存在多个价格，无法安全展示")
