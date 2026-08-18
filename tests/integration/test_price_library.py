from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import database_session
from src.database.seed import seed_database
from src.models import AdjustmentMode, Channel, format_local_price, format_usd_tier
from src.services import PriceLibraryError, PriceLibraryService


def test_catalog_defaults_and_independent_version_combination(tmp_path: Path) -> None:
    database_path = _library_database(tmp_path)
    service = PriceLibraryService(database_path)

    catalog = service.load_catalog()

    assert len(catalog.countries) == 191
    assert len(catalog.tiers) == 14
    assert catalog.active_for(Channel.GOOGLE).version_id == "GOOGLE_V20260818_001"
    assert catalog.active_for(Channel.IOS).version_id == "IOS_V20260818_001"
    assert catalog.active_for(Channel.WEB) is None
    google_versions = catalog.versions_for(Channel.GOOGLE)
    assert [version.version_id for version in google_versions] == [
        "GOOGLE_V20260818_001",
        "GOOGLE_V20260705_001",
        "GOOGLE_V20251201_001",
    ]

    prices = service.load_prices(
        {
            Channel.GOOGLE: "GOOGLE_V20260705_001",
            Channel.IOS: "IOS_V20260818_001",
            Channel.WEB: None,
        }
    )

    assert {(price.channel, price.version_id) for price in prices} == {
        (Channel.GOOGLE, "GOOGLE_V20260705_001"),
        (Channel.IOS, "IOS_V20260818_001"),
    }
    google_us = next(
        price
        for price in prices
        if price.channel is Channel.GOOGLE and price.country.country_code == "US"
    )
    assert google_us.local_price == Decimal("8.99")


def test_tier_view_union_all_countries_currency_filter_and_ios_mode(tmp_path: Path) -> None:
    database_path = _library_database(tmp_path)
    service = PriceLibraryService(database_path)
    catalog = service.load_catalog()
    prices = service.load_prices(
        {
            Channel.GOOGLE: "GOOGLE_V20260818_001",
            Channel.IOS: "IOS_V20260818_001",
            Channel.WEB: None,
        }
    )

    rows = service.tier_view(
        catalog=catalog,
        prices=prices,
        usd_tier=Decimal("9.99"),
    )

    assert [row.country.country_code for row in rows] == ["JP", "KR", "US"]
    us = next(row for row in rows if row.country.country_code == "US")
    assert us.price_for(Channel.GOOGLE).currency == "USD"
    assert us.price_for(Channel.IOS).adjustment_mode is AdjustmentMode.MANUAL
    jp = next(row for row in rows if row.country.country_code == "JP")
    assert jp.price_for(Channel.IOS) is None

    usd_rows = service.tier_view(
        catalog=catalog,
        prices=prices,
        usd_tier=Decimal("9.99"),
        currency="USD",
    )
    assert [row.country.country_code for row in usd_rows] == ["US"]
    assert usd_rows[0].price_for(Channel.GOOGLE) is not None
    assert usd_rows[0].price_for(Channel.IOS) is not None

    all_rows = service.tier_view(
        catalog=catalog,
        prices=prices,
        usd_tier=Decimal("9.99"),
        show_all_countries=True,
        search="Japan",
    )
    assert [row.country.country_code for row in all_rows] == ["JP"]


def test_country_view_always_uses_approved_tiers_and_missing_cells(tmp_path: Path) -> None:
    database_path = _library_database(tmp_path)
    service = PriceLibraryService(database_path)
    catalog = service.load_catalog()
    prices = service.load_prices(
        {
            Channel.GOOGLE: "GOOGLE_V20260818_001",
            Channel.IOS: "IOS_V20260818_001",
            Channel.WEB: None,
        }
    )

    rows = service.country_view(
        catalog=catalog,
        prices=prices,
        country_code="US",
    )

    assert len(rows) == 14
    assert [row.usd_tier for row in rows] == list(catalog.tiers)
    tier = next(row for row in rows if row.usd_tier == Decimal("9.99"))
    assert tier.price_for(Channel.GOOGLE).local_price == Decimal("9.99")
    assert tier.price_for(Channel.IOS).local_price == Decimal("10.99")
    assert tier.price_for(Channel.WEB) is None

    filtered = service.country_view(
        catalog=catalog,
        prices=prices,
        country_code="US",
        search="10.99",
    )
    assert [row.usd_tier for row in filtered] == [Decimal("10.99")]


