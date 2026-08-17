"""Read-only iOS CSV bundle adapter for P1-005."""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.models import (
    AdjustmentMode,
    Channel,
    ImportIssue,
    ImportPreview,
    ImportStatistics,
    ImportTaskStatus,
    IssueSeverity,
    StandardPrice,
)

EXPECTED_FILE_NAMES = (
    "当前价格 可能进行自动调整.csv",
    "当前价格 已手动调整.csv",
)
REQUIRED_HEADERS = (
    "国家或地区",
    "货币代码",
    "价格",
    "收入",
    "可能进行自动调整",
)
HEADER_ALIASES = {
    "国家或地区": frozenset({"国家或地区", "国家/地区"}),
    "货币代码": frozenset({"货币代码", "币种", "currency", "currencycode"}),
    "价格": frozenset({"价格", "price"}),
    "收入": frozenset({"收入", "proceeds"}),
    "可能进行自动调整": frozenset(
        {"可能进行自动调整", "自动调整", "automaticallyadjustable"}
    ),
}
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
TIER_FOLDER_PATTERN = re.compile(r"^[0-9]+\.[0-9]{2}$")


@dataclass(frozen=True, slots=True)
class IosAdapterConfig:
    country_names: Mapping[str, str]
    supported_currency_codes: frozenset[str]
    configured_tiers: frozenset[Decimal]
    max_file_size_bytes: int = 10 * 1024 * 1024


