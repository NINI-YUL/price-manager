from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import database_session
from src.database.repositories import ReferenceDataRepository
from src.database.schema import initialize_database
from src.models import AdjustmentMode, ImportTaskStatus
from src.services import IosImportService
from src.services.ios_import import load_ios_country_aliases

AUTO_FILE = "当前价格 可能进行自动调整.csv"
MANUAL_FILE = "当前价格 已手动调整.csv"
HEADERS = "国家或地区,货币代码,价格,收入,可能进行自动调整\n"
FIXED_TIME = datetime(2026, 8, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def test_service_records_checking_task_without_writing_formal_prices(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ios-import.db"
    aliases_path = _initialize_reference_data(database_path, tmp_path)
    bundle = _write_bundle(tmp_path)

    result = IosImportService(
        database_path, aliases_path=aliases_path, clock=lambda: FIXED_TIME
    ).preview(bundle, task_id="IOS_TASK_VALID")

    assert result.status is ImportTaskStatus.CHECKING
    assert result.statistics.accepted_record_count == 4
    assert result.statistics.manual_adjustment_count == 2
    assert result.statistics.automatic_adjustment_count == 2
    assert {record.adjustment_mode for record in result.records} == {
        AdjustmentMode.MANUAL,
        AdjustmentMode.AUTOMATIC,
    }
    assert {record.country_code for record in result.records} == {"US", "CN"}
    with database_session(database_path) as connection:
        task = connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = 'IOS_TASK_VALID'"
        ).fetchone()
        assert task["channel"] == "IOS"
        assert task["status"] == "CHECKING"
        assert task["error_count"] == 0
        assert task["completed_time"] is None
        assert connection.execute("SELECT COUNT(*) FROM channel_prices").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM price_versions").fetchone()[0] == 0


def test_fatal_parse_updates_task_to_failed(tmp_path: Path) -> None:
    database_path = tmp_path / "ios-fatal.db"
    aliases_path = _initialize_reference_data(database_path, tmp_path)

    result = IosImportService(
        database_path, aliases_path=aliases_path, clock=lambda: FIXED_TIME
    ).preview(tmp_path / "missing", task_id="IOS_TASK_FATAL")

    assert result.status is ImportTaskStatus.FAILED
    with database_session(database_path) as connection:
        task = connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = 'IOS_TASK_FATAL'"
        ).fetchone()
        assert task["status"] == "FAILED"
        assert task["error_count"] == 1
        assert task["completed_time"] == FIXED_TIME.isoformat()
        assert task["error_message"]


def test_unexpected_parser_error_is_recorded_and_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "ios-unexpected.db"
    aliases_path = _initialize_reference_data(database_path, tmp_path)
    bundle = _write_bundle(tmp_path)

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("synthetic iOS parser failure")

    monkeypatch.setattr("src.services.ios_import.IosPriceAdapter.parse", fail_parse)
    with pytest.raises(RuntimeError, match="synthetic iOS parser failure"):
        IosImportService(
            database_path, aliases_path=aliases_path, clock=lambda: FIXED_TIME
        ).preview(bundle, task_id="IOS_TASK_UNEXPECTED")

    with database_session(database_path) as connection:
        task = connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = 'IOS_TASK_UNEXPECTED'"
        ).fetchone()
        assert task["status"] == "FAILED"
        assert "synthetic iOS parser failure" in task["error_message"]


def test_reference_data_and_alias_config_are_validated_before_task(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "empty.db"
    initialize_database(database_path)
    aliases_path = tmp_path / "aliases.csv"
    aliases_path.write_text("alias,country_code\n中国大陆,CN\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="P1-003 reference data"):
        IosImportService(database_path, aliases_path=aliases_path).preview(
            tmp_path / "bundle", task_id="IOS_TASK_EMPTY"
        )

    with pytest.raises(ValueError, match="invalid iOS country alias"):
        load_ios_country_aliases(aliases_path, {"US"})
    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM import_tasks").fetchone()[0] == 0


def _initialize_reference_data(database_path: Path, tmp_path: Path) -> Path:
    initialize_database(database_path)
    with database_session(database_path) as connection:
        repository = ReferenceDataRepository(connection)
        repository.add_country(
            country_code="US", name_cn="美国", name_en="United States", default_currency="USD"
        )
        repository.add_country(
            country_code="CN", name_cn="中国", name_en="China", default_currency="CNY"
        )
        repository.add_price_tier(Decimal("0.99"))
        repository.add_price_tier(Decimal("1.99"))
    aliases_path = tmp_path / "ios-aliases.csv"
    aliases_path.write_text("alias,country_code\n中国大陆,CN\n", encoding="utf-8")
    return aliases_path


def _write_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "ios-bundle"
    for tier in ("0.99", "1.99"):
        folder = bundle / tier
        folder.mkdir(parents=True)
        (folder / AUTO_FILE).write_text(
            HEADERS + f"美国,USD,{tier},0.7,N\n",
            encoding="utf-8",
        )
        (folder / MANUAL_FILE).write_text(
            HEADERS + "中国大陆,CNY,8,5,Y\n",
            encoding="utf-8",
        )
    return bundle
