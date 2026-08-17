from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import database_session
from src.database.repositories import ReferenceDataRepository
from src.database.schema import initialize_database
from src.models import ImportTaskStatus
from src.services import GoogleImportService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "google"
FIXED_TIME = datetime(2026, 8, 17, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def test_service_records_checking_task_without_writing_formal_prices(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "google-import.db"
    _initialize_reference_data(database_path)
    service = GoogleImportService(database_path, clock=lambda: FIXED_TIME)

    result = service.preview(
        FIXTURES / "google_minimal_valid.xlsx", task_id="GOOGLE_TASK_VALID"
    )

    assert result.status is ImportTaskStatus.CHECKING
    assert result.statistics.accepted_record_count == 4
    assert result.statistics.error_count == 0
    with database_session(database_path) as connection:
        task = connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = 'GOOGLE_TASK_VALID'"
        ).fetchone()
        assert task["status"] == "CHECKING"
        assert task["error_count"] == 0
        assert task["warning_count"] == 0
        assert task["completed_time"] is None
        assert connection.execute("SELECT COUNT(*) FROM channel_prices").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM price_versions").fetchone()[0] == 0


def test_service_uses_existing_channel_product_mapping(tmp_path: Path) -> None:
    database_path = tmp_path / "mapped-product.db"
    _initialize_reference_data(database_path)
    with database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_products (channel, product_id, usd_tier)
            VALUES ('GOOGLE', 'com.example.invalid', 0.99)
            """
        )

    result = GoogleImportService(database_path, clock=lambda: FIXED_TIME).preview(
        FIXTURES / "google_invalid_product.xlsx", task_id="GOOGLE_TASK_MAPPING"
    )

    assert result.issues == ()
    assert result.statistics.accepted_record_count == 2


def test_fatal_parse_updates_task_to_failed(tmp_path: Path) -> None:
    database_path = tmp_path / "fatal-import.db"
    _initialize_reference_data(database_path)
    corrupt = tmp_path / "corrupt.xlsx"
    corrupt.write_bytes(b"not an xlsx package")

    result = GoogleImportService(database_path, clock=lambda: FIXED_TIME).preview(
        corrupt, task_id="GOOGLE_TASK_FATAL"
    )

    assert result.status is ImportTaskStatus.FAILED
    with database_session(database_path) as connection:
        task = connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = 'GOOGLE_TASK_FATAL'"
        ).fetchone()
        assert task["status"] == "FAILED"
        assert task["error_count"] == 1
        assert task["completed_time"] == FIXED_TIME.isoformat()
        assert task["error_message"]


def test_unexpected_parser_error_is_recorded_and_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "unexpected-import.db"
    _initialize_reference_data(database_path)

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr("src.services.google_import.GooglePriceAdapter.parse", fail_parse)
    with pytest.raises(RuntimeError, match="synthetic parser failure"):
        GoogleImportService(database_path, clock=lambda: FIXED_TIME).preview(
            FIXTURES / "google_minimal_valid.xlsx", task_id="GOOGLE_TASK_UNEXPECTED"
        )

    with database_session(database_path) as connection:
        task = connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = 'GOOGLE_TASK_UNEXPECTED'"
        ).fetchone()
        assert task["status"] == "FAILED"
        assert task["error_count"] == 1
        assert "synthetic parser failure" in task["error_message"]


def test_reference_data_is_required_before_task_creation(tmp_path: Path) -> None:
    database_path = tmp_path / "empty-reference.db"
    initialize_database(database_path)

    with pytest.raises(RuntimeError, match="P1-003 reference data"):
        GoogleImportService(database_path, clock=lambda: FIXED_TIME).preview(
            FIXTURES / "google_minimal_valid.xlsx", task_id="GOOGLE_TASK_NO_REFERENCE"
        )

    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM import_tasks").fetchone()[0] == 0


def _initialize_reference_data(database_path: Path) -> None:
    initialize_database(database_path)
    with database_session(database_path) as connection:
        repository = ReferenceDataRepository(connection)
        repository.add_country(
            country_code="JP", name_cn="日本", name_en="Japan", default_currency="JPY"
        )
        repository.add_country(
            country_code="US",
            name_cn="美国",
            name_en="United States",
            default_currency="USD",
        )
        repository.add_price_tier(Decimal("0.99"))
        repository.add_price_tier(Decimal("9.99"))