def test_wrong_channel_version_is_rejected_without_database_writes(tmp_path: Path) -> None:
    database_path = _library_database(tmp_path)
    service = PriceLibraryService(database_path)
    with database_session(database_path) as connection:
        before = tuple(
            connection.execute(
                "SELECT version_id, status FROM price_versions ORDER BY version_id"
            )
        )
        price_count = int(connection.execute("SELECT COUNT(*) FROM channel_prices").fetchone()[0])

    with pytest.raises(PriceLibraryError, match="不属于"):
        service.load_prices(
            {
                Channel.GOOGLE: "IOS_V20260818_001",
                Channel.IOS: None,
                Channel.WEB: None,
            }
        )

    with database_session(database_path) as connection:
        after = tuple(
            connection.execute(
                "SELECT version_id, status FROM price_versions ORDER BY version_id"
            )
        )
        assert [(row["version_id"], row["status"]) for row in after] == [
            (row["version_id"], row["status"]) for row in before
        ]
        assert connection.execute("SELECT COUNT(*) FROM channel_prices").fetchone()[0] == price_count


def test_decimal_display_never_uses_exponents_or_thousands_separators() -> None:
    assert format_local_price(Decimal(1500)) == "1500"
    assert format_local_price(Decimal("0.000001")) == "0.000001"
    assert format_local_price(Decimal("1234.5600")) == "1234.5600"
    assert format_usd_tier(Decimal("9.99")) == "9.99"


def _library_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "price-library.db"
    seed_database(database_path)
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_V20251201_001",
            "GOOGLE",
            "archive/google-2025.xlsx",
            "2025-12-01T09:00:00+08:00",
            "ARCHIVED",
            (("US", "9.99", "USD", "7.99", None),),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260705_001",
            "GOOGLE",
            "archive/google-july.xlsx",
            "2026-07-05T09:00:00+08:00",
            "ARCHIVED",
            (("US", "9.99", "USD", "8.99", None),),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260818_001",
            "GOOGLE",
            "archive/google-active.xlsx",
            "2026-08-18T09:00:00+08:00",
            "ACTIVE",
            (
                ("US", "9.99", "USD", "9.99", None),
                ("JP", "9.99", "JPY", "1500", None),
            ),
        )
        _insert_version(
            connection,
            "IOS_V20260818_001",
            "IOS",
            "archive/ios-active.zip",
            "2026-08-18T10:00:00+08:00",
            "ACTIVE",
            (
                ("US", "9.99", "USD", "10.99", "MANUAL"),
                ("KR", "9.99", "KRW", "14000", "AUTOMATIC"),
            ),
        )
    return database_path


def _insert_version(
    connection,
    version_id: str,
    channel: str,
    source_file: str,
    import_time: str,
    status: str,
    prices: tuple[tuple[str, str, str, str, str | None], ...],
) -> None:
    connection.execute(
        """
        INSERT INTO price_versions
            (version_id, channel, source_file, source_sha256,
             import_time, status, record_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (version_id, channel, source_file, version_id.lower(), import_time, status, len(prices)),
    )
    connection.executemany(
        """
        INSERT INTO channel_prices
            (channel, country_code, usd_tier, currency, local_price,
             adjustment_mode, version_id, created_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (channel, country, tier, currency, price, mode, version_id, import_time)
            for country, tier, currency, price, mode in prices
        ),
    )