class IosPriceAdapter:
    """Parse a complete iOS tier directory without changing source files or a database."""

    def __init__(self, config: IosAdapterConfig) -> None:
        self._config = config
        self._country_names = {
            _normalise_name(name): code.upper()
            for name, code in config.country_names.items()
        }
        self._supported_currency_codes = frozenset(
            code.upper() for code in config.supported_currency_codes
        )
        self._configured_tiers = frozenset(
            Decimal(str(tier)) for tier in config.configured_tiers
        )

    def parse(self, directory_path: str | Path) -> ImportPreview:
        root = Path(directory_path).expanduser().resolve()
        source_path = str(root)
        if not root.is_dir():
            return self._fatal_preview(
                source_path,
                None,
                "iOS import requires a directory containing tier folders",
                source_value=str(root),
            )

        try:
            tier_directories = tuple(path for path in root.iterdir() if path.is_dir())
            csv_paths = tuple(
                path
                for tier_directory in tier_directories
                for path in tier_directory.iterdir()
                if path.is_file() and path.suffix.casefold() == ".csv"
            )
            source_sha256 = _bundle_sha256(root, csv_paths)
        except OSError as error:
            return self._fatal_preview(
                source_path, None, f"cannot access iOS source bundle: {error}"
            )

        issues: list[ImportIssue] = []
        directories_by_tier: dict[Decimal, Path] = {}
        for directory in sorted(tier_directories, key=lambda item: item.name):
            tier = _parse_tier_folder(directory.name)
            if tier is None or tier not in self._configured_tiers:
                issues.append(
                    _issue(
                        "I002",
                        IssueSeverity.ERROR,
                        "unexpected iOS tier directory",
                        directory.name,
                        source_value=directory.name,
                    )
                )
                continue
            directories_by_tier[tier] = directory

        for tier in sorted(self._configured_tiers):
            if tier not in directories_by_tier:
                issues.append(
                    _issue(
                        "I008",
                        IssueSeverity.ERROR,
                        "configured iOS tier directory is missing",
                        source_value=str(tier),
                    )
                )

        records: list[StandardPrice] = []
        records_by_key: dict[tuple[Channel, str, Decimal, str], StandardPrice] = {}
        currencies_by_country_tier: dict[tuple[str, Decimal], set[str]] = {}
        source_row_count = 0
        price_cell_count = 0
        duplicate_count = 0

        for tier, directory in sorted(directories_by_tier.items()):
            expected_paths = {name: directory / name for name in EXPECTED_FILE_NAMES}
            actual_csv_names = {
                path.name
                for path in csv_paths
                if path.parent == directory
            }
            for name, path in expected_paths.items():
                if not path.is_file():
                    issues.append(
                        _issue(
                            "I002",
                            IssueSeverity.ERROR,
                            "required iOS CSV file is missing",
                            f"{directory.name}/{name}",
                            source_value=name,
                        )
                    )
            for name in sorted(actual_csv_names - set(EXPECTED_FILE_NAMES)):
                issues.append(
                    _issue(
                        "I002",
                        IssueSeverity.ERROR,
                        "unexpected CSV file in iOS tier directory",
                        f"{directory.name}/{name}",
                        source_value=name,
                    )
                )

            for name in EXPECTED_FILE_NAMES:
                path = expected_paths[name]
                if not path.is_file():
                    continue
                source_name = f"{directory.name}/{name}"
                try:
                    if path.stat().st_size > self._config.max_file_size_bytes:
                        return self._fatal_preview(
                            source_path,
                            source_sha256,
                            "iOS CSV file exceeds the configured size limit",
                            sheet_name=source_name,
                            source_value=str(path.stat().st_size),
                        )
                    headers, rows = _read_csv(path)
                except (OSError, UnicodeError, csv.Error) as error:
                    return self._fatal_preview(
                        source_path,
                        source_sha256,
                        f"cannot read iOS CSV file: {error}",
                        sheet_name=source_name,
                    )

                header_indexes, header_issues = _resolve_headers(headers, source_name)
                issues.extend(header_issues)
                if header_issues:
                    continue

                for source_row, row in enumerate(rows, start=2):
                    if not any(value.strip() for value in row):
                        continue
                    source_row_count += 1
                    price_cell_count += 1
                    raw_country = _row_value(row, header_indexes["国家或地区"])
                    raw_currency = _row_value(row, header_indexes["货币代码"])
                    raw_price = _row_value(row, header_indexes["价格"])
                    raw_adjustment = _row_value(
                        row, header_indexes["可能进行自动调整"]
                    )

                    country_code = self._country_names.get(_normalise_name(raw_country))
                    if country_code is None:
                        issues.append(
                            _issue(
                                "I003",
                                IssueSeverity.ERROR,
                                "iOS country or region name is not configured",
                                source_name,
                                source_row,
                                "A",
                                raw_country,
                            )
                        )

                    currency = raw_currency.strip().upper()
                    if (
                        not CURRENCY_PATTERN.fullmatch(currency)
                        or currency not in self._supported_currency_codes
                    ):
                        issues.append(
                            _issue(
                                "I004",
                                IssueSeverity.ERROR,
                                "iOS currency code is invalid or unknown",
                                source_name,
                                source_row,
                                "B",
                                raw_currency,
                            )
                        )
                        currency = ""

                    local_price, price_error = _parse_positive_decimal(raw_price)
                    if price_error is not None:
                        issues.append(
                            _issue(
                                "I005",
                                IssueSeverity.ERROR,
                                price_error,
                                source_name,
                                source_row,
                                "C",
                                raw_price,
                            )
                        )

                    adjustment_mode = _parse_adjustment_mode(raw_adjustment)
                    if adjustment_mode is None:
                        issues.append(
                            _issue(
                                "I006",
                                IssueSeverity.ERROR,
                                "adjustment flag must be N or Y",
                                source_name,
                                source_row,
                                "E",
                                raw_adjustment,
                            )
                        )

                    if (
                        country_code is None
                        or not currency
                        or local_price is None
                        or adjustment_mode is None
                    ):
                        continue

                    record = StandardPrice(
                        channel=Channel.IOS,
                        country_code=country_code,
                        usd_tier=tier,
                        currency=currency,
                        local_price=local_price,
                        product_id=None,
                        source_sheet=source_name,
                        source_row=source_row,
                        source_column="C",
                        adjustment_mode=adjustment_mode,
                    )
                    country_tier_key = (country_code, tier)
                    known_currencies = currencies_by_country_tier.setdefault(
                        country_tier_key, set()
                    )
                    if known_currencies and currency not in known_currencies:
                        issues.append(
                            _issue(
                                "I007",
                                IssueSeverity.ERROR,
                                "the same iOS country and tier use multiple currencies",
                                source_name,
                                source_row,
                                "B",
                                currency,
                            )
                        )
                    known_currencies.add(currency)

                    existing = records_by_key.get(record.natural_key)
                    if existing is not None:
                        if (
                            existing.local_price == record.local_price
                            and existing.adjustment_mode is record.adjustment_mode
                        ):
                            duplicate_count += 1
                            issues.append(
                                _issue(
                                    "I102",
                                    IssueSeverity.WARNING,
                                    "identical iOS price was deduplicated",
                                    source_name,
                                    source_row,
                                    "C",
                                    raw_price,
                                )
                            )
                        else:
                            issues.append(
                                _issue(
                                    "I007",
                                    IssueSeverity.ERROR,
                                    "the same iOS standard key has conflicting data",
                                    source_name,
                                    source_row,
                                    "C",
                                    raw_price,
                                )
                            )
                        continue
                    records_by_key[record.natural_key] = record
                    records.append(record)

        return self._preview(
            source_path,
            source_sha256,
            records,
            issues,
            source_row_count,
            price_cell_count,
            duplicate_count,
        )

    def _preview(
        self,
        source_path: str,
        source_sha256: str,
        records: list[StandardPrice],
        issues: list[ImportIssue],
        source_row_count: int,
        price_cell_count: int,
        duplicate_count: int,
    ) -> ImportPreview:
        return ImportPreview(
            channel=Channel.IOS,
            source_path=source_path,
            source_sha256=source_sha256,
            selected_sheet=None,
            status=ImportTaskStatus.CHECKING,
            records=tuple(records),
            issues=tuple(issues),
            statistics=ImportStatistics(
                source_row_count=source_row_count,
                product_count=0,
                price_cell_count=price_cell_count,
                accepted_record_count=len(records),
                country_count=len({record.country_code for record in records}),
                currency_count=len({record.currency for record in records}),
                tier_count=len({record.usd_tier for record in records}),
                duplicate_count=duplicate_count,
                error_count=sum(
                    issue.severity is IssueSeverity.ERROR for issue in issues
                ),
                warning_count=sum(
                    issue.severity is IssueSeverity.WARNING for issue in issues
                ),
                manual_adjustment_count=sum(
                    record.adjustment_mode is AdjustmentMode.MANUAL for record in records
                ),
                automatic_adjustment_count=sum(
                    record.adjustment_mode is AdjustmentMode.AUTOMATIC for record in records
                ),
            ),
        )

    def _fatal_preview(
        self,
        source_path: str,
        source_sha256: str | None,
        message: str,
        *,
        sheet_name: str | None = None,
        source_value: str | None = None,
    ) -> ImportPreview:
        issue = _issue(
            "I001",
            IssueSeverity.ERROR,
            message,
            sheet_name,
            source_value=source_value,
        )
        return ImportPreview(
            channel=Channel.IOS,
            source_path=source_path,
            source_sha256=source_sha256,
            selected_sheet=None,
            status=ImportTaskStatus.FAILED,
            records=(),
            issues=(issue,),
            statistics=ImportStatistics(error_count=1),
        )


