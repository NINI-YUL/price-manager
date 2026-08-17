from __future__ import annotations

import hashlib
from collections import Counter
from decimal import Decimal
from pathlib import Path

from src.adapters import IosAdapterConfig, IosPriceAdapter
from src.models import AdjustmentMode, Channel, ImportTaskStatus

TIERS = frozenset({Decimal("0.99"), Decimal("1.99")})
AUTO_FILE = "当前价格 可能进行自动调整.csv"
MANUAL_FILE = "当前价格 已手动调整.csv"
HEADERS = "国家或地区,货币代码,价格,收入,可能进行自动调整\n"


def test_valid_bundle_maps_adjustment_mode_and_preserves_source(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    before = _hashes(bundle)

    result = _adapter().parse(bundle)

    assert _hashes(bundle) == before
    assert result.status is ImportTaskStatus.CHECKING
    assert result.channel is Channel.IOS
    assert result.selected_sheet is None
    assert result.issues == ()
    assert result.statistics.source_row_count == 6
    assert result.statistics.price_cell_count == 6
    assert result.statistics.accepted_record_count == 6
    assert result.statistics.country_count == 3
    assert result.statistics.currency_count == 3
    assert result.statistics.tier_count == 2
    assert result.statistics.manual_adjustment_count == 4
    assert result.statistics.automatic_adjustment_count == 2
    us = next(
        record
        for record in result.records
        if record.country_code == "US" and record.usd_tier == Decimal("0.99")
    )
    assert us.product_id is None
    assert us.local_price == Decimal("0.99")
    assert us.adjustment_mode is AdjustmentMode.MANUAL
    assert (us.source_sheet, us.source_row, us.source_column) == (
        f"0.99/{AUTO_FILE}",
        2,
        "C",
    )
    jp = next(
        record
        for record in result.records
        if record.country_code == "JP" and record.usd_tier == Decimal("0.99")
    )
    assert jp.adjustment_mode is AdjustmentMode.AUTOMATIC


def test_missing_tier_file_and_unexpected_structure_are_reported(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    (bundle / "1.99" / MANUAL_FILE).unlink()
    (bundle / "0.99" / "extra.csv").write_text(HEADERS, encoding="utf-8")
    (bundle / "bad-tier").mkdir()

    result = _adapter().parse(bundle)

    assert _codes(result) == Counter({"I002": 3})
    assert result.statistics.accepted_record_count == 5


def test_missing_configured_tier_is_blocking(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    for file in (bundle / "1.99").iterdir():
        file.unlink()
    (bundle / "1.99").rmdir()

    result = _adapter().parse(bundle)

    assert _codes(result) == Counter({"I008": 1})
    assert result.statistics.tier_count == 1


def test_bad_headers_are_reported_with_source_location(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    source = bundle / "0.99" / AUTO_FILE
    source.write_text(
        "国家或地区,货币代码,价格,价格,可能进行自动调整\n美国,USD,0.99,0.7,N\n",
        encoding="utf-8",
    )

    result = _adapter().parse(bundle)

    assert _codes(result) == Counter({"I002": 2})
    assert all(issue.source_row == 1 for issue in result.issues)


def test_country_currency_price_and_adjustment_errors_are_located(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    source = bundle / "0.99" / AUTO_FILE
    source.write_text(
        HEADERS
        + "未知地区,USD,0.99,0.7,Y\n"
        + "日本,ZZZ,text,0.7,X\n"
        + "美国,USD,-1,0.7,N\n",
        encoding="utf-8",
    )

    result = _adapter().parse(bundle)

    assert _codes(result) == Counter(
        {"I003": 1, "I004": 1, "I005": 2, "I006": 1}
    )
    assert result.statistics.accepted_record_count == 4
    assert {
        (issue.code, issue.source_column)
        for issue in result.issues
    } >= {("I003", "A"), ("I004", "B"), ("I005", "C"), ("I006", "E")}


def test_identical_duplicate_is_warned_and_conflict_is_blocking(tmp_path: Path) -> None:
    bundle = _write_valid_bundle(tmp_path)
    manual = bundle / "0.99" / MANUAL_FILE
    manual.write_text(
        HEADERS
        + "美国,USD,0.99,0.7,N\n"
        + "日本,JPY,999,700,N\n"
        + "香港,HKD,8,5,N\n",
        encoding="utf-8",
    )

    result = _adapter().parse(bundle)

    assert _codes(result) == Counter({"I102": 1, "I007": 1})
    assert result.statistics.duplicate_count == 1


def test_non_directory_oversized_and_invalid_utf8_are_fatal(tmp_path: Path) -> None:
    source_file = tmp_path / "not-a-directory.csv"
    source_file.write_text("x", encoding="utf-8")
    non_directory = _adapter().parse(source_file)

    bundle = _write_valid_bundle(tmp_path / "oversized")
    oversized = _adapter(max_file_size_bytes=1).parse(bundle)

    invalid_bundle = _write_valid_bundle(tmp_path / "invalid")
    (invalid_bundle / "0.99" / AUTO_FILE).write_bytes(b"\xff\xfe")
    invalid_utf8 = _adapter().parse(invalid_bundle)

    assert non_directory.status is ImportTaskStatus.FAILED
    assert oversized.status is ImportTaskStatus.FAILED
    assert invalid_utf8.status is ImportTaskStatus.FAILED
    assert _codes(non_directory) == Counter({"I001": 1})
    assert _codes(oversized) == Counter({"I001": 1})
    assert _codes(invalid_utf8) == Counter({"I001": 1})


def _adapter(*, max_file_size_bytes: int = 10 * 1024 * 1024) -> IosPriceAdapter:
    return IosPriceAdapter(
        IosAdapterConfig(
            country_names={"美国": "US", "日本": "JP", "香港": "HK"},
            supported_currency_codes=frozenset({"USD", "JPY", "HKD"}),
            configured_tiers=TIERS,
            max_file_size_bytes=max_file_size_bytes,
        )
    )


def _write_valid_bundle(base: Path) -> Path:
    bundle = base / "ios-bundle"
    for tier in ("0.99", "1.99"):
        folder = bundle / tier
        folder.mkdir(parents=True)
        (folder / AUTO_FILE).write_text(
            HEADERS
            + f"美国,USD,{tier},0.7,N\n"
            + "日本,JPY,100,70,Y\n",
            encoding="utf-8",
        )
        (folder / MANUAL_FILE).write_text(
            HEADERS + "香港,HKD,8,5,N\n",
            encoding="utf-8",
        )
    return bundle


def _codes(result) -> Counter[str]:
    return Counter(issue.code for issue in result.issues)


def _hashes(bundle: Path) -> dict[str, str]:
    return {
        str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in bundle.rglob("*.csv")
    }
