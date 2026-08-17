"""Validate and seed the Phase1 reference data atomically."""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, database_session
from src.database.repositories import ReferenceDataRepository
from src.database.schema import initialize_database

SEEDS_DIR = Path(__file__).with_name("seeds")
COUNTRIES_PATH = SEEDS_DIR / "countries.csv"
PRICE_TIERS_PATH = SEEDS_DIR / "price_tiers.csv"
EXPECTED_COUNTRY_COUNT = 191
EXPECTED_PRICE_TIERS = tuple(
    Decimal(value)
    for value in (
        "0.99",
        "1.99",
        "4.99",
        "5.99",
        "9.99",
        "10.99",
        "14.99",
        "15.99",
        "19.99",
        "24.99",
        "29.99",
        "49.99",
        "69.99",
        "99.99",
    )
)
COUNTRY_HEADERS = ("country_code", "name_cn", "name_en", "default_currency")
COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
PRICE_PATTERN = re.compile(r"^[0-9]+\.[0-9]{2}$")


class SeedError(RuntimeError):
    """Base error raised by the P1-003 seed process."""


class SeedValidationError(SeedError):
    """Raised before any database write when a seed file is invalid."""


class SeedConflictError(SeedError):
    """Raised when existing reference data differs from the approved seed."""


@dataclass(frozen=True, slots=True)
class CountrySeed:
    country_code: str
    name_cn: str
    name_en: str
    default_currency: str


@dataclass(frozen=True, slots=True)
class SeedReport:
    countries_added: int
    countries_unchanged: int
    tiers_added: int
    tiers_unchanged: int
    conflicts: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def load_countries(path: Path = COUNTRIES_PATH) -> tuple[CountrySeed, ...]:
    """Load and fully validate the approved 191-country union seed file."""

    try:
        source = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise SeedValidationError(f"cannot read country seed file {path}: {error}") from error

    countries: list[CountrySeed] = []
    seen_codes: set[str] = set()
    with source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != COUNTRY_HEADERS:
            raise SeedValidationError(
                f"country seed headers must be {','.join(COUNTRY_HEADERS)}"
            )

        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise SeedValidationError(f"country seed row {row_number} has extra columns")
            country = CountrySeed(
                country_code=(row["country_code"] or "").strip(),
                name_cn=(row["name_cn"] or "").strip(),
                name_en=(row["name_en"] or "").strip(),
                default_currency=(row["default_currency"] or "").strip(),
            )
            if not COUNTRY_CODE_PATTERN.fullmatch(country.country_code):
                raise SeedValidationError(
                    f"country seed row {row_number} has invalid country_code "
                    f"{country.country_code!r}"
                )
            if not country.name_cn or not country.name_en:
                raise SeedValidationError(
                    f"country seed row {row_number} requires non-empty Chinese and English names"
                )
            if not CURRENCY_PATTERN.fullmatch(country.default_currency):
                raise SeedValidationError(
                    f"country seed row {row_number} has invalid default_currency "
                    f"{country.default_currency!r}"
                )
            if country.country_code in seen_codes:
                raise SeedValidationError(
                    f"country seed row {row_number} duplicates {country.country_code}"
                )
            seen_codes.add(country.country_code)
            countries.append(country)

    if len(countries) != EXPECTED_COUNTRY_COUNT:
        raise SeedValidationError(
            f"country seed must contain exactly {EXPECTED_COUNTRY_COUNT} rows; "
            f"got {len(countries)}"
        )
    return tuple(countries)


def load_price_tiers(path: Path = PRICE_TIERS_PATH) -> tuple[Decimal, ...]:
    """Load and verify the exact, ordered 14-tier approved baseline."""

    try:
        source = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise SeedValidationError(f"cannot read price-tier seed file {path}: {error}") from error

    tiers: list[Decimal] = []
    with source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ("usd_price",):
            raise SeedValidationError("price-tier seed header must be usd_price")

        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise SeedValidationError(f"price-tier seed row {row_number} has extra columns")
            raw_value = (row["usd_price"] or "").strip()
            if not PRICE_PATTERN.fullmatch(raw_value):
                raise SeedValidationError(
                    f"price-tier seed row {row_number} must be a number with two decimals; "
                    f"got {raw_value!r}"
                )
            value = Decimal(raw_value)
            if value <= 0:
                raise SeedValidationError(
                    f"price-tier seed row {row_number} must be positive; got {raw_value}"
                )
            tiers.append(value)

    if len(set(tiers)) != len(tiers):
        raise SeedValidationError("price-tier seed contains duplicate values")
    if tuple(tiers) != EXPECTED_PRICE_TIERS:
        expected = ", ".join(str(value) for value in EXPECTED_PRICE_TIERS)
        actual = ", ".join(str(value) for value in tiers)
        raise SeedValidationError(
            f"price-tier seed must match the approved 14-tier baseline; "
            f"expected [{expected}], got [{actual}]"
        )
    return tuple(tiers)


def seed_database(
    database_path: DatabasePath = DATABASE_PATH,
    *,
    countries_path: Path = COUNTRIES_PATH,
    price_tiers_path: Path = PRICE_TIERS_PATH,
) -> SeedReport:
    """Validate all files, then insert reference data in one transaction."""

    countries = load_countries(countries_path)
    price_tiers = load_price_tiers(price_tiers_path)
    resolved_database_path = initialize_database(database_path)

    countries_added = 0
    countries_unchanged = 0
    tiers_added = 0
    tiers_unchanged = 0

    with database_session(resolved_database_path) as connection:
        repository = ReferenceDataRepository(connection)
        for country in countries:
            existing = repository.get_country(country.country_code)
            if existing is None:
                repository.add_country(
                    country_code=country.country_code,
                    name_cn=country.name_cn,
                    name_en=country.name_en,
                    default_currency=country.default_currency,
                )
                countries_added += 1
                continue

            differences = {
                field: (str(existing[field]), getattr(country, field))
                for field in ("name_cn", "name_en", "default_currency")
                if str(existing[field]) != getattr(country, field)
            }
            if differences:
                detail = "; ".join(
                    f"{field}: database={old!r}, seed={new!r}"
                    for field, (old, new) in differences.items()
                )
                raise SeedConflictError(f"country {country.country_code} conflicts: {detail}")
            countries_unchanged += 1

        for usd_price in price_tiers:
            if repository.get_price_tier(usd_price) is None:
                repository.add_price_tier(usd_price)
                tiers_added += 1
            else:
                tiers_unchanged += 1

    return SeedReport(
        countries_added=countries_added,
        countries_unchanged=countries_unchanged,
        tiers_added=tiers_added,
        tiers_unchanged=tiers_unchanged,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize Phase1 reference data")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    args = parser.parse_args()
    report = seed_database(args.database)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
