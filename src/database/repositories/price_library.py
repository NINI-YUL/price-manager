"""Read-only database access for the P1-008 price library."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from src.models import (
    AdjustmentMode,
    Channel,
    Country,
    LibraryPrice,
    PriceVersion,
    VersionStatus,
)


class PriceLibraryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def list_countries(self) -> tuple[Country, ...]:
        rows = self._connection.execute(
            """
            SELECT country_code, name_cn, name_en, default_currency
            FROM countries
            ORDER BY country_code
            """
        )
        return tuple(
            Country(
                country_code=str(row["country_code"]),
                name_cn=str(row["name_cn"]),
                name_en=str(row["name_en"]),
                default_currency=str(row["default_currency"]),
            )
            for row in rows
        )

    def list_tiers(self) -> tuple[Decimal, ...]:
        rows = self._connection.execute(
            "SELECT usd_price FROM price_tiers ORDER BY usd_price"
        )
        return tuple(Decimal(str(row["usd_price"])) for row in rows)

    def list_versions(self) -> tuple[PriceVersion, ...]:
        rows = self._connection.execute(
            """
            SELECT version_id, channel, source_file, import_time, status, record_count
            FROM price_versions
            ORDER BY import_time DESC, version_id DESC
            """
        )
        return tuple(self._version(row) for row in rows)

    def get_version(self, version_id: str) -> PriceVersion | None:
        row = self._connection.execute(
            """
            SELECT version_id, channel, source_file, import_time, status, record_count
            FROM price_versions
            WHERE version_id = ?
            """,
            (version_id,),
        ).fetchone()
        return None if row is None else self._version(row)

    def list_prices(self, selections: dict[Channel, str | None]) -> tuple[LibraryPrice, ...]:
        prices: list[LibraryPrice] = []
        for channel in Channel:
            version_id = selections.get(channel)
            if version_id is None:
                continue
            rows = self._connection.execute(
                """
                SELECT p.channel, p.version_id, p.country_code, p.usd_tier,
                       p.currency, p.local_price, p.adjustment_mode, p.created_time,
                       c.name_cn, c.name_en, c.default_currency,
                       v.status AS version_status, v.import_time AS version_import_time
                FROM channel_prices AS p
                JOIN countries AS c ON c.country_code = p.country_code
                JOIN price_versions AS v
                  ON v.version_id = p.version_id AND v.channel = p.channel
                WHERE p.channel = ? AND p.version_id = ?
                ORDER BY p.country_code, p.usd_tier, p.currency
                """,
                (channel.value, version_id),
            )
            prices.extend(self._price(row) for row in rows)
        return tuple(prices)

    @staticmethod
    def _version(row: sqlite3.Row) -> PriceVersion:
        return PriceVersion(
            version_id=str(row["version_id"]),
            channel=Channel(str(row["channel"])),
            source_file=str(row["source_file"]),
            import_time=_parse_time(str(row["import_time"])),
            status=VersionStatus(str(row["status"])),
            record_count=int(row["record_count"]),
        )

    @staticmethod
    def _price(row: sqlite3.Row) -> LibraryPrice:
        adjustment = row["adjustment_mode"]
        return LibraryPrice(
            channel=Channel(str(row["channel"])),
            version_id=str(row["version_id"]),
            version_status=VersionStatus(str(row["version_status"])),
            version_import_time=_parse_time(str(row["version_import_time"])),
            country=Country(
                country_code=str(row["country_code"]),
                name_cn=str(row["name_cn"]),
                name_en=str(row["name_en"]),
                default_currency=str(row["default_currency"]),
            ),
            usd_tier=Decimal(str(row["usd_tier"])),
            currency=str(row["currency"]),
            local_price=Decimal(str(row["local_price"])),
            adjustment_mode=(AdjustmentMode(str(adjustment)) if adjustment is not None else None),
            created_time=_parse_time(str(row["created_time"])),
        )


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid ISO 8601 database timestamp: {value!r}") from error
