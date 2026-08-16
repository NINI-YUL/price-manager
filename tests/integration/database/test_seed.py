from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import database_session
from src.database.repositories import ReferenceDataRepository
from src.database.schema import initialize_database
from src.database.seed import (
    COUNTRIES_PATH,
    EXPECTED_COUNTRY_COUNT,
    EXPECTED_PRICE_TIERS,
    PRICE_TIERS_PATH,
    SEEDS_DIR,
    SeedConflictError,
    SeedValidationError,
    load_countries,
    load_price_tiers,
    seed_database,
)


def test_approved_seed_files_have_expected_content_and_checksums() -> None:
    countries = load_countries()
    tiers = load_price_tiers()
    by_code = {country.country_code: country for country in countries}

    assert len(countries) == EXPECTED_COUNTRY_COUNT == 173
    assert len(by_code) == 173
    assert tuple(country.country_code for country in countries) == tuple(sorted(by_code))
    assert by_code["JP"].name_cn == "日本"
    assert by_code["JP"].name_en == "Japan"
    assert by_code["JP"].default_currency == "JPY"
    assert by_code["US"].default_currency == "USD"
    assert tiers == EXPECTED_PRICE_TIERS
    assert Decimal("9.99") in tiers

    manifest = json.loads((SEEDS_DIR / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["googleSource"]["rowCount"] == 173
    assert manifest["cldrSource"]["version"] == "48.2.0"
    assert manifest["outputs"]["countries"]["sha256"] == _sha256(COUNTRIES_PATH)
    assert manifest["outputs"]["tiers"]["sha256"] == _sha256(PRICE_TIERS_PATH)


def test_seed_database_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "seed.db"

    first = seed_database(database_path)
    second = seed_database(database_path)

    assert first.as_dict() == {
        "countries_added": 173,
        "countries_unchanged": 0,
        "tiers_added": 14,
        "tiers_unchanged": 0,
        "conflicts": 0,
        "failures": 0,
    }
    assert second.as_dict() == {
        "countries_added": 0,
        "countries_unchanged": 173,
        "tiers_added": 0,
        "tiers_unchanged": 14,
        "conflicts": 0,
        "failures": 0,
    }

    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 173
        assert connection.execute("SELECT COUNT(*) FROM price_tiers").fetchone()[0] == 14
        assert connection.execute(
            "SELECT default_currency FROM countries WHERE country_code = 'JP'"
        ).fetchone()[0] == "JPY"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("AE,", "A1,"),
        (",AED\n", ",A1D\n"),
        (",阿拉伯联合酋长国,", ",,"),
    ],
)
def test_invalid_country_fields_are_rejected(
    tmp_path: Path, old: str, new: str
) -> None:
    invalid_path = tmp_path / "countries.csv"
    source = COUNTRIES_PATH.read_text(encoding="utf-8")
    assert old in source
    invalid_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(SeedValidationError):
        load_countries(invalid_path)


def test_duplicate_country_is_rejected(tmp_path: Path) -> None:
    invalid_path = tmp_path / "countries.csv"
    lines = COUNTRIES_PATH.read_text(encoding="utf-8").splitlines()
    invalid_path.write_text("\n".join([*lines, lines[1]]) + "\n", encoding="utf-8")

    with pytest.raises(SeedValidationError, match="duplicates AE"):
        load_countries(invalid_path)


@pytest.mark.parametrize("invalid_value", ["0.00", "-0.99", "word", "0.990", "1.99"])
def test_invalid_or_duplicate_tier_is_rejected(
    tmp_path: Path, invalid_value: str
) -> None:
    invalid_path = tmp_path / "price_tiers.csv"
    values = [str(value) for value in EXPECTED_PRICE_TIERS]
    values[0] = invalid_value
    invalid_path.write_text("usd_price\n" + "\n".join(values) + "\n", encoding="utf-8")

    with pytest.raises(SeedValidationError):
        load_price_tiers(invalid_path)


def test_all_validation_finishes_before_database_creation(tmp_path: Path) -> None:
    database_path = tmp_path / "must-not-exist.db"
    invalid_tiers = tmp_path / "invalid-tiers.csv"
    invalid_tiers.write_text("usd_price\n0.00\n", encoding="utf-8")

    with pytest.raises(SeedValidationError):
        seed_database(database_path, price_tiers_path=invalid_tiers)

    assert not database_path.exists()


def test_existing_conflict_reports_difference_and_rolls_back_new_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "conflict.db"
    initialize_database(database_path)
    with database_session(database_path) as connection:
        ReferenceDataRepository(connection).add_country(
            country_code="US",
            name_cn="美国",
            name_en="United States",
            default_currency="EUR",
        )

    with pytest.raises(
        SeedConflictError,
        match="country US conflicts: default_currency: database='EUR', seed='USD'",
    ):
        seed_database(database_path)

    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM price_tiers").fetchone()[0] == 0
        assert connection.execute(
            "SELECT default_currency FROM countries WHERE country_code = 'US'"
        ).fetchone()[0] == "EUR"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
