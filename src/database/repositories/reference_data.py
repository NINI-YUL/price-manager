"""Reference-data persistence used by schema smoke tests and P1-003."""

from __future__ import annotations

import sqlite3
from decimal import Decimal


class ReferenceDataRepository:
    """Insert and query the two Phase1 reference tables by natural key."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add_country(
        self,
        *,
        country_code: str,
        name_cn: str,
        name_en: str,
        default_currency: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO countries (country_code, name_cn, name_en, default_currency)
            VALUES (?, ?, ?, ?)
            """,
            (country_code, name_cn, name_en, default_currency),
        )

    def get_country(self, country_code: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM countries WHERE country_code = ?", (country_code,)
        ).fetchone()

    def add_price_tier(self, usd_price: Decimal) -> None:
        self._connection.execute(
            "INSERT INTO price_tiers (usd_price) VALUES (?)", (str(usd_price),)
        )

    def get_price_tier(self, usd_price: Decimal) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM price_tiers WHERE usd_price = ?", (str(usd_price),)
        ).fetchone()