def _read_csv(path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = tuple(tuple(value for value in row) for row in csv.reader(source))
    if not rows:
        return (), ()
    return rows[0], rows[1:]


def _resolve_headers(
    headers: tuple[str, ...], source_name: str
) -> tuple[dict[str, int], list[ImportIssue]]:
    indexes: dict[str, int] = {}
    issues: list[ImportIssue] = []
    for canonical in REQUIRED_HEADERS:
        aliases = {_normalise_header(alias) for alias in HEADER_ALIASES[canonical]}
        matches = [
            index
            for index, header in enumerate(headers)
            if _normalise_header(header) in aliases
        ]
        if len(matches) != 1:
            issues.append(
                _issue(
                    "I002",
                    IssueSeverity.ERROR,
                    f"required iOS header {canonical!r} must appear exactly once",
                    source_name,
                    1,
                    None if not matches else _column_letter(matches[0]),
                    canonical,
                )
            )
            continue
        indexes[canonical] = matches[0]
    return indexes, issues


def _parse_tier_folder(raw_value: str) -> Decimal | None:
    if not TIER_FOLDER_PATTERN.fullmatch(raw_value):
        return None
    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _parse_positive_decimal(raw_value: str) -> tuple[Decimal | None, str | None]:
    value_text = raw_value.strip()
    if not value_text:
        return None, "price is blank"
    try:
        value = Decimal(value_text)
    except InvalidOperation:
        return None, "price is not numeric"
    if not value.is_finite():
        return None, "price is not finite"
    if value <= 0:
        return None, "price must be greater than zero"
    return value, None


def _parse_adjustment_mode(raw_value: str) -> AdjustmentMode | None:
    return {
        "N": AdjustmentMode.MANUAL,
        "Y": AdjustmentMode.AUTOMATIC,
    }.get(raw_value.strip().upper())


def _bundle_sha256(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative_path}|{file_hash}\n".encode())
    return digest.hexdigest()


def _normalise_name(value: str) -> str:
    return re.sub(r"\s+", "", value.strip()).casefold()


def _normalise_header(value: str) -> str:
    return re.sub(r"[\s/_-]+", "", value.strip()).casefold()


def _row_value(row: tuple[str, ...], index: int) -> str:
    return row[index] if index < len(row) else ""


def _column_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _issue(
    code: str,
    severity: IssueSeverity,
    message: str,
    sheet_name: str | None = None,
    source_row: int | None = None,
    source_column: str | None = None,
    source_value=None,
) -> ImportIssue:
    return ImportIssue(
        code=code,
        severity=severity,
        message=message,
        sheet_name=sheet_name,
        source_row=source_row,
        source_column=source_column,
        source_value=None if source_value is None else str(source_value),
    )
