from __future__ import annotations

import hashlib
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from src.adapters import GoogleAdapterConfig, GooglePriceAdapter
from src.models import Channel, ImportTaskStatus, IssueSeverity

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "google"
TIERS = frozenset({Decimal("0.99"), Decimal("9.99")})


def test_valid_minimal_workbook_produces_exact_preview_without_modifying_source() -> None:
    source = FIXTURES / "google_minimal_valid.xlsx"
    before = _sha256(source)

    result = _adapter().parse(source)

    assert _sha256(source) == before
    assert result.status is ImportTaskStatus.CHECKING
    assert result.channel is Channel.GOOGLE
    assert result.selected_sheet == "Sheet1"
    assert result.issues == ()
    assert len(result.records) == 4
    assert result.statistics.source_row_count == 2
    assert result.statistics.product_count == 2
    assert result.statistics.price_cell_count == 4
    assert result.statistics.accepted_record_count == 4
    assert result.statistics.country_count == 2
    assert result.statistics.currency_count == 2
    assert result.statistics.tier_count == 2
    jp_tier = next(
        record
        for record in result.records
        if record.country_code == "JP" and record.usd_tier == Decimal("0.99")
    )
    assert jp_tier.local_price == Decimal(120)
    assert jp_tier.product_id == "com.example.game_iap_0.99&legacy-base"
    assert (jp_tier.source_sheet, jp_tier.source_row, jp_tier.source_column) == (
        "Sheet1",
        2,
        "D",
    )
    us_tier = next(
        record
        for record in result.records
        if record.country_code == "US" and record.usd_tier == Decimal("0.99")
    )
    assert us_tier.local_price == Decimal("0.99")


@pytest.mark.parametrize("suffix", [".xls", ".csv", ".txt"])
def test_unsupported_file_type_returns_located_g001(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / f"sample{suffix}"
    source.write_text("not an xlsx", encoding="utf-8")

    result = _adapter().parse(source)

    assert result.status is ImportTaskStatus.FAILED
    assert _codes(result) == Counter({"G001": 1})
    assert result.issues[0].source_value == suffix


def test_corrupt_or_oversized_workbook_returns_g001(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"not a zip package")

    corrupt_result = _adapter().parse(corrupt)
    oversized_result = _adapter(max_file_size_bytes=1).parse(
        FIXTURES / "google_minimal_valid.xlsx"
    )

    assert corrupt_result.status is ImportTaskStatus.FAILED
    assert _codes(corrupt_result) == Counter({"G001": 1})
    assert oversized_result.status is ImportTaskStatus.FAILED
    assert _codes(oversized_result) == Counter({"G001": 1})


def test_duplicate_required_and_product_headers_return_g002() -> None:
    result = _adapter().parse(FIXTURES / "google_duplicate_headers.xlsx")

    assert result.status is ImportTaskStatus.FAILED
    assert _codes(result) == Counter({"G002": 2})
    assert {issue.source_column for issue in result.issues} == {"C", "F"}


def test_multiple_matching_worksheets_return_g002() -> None:
    result = _adapter().parse(FIXTURES / "google_ambiguous_sheets.xlsx")

    assert result.status is ImportTaskStatus.FAILED
    assert _codes(result) == Counter({"G002": 1})
    assert result.issues[0].source_value == "First, Second"


def test_bad_product_id_returns_g003_and_missing_tier_g008() -> None:
    result = _adapter().parse(FIXTURES / "google_invalid_product.xlsx")

    assert result.status is ImportTaskStatus.CHECKING
    assert _codes(result) == Counter({"G003": 1, "G008": 1})
    assert result.statistics.accepted_record_count == 1
    assert result.issues[0].source_row == 1
    assert result.issues[0].source_column == "D"


def test_explicit_channel_product_mapping_has_priority_over_id_pattern() -> None:
    result = _adapter(
        product_tiers={"com.example.invalid": Decimal("0.99")}
    ).parse(FIXTURES / "google_invalid_product.xlsx")

    assert result.issues == ()
    assert {record.usd_tier for record in result.records} == TIERS
    assert result.records[0].product_id == "com.example.invalid"


def test_country_currency_and_price_errors_are_located_and_alias_is_warned() -> None:
    result = _adapter(country_aliases={"日本": "JP"}).parse(
        FIXTURES / "google_invalid_rows.xlsx"
    )

    assert result.status is ImportTaskStatus.CHECKING
    assert _codes(result) == Counter({"G004": 1, "G005": 1, "G006": 4, "G101": 1})
    assert result.statistics.source_row_count == 5
    assert result.statistics.price_cell_count == 10
    assert result.statistics.accepted_record_count == 2
    assert result.statistics.error_count == 6
    assert result.statistics.warning_count == 1
    alias_issue = next(issue for issue in result.issues if issue.code == "G101")
    assert (alias_issue.source_row, alias_issue.source_column, alias_issue.source_value) == (
        6,
        "B",
        "日本",
    )
    blank_issue = next(
        issue
        for issue in result.issues
        if issue.code == "G006" and issue.message == "price is blank"
    )
    assert (blank_issue.source_row, blank_issue.source_column) == (4, "D")


def test_identical_duplicates_are_warned_and_conflicts_are_blocking() -> None:
    result = _adapter().parse(FIXTURES / "google_duplicates_conflicts.xlsx")

    assert _codes(result) == Counter({"G007": 4, "G102": 2})
    assert result.statistics.accepted_record_count == 4
    assert result.statistics.duplicate_count == 2
    assert result.statistics.error_count == 4
    assert result.statistics.warning_count == 2
    assert all(
        issue.severity is IssueSeverity.WARNING
        for issue in result.issues
        if issue.code == "G102"
    )


def test_missing_configured_tier_returns_g008() -> None:
    result = _adapter().parse(FIXTURES / "google_missing_tier.xlsx")

    assert _codes(result) == Counter({"G008": 1})
    assert result.issues[0].source_value == "9.99"
    assert result.statistics.accepted_record_count == 1


def test_formula_without_cached_value_returns_g006() -> None:
    result = _adapter().parse(FIXTURES / "google_formula_no_cache.xlsx")

    assert _codes(result) == Counter({"G006": 2})
    assert result.statistics.accepted_record_count == 0
    assert {issue.source_value for issue in result.issues} == {
        "=UNSUPPORTED_FUNCTION(1)",
        "=UNSUPPORTED_FUNCTION(2)",
    }
    assert all("no cached value" in issue.message for issue in result.issues)


def _adapter(
    *,
    product_tiers: dict[str, Decimal] | None = None,
    country_aliases: dict[str, str] | None = None,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> GooglePriceAdapter:
    return GooglePriceAdapter(
        GoogleAdapterConfig(
            supported_country_codes=frozenset({"JP", "US"}),
            supported_currency_codes=frozenset({"JPY", "USD"}),
            configured_tiers=TIERS,
            product_tiers=product_tiers or {},
            country_aliases=country_aliases or {},
            max_file_size_bytes=max_file_size_bytes,
        )
    )


def _codes(result) -> Counter[str]:
    return Counter(issue.code for issue in result.issues)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
