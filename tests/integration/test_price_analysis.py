from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.database.connection import database_session
from src.database.seed import seed_database
from src.models import (
    Channel,
    DeviationLevel,
    ProviderRateBundle,
    RateFetchTrigger,
    format_deviation_percent,
    format_exchange_rate,
    format_signed_difference,
    format_theoretical_price,
)
from src.services import ExchangeRateService, PriceAnalysisService, PriceLibraryService


class StaticRateProvider:
    def fetch_latest(self) -> ProviderRateBundle:
        return ProviderRateBundle(
            provider="EXCHANGE_RATE_API",
            base_currency="USD",
            updated_at=datetime(2026, 8, 20, 0, 2, 31, tzinfo=UTC),
            next_update_at=datetime(2026, 8, 21, 0, 11, 41, tzinfo=UTC),
            rates={"USD": Decimal(1), "JPY": Decimal(150)},
        )


PHASE1_TABLES = (
    "countries",
    "price_tiers",
    "channel_products",
    "channel_prices",
    "price_versions",
    "import_tasks",
    "version_status_events",
)


def test_analysis_uses_one_snapshot_decimal_formula_and_fixed_thresholds(
    tmp_path: Path,
) -> None:
    database_path = _analysis_database(tmp_path)
    before_tables = _phase1_rows(database_path)
    refresh = ExchangeRateService(
        database_path,
        provider=StaticRateProvider(),
    ).refresh(
        RateFetchTrigger.MANUAL,
        requested_at=datetime(2026, 8, 20, 8, 20, tzinfo=UTC),
    )
    snapshot_id = refresh.snapshot.snapshot_id
    selections = {
        Channel.GOOGLE: "GOOGLE_V20260820_001",
        Channel.IOS: None,
        Channel.WEB: None,
    }
    library = PriceLibraryService(database_path)
    before = library.load_catalog().active_for(Channel.GOOGLE)

    rows = PriceAnalysisService(database_path).analyze(
        selections=selections,
        snapshot_id=snapshot_id,
    )

    by_tier = {row.usd_tier: row for row in rows}
    normal = by_tier[Decimal("1.99")]
    assert normal.theoretical_local_price == Decimal("298.50")
    assert normal.local_difference == Decimal("1.50")
    assert normal.deviation_percent == Decimal("0.5025125628140703517587939698")
    assert normal.level is DeviationLevel.NORMAL
    assert by_tier[Decimal("4.99")].deviation_percent == Decimal("10.0")
    assert by_tier[Decimal("4.99")].level is DeviationLevel.ATTENTION
    assert by_tier[Decimal("5.99")].deviation_percent == Decimal("20.0")
    assert by_tier[Decimal("5.99")].level is DeviationLevel.SIGNIFICANT
    assert by_tier[Decimal("9.99")].deviation_percent == Decimal("-10.0")
    assert by_tier[Decimal("9.99")].level is DeviationLevel.ATTENTION
    assert by_tier[Decimal("10.99")].deviation_percent == Decimal("-20.0")
    assert by_tier[Decimal("10.99")].level is DeviationLevel.SIGNIFICANT
    missing = by_tier[Decimal("14.99")]
    assert missing.currency == "EUR"
    assert missing.exchange_rate is None
    assert missing.deviation_percent is None
    assert missing.level is None

    after = library.load_catalog().active_for(Channel.GOOGLE)
    assert after == before

    assert _phase1_rows(database_path) == before_tables


def _analysis_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "price-analysis.db"
    seed_database(database_path)
    prices = (
        ("1.99", "300", "JPY"),
        ("4.99", "823.35", "JPY"),
        ("5.99", "1078.20", "JPY"),
        ("9.99", "1348.65", "JPY"),
        ("10.99", "1318.80", "JPY"),
        ("14.99", "1.00", "EUR"),
    )
    with database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO price_versions
                (version_id, channel, source_file, source_sha256,
                 import_time, status, record_count)
            VALUES ('GOOGLE_V20260820_001', 'GOOGLE', 'google.xlsx', 'digest',
                    '2026-08-20T08:00:00+08:00', 'ACTIVE', ?)
            """,
            (len(prices),),
        )
        connection.executemany(
            """
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price,
                 version_id, created_time)
            VALUES ('GOOGLE', 'JP', ?, ?, ?,
                    'GOOGLE_V20260820_001', '2026-08-20T08:00:00+08:00')
            """,
            ((tier, currency, price) for tier, price, currency in prices),
        )
    return database_path


def _phase1_rows(database_path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with database_session(database_path) as connection:
        return {
            table: tuple(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            )
            for table in PHASE1_TABLES
        }


def test_analysis_display_formats_are_rounded_only_at_the_edge() -> None:
    assert format_exchange_rate(Decimal("147.1235")) == "147.124"
    assert format_exchange_rate(Decimal("150.000")) == "150"
    assert format_theoretical_price(Decimal("298.5")) == "298.50"
    assert format_signed_difference(Decimal("1.5")) == "+1.50"
    assert format_signed_difference(Decimal("-1.5")) == "-1.50"
    assert format_deviation_percent(Decimal("0.502512")) == "+0.50%"
