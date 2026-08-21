"""Read-only deterministic Phase2 price-deviation analysis."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, open_database
from src.database.repositories import ExchangeRateRepository
from src.models import (
    Channel,
    DeviationLevel,
    PriceAnalysisRow,
)
from src.services.price_library import PriceLibraryService


class PriceAnalysisError(RuntimeError):
    pass


class PriceAnalysisService:
    def __init__(self, database_path: DatabasePath = DATABASE_PATH) -> None:
        self._database_path = database_path
        self._price_library = PriceLibraryService(database_path)

    def analyze(
        self,
        *,
        selections: Mapping[Channel, str | None],
        snapshot_id: str,
    ) -> tuple[PriceAnalysisRow, ...]:
        connection = open_database(self._database_path)
        connection.execute("PRAGMA query_only = ON")
        try:
            snapshot = ExchangeRateRepository(connection).get_snapshot(snapshot_id)
        finally:
            connection.close()
        if snapshot is None:
            raise PriceAnalysisError(f"汇率快照 {snapshot_id} 不存在")

        prices = self._price_library.load_prices(selections)
        rows: list[PriceAnalysisRow] = []
        for price in prices:
            rate = snapshot.rates.get(price.currency)
            if rate is None:
                rows.append(
                    PriceAnalysisRow(
                        channel=price.channel,
                        version_id=price.version_id,
                        country=price.country,
                        usd_tier=price.usd_tier,
                        currency=price.currency,
                        actual_local_price=price.local_price,
                        exchange_rate=None,
                        theoretical_local_price=None,
                        local_difference=None,
                        deviation_percent=None,
                        level=None,
                        adjustment_mode=price.adjustment_mode,
                    )
                )
                continue
            theoretical = price.usd_tier * rate
            difference = price.local_price - theoretical
            deviation = difference / theoretical * Decimal(100)
            rows.append(
                PriceAnalysisRow(
                    channel=price.channel,
                    version_id=price.version_id,
                    country=price.country,
                    usd_tier=price.usd_tier,
                    currency=price.currency,
                    actual_local_price=price.local_price,
                    exchange_rate=rate,
                    theoretical_local_price=theoretical,
                    local_difference=difference,
                    deviation_percent=deviation,
                    level=self._level(deviation),
                    adjustment_mode=price.adjustment_mode,
                )
            )
        return tuple(rows)

    @staticmethod
    def _level(deviation_percent: Decimal) -> DeviationLevel:
        absolute = abs(deviation_percent)
        if absolute >= Decimal(20):
            return DeviationLevel.SIGNIFICANT
        if absolute >= Decimal(10):
            return DeviationLevel.ATTENTION
        return DeviationLevel.NORMAL
