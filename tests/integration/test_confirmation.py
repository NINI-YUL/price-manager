from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import database_session
from src.database.repositories import ImportTaskRepository
from src.database.seed import seed_database
from src.models import (
    AdjustmentMode,
    Channel,
    ConfirmationError,
    ImportIssue,
    ImportPreview,
    ImportStatistics,
    ImportTaskStatus,
    IssueSeverity,
    StandardPrice,
)
from src.services import ImportConfirmationService
from src.services.source_archive import SourceArchiver
from src.utils.source_hash import file_sha256, ios_bundle_sha256

FIXED_TIME = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


def test_google_confirmation_writes_version_prices_product_and_archive(
    tmp_path: Path,
) -> None:
    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "google.xlsx", b"google-source")
    records = (
        _record(Channel.GOOGLE, "US", "0.99", "USD", "0.99", "product-0.99"),
        _record(Channel.GOOGLE, "JP", "0.99", "JPY", "150", "product-0.99"),
    )
    preview = _preview(Channel.GOOGLE, source, records)
    _checking_task(database_path, preview, "TASK-G-1")

    result = _service(database_path, archives).confirm(preview, task_id="TASK-G-1")

    assert result.version_id == "GOOGLE_V20260817_001"
    assert result.record_count == 2
    archived = archives / result.archive_path
    assert archived.read_bytes() == source.read_bytes()
    with database_session(database_path) as connection:
        version = connection.execute("SELECT * FROM price_versions").fetchone()
        task = connection.execute("SELECT * FROM import_tasks").fetchone()
        product = connection.execute("SELECT * FROM channel_products").fetchone()
        assert version["status"] == "ACTIVE"
        assert version["record_count"] == 2
        assert task["status"] == "SUCCESS"
        assert task["version_id"] == result.version_id
        assert product["product_id"] == "product-0.99"
        assert Decimal(str(product["usd_tier"])) == Decimal("0.99")
        assert connection.execute("SELECT COUNT(*) FROM channel_prices").fetchone()[0] == 2


def test_warning_requires_explicit_confirmation(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "warning.xlsx", b"warning-source")
    records = (_record(Channel.WEB, "US", "0.99", "USD", "0.99"),)
    warning = ImportIssue("W102", IssueSeverity.WARNING, "identical duplicate removed")
    preview = _preview(Channel.WEB, source, records, issues=(warning,))
    _checking_task(database_path, preview, "TASK-W-WARN")
    service = _service(database_path, archives)

    with pytest.raises(ConfirmationError, match="require explicit confirmation") as caught:
        service.confirm(preview, task_id="TASK-W-WARN")
    assert caught.value.code == "C003"

    result = service.confirm(
        preview,
        task_id="TASK-W-WARN",
        accept_warnings=True,
    )
    assert result.version_id == "WEB_V20260817_001"


def test_smaller_snapshot_requires_confirmation_and_replaces_old_version(
    tmp_path: Path,
) -> None:
    database_path, archives = _environment(tmp_path)
    service = _service(database_path, archives)
    first_source = _file_source(tmp_path, "first.xlsx", b"first")
    first = _preview(
        Channel.WEB,
        first_source,
        (
            _record(Channel.WEB, "US", "0.99", "USD", "0.99"),
            _record(Channel.WEB, "JP", "0.99", "JPY", "150"),
        ),
    )
    _checking_task(database_path, first, "TASK-W-1")
    first_result = service.confirm(first, task_id="TASK-W-1")

    second_source = _file_source(tmp_path, "second.xlsx", b"second")
    second = _preview(
        Channel.WEB,
        second_source,
        (_record(Channel.WEB, "US", "0.99", "USD", "1.09"),),
    )
    _checking_task(database_path, second, "TASK-W-2")

    with pytest.raises(ConfirmationError) as caught:
        service.confirm(second, task_id="TASK-W-2")
    assert caught.value.code == "C004"

    result = service.confirm(
        second,
        task_id="TASK-W-2",
        accept_coverage_reduction=True,
    )
    assert result.version_id == "WEB_V20260817_002"
    assert result.archived_version_id == first_result.version_id
    with database_session(database_path) as connection:
        statuses = {
            row["version_id"]: row["status"]
            for row in connection.execute("SELECT version_id, status FROM price_versions")
        }
        assert statuses == {
            "WEB_V20260817_001": "ARCHIVED",
            "WEB_V20260817_002": "ACTIVE",
        }
        current = connection.execute(
            "SELECT country_code FROM channel_prices WHERE version_id = ?",
            (result.version_id,),
        ).fetchall()
        assert [row["country_code"] for row in current] == ["US"]


def test_duplicate_successful_source_is_blocked(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "duplicate.xlsx", b"same")
    preview = _preview(
        Channel.WEB,
        source,
        (_record(Channel.WEB, "US", "0.99", "USD", "0.99"),),
    )
    service = _service(database_path, archives)
    _checking_task(database_path, preview, "TASK-DUP-1")
    first = service.confirm(preview, task_id="TASK-DUP-1")
    _checking_task(database_path, preview, "TASK-DUP-2")

    with pytest.raises(ConfirmationError) as caught:
        service.confirm(preview, task_id="TASK-DUP-2")
    assert caught.value.code == "C005"
    assert caught.value.existing_version_id == first.version_id


def test_changed_source_is_rejected_and_task_remains_checking(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "changed.xlsx", b"before")
    preview = _preview(
        Channel.WEB,
        source,
        (_record(Channel.WEB, "US", "0.99", "USD", "0.99"),),
    )
    _checking_task(database_path, preview, "TASK-CHANGED")
    source.write_bytes(b"after")

    with pytest.raises(ConfirmationError) as caught:
        _service(database_path, archives).confirm(preview, task_id="TASK-CHANGED")
    assert caught.value.code == "C006"
    with database_session(database_path) as connection:
        task = ImportTaskRepository(connection).get("TASK-CHANGED")
        assert task["status"] == "CHECKING"
        assert "changed" in task["error_message"]
        assert connection.execute("SELECT COUNT(*) FROM price_versions").fetchone()[0] == 0


def test_product_mapping_conflict_rolls_back_database_and_archive(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "conflict.xlsx", b"conflict")
    preview = _preview(
        Channel.GOOGLE,
        source,
        (_record(Channel.GOOGLE, "US", "0.99", "USD", "0.99", "fixed-product"),),
    )
    _checking_task(database_path, preview, "TASK-CONFLICT")
    with database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_products (channel, product_id, usd_tier)
            VALUES ('GOOGLE', 'fixed-product', 1.99)
            """
        )

    with pytest.raises(ConfirmationError) as caught:
        _service(database_path, archives).confirm(preview, task_id="TASK-CONFLICT")
    assert caught.value.code == "C008"
    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM price_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM channel_prices").fetchone()[0] == 0
        assert ImportTaskRepository(connection).get("TASK-CONFLICT")["status"] == "CHECKING"
    assert not list(archives.glob("google/*"))


def test_ios_confirmation_creates_readable_zip_and_keeps_adjustment_mode(
    tmp_path: Path,
) -> None:
    database_path, archives = _environment(tmp_path)
    source = tmp_path / "ios-source"
    csv_path = source / "0.99" / "当前价格 已手动调整.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("header\nvalue\n", encoding="utf-8")
    preview = _preview(
        Channel.IOS,
        source,
        (
            _record(
                Channel.IOS,
                "US",
                "0.99",
                "USD",
                "0.99",
                adjustment_mode=AdjustmentMode.MANUAL,
            ),
        ),
        source_sha256=ios_bundle_sha256(source),
    )
    _checking_task(database_path, preview, "TASK-IOS")

    result = _service(database_path, archives).confirm(preview, task_id="TASK-IOS")

    archive_path = archives / result.archive_path
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["0.99/当前价格 已手动调整.csv"]
        assert archive.testzip() is None
    with database_session(database_path) as connection:
        saved = connection.execute("SELECT adjustment_mode FROM channel_prices").fetchone()
        assert saved["adjustment_mode"] == "MANUAL"


def test_invalid_preview_and_task_mismatch_are_rejected(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "invalid.xlsx", b"invalid")
    record = _record(Channel.WEB, "US", "0.99", "USD", "0.99")
    error = ImportIssue("W006", IssueSeverity.ERROR, "bad price")
    invalid = _preview(Channel.WEB, source, (record,), issues=(error,))
    _checking_task(database_path, invalid, "TASK-INVALID")

    with pytest.raises(ConfirmationError) as caught:
        _service(database_path, archives).confirm(invalid, task_id="TASK-INVALID")
    assert caught.value.code == "C002"

    valid = _preview(Channel.WEB, source, (record,))
    with database_session(database_path) as connection:
        connection.execute(
            "UPDATE import_tasks SET channel = 'GOOGLE' WHERE task_id = 'TASK-INVALID'"
        )
    with pytest.raises(ConfirmationError) as caught:
        _service(database_path, archives).assess(valid, task_id="TASK-INVALID")
    assert caught.value.code == "C001"


def test_archive_failure_does_not_create_version(tmp_path: Path) -> None:
    class FailingArchiver(SourceArchiver):
        def prepare(self, **kwargs):
            raise ConfirmationError("C007", "synthetic archive failure")

    database_path, archives = _environment(tmp_path)
    source = _file_source(tmp_path, "archive-fail.xlsx", b"archive")
    preview = _preview(
        Channel.WEB,
        source,
        (_record(Channel.WEB, "US", "0.99", "USD", "0.99"),),
    )
    _checking_task(database_path, preview, "TASK-ARCHIVE-FAIL")
    service = ImportConfirmationService(
        database_path,
        archives_root=archives,
        clock=lambda: FIXED_TIME,
        archiver=FailingArchiver(archives),
    )

    with pytest.raises(ConfirmationError, match="synthetic archive failure"):
        service.confirm(preview, task_id="TASK-ARCHIVE-FAIL")
    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM price_versions").fetchone()[0] == 0


def test_orphaned_archives_are_reported_without_deletion(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    orphan = archives / "web" / "WEB_V20260817_999"
    orphan.mkdir(parents=True)
    (orphan / "source.xlsx").write_bytes(b"orphan")

    service = _service(database_path, archives)

    assert service.list_orphaned_archives() == ("web/WEB_V20260817_999",)
    assert orphan.is_dir()


def _environment(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "confirmation.db"
    seed_database(database_path)
    archives = tmp_path / "archives"
    return database_path, archives


def _service(database_path: Path, archives: Path) -> ImportConfirmationService:
    return ImportConfirmationService(
        database_path,
        archives_root=archives,
        clock=lambda: FIXED_TIME,
    )


def _file_source(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _checking_task(database_path: Path, preview: ImportPreview, task_id: str) -> None:
    with database_session(database_path) as connection:
        repository = ImportTaskRepository(connection)
        repository.create(
            task_id=task_id,
            channel=preview.channel,
            file_path=preview.source_path,
            created_time=FIXED_TIME.isoformat(),
        )
        repository.update_result(
            task_id=task_id,
            status=ImportTaskStatus.CHECKING,
            error_count=preview.statistics.error_count,
            warning_count=preview.statistics.warning_count,
            completed_time=None,
            error_message=None,
        )


def _preview(
    channel: Channel,
    source: Path,
    records: tuple[StandardPrice, ...],
    *,
    issues: tuple[ImportIssue, ...] = (),
    source_sha256: str | None = None,
) -> ImportPreview:
    return ImportPreview(
        channel=channel,
        source_path=str(source.resolve()),
        source_sha256=source_sha256
        or (ios_bundle_sha256(source) if channel is Channel.IOS else file_sha256(source)),
        selected_sheet=None if channel is Channel.IOS else "Sheet1",
        status=ImportTaskStatus.CHECKING,
        records=records,
        issues=issues,
        statistics=ImportStatistics(
            accepted_record_count=len(records),
            country_count=len({record.country_code for record in records}),
            currency_count=len({record.currency for record in records}),
            tier_count=len({record.usd_tier for record in records}),
            error_count=sum(issue.severity is IssueSeverity.ERROR for issue in issues),
            warning_count=sum(issue.severity is IssueSeverity.WARNING for issue in issues),
        ),
    )


def _record(
    channel: Channel,
    country_code: str,
    tier: str,
    currency: str,
    price: str,
    product_id: str | None = None,
    *,
    adjustment_mode: AdjustmentMode | None = None,
) -> StandardPrice:
    return StandardPrice(
        channel=channel,
        country_code=country_code,
        usd_tier=Decimal(tier),
        currency=currency,
        local_price=Decimal(price),
        product_id=product_id,
        source_sheet="Sheet1",
        source_row=2,
        source_column="D",
        adjustment_mode=adjustment_mode,
    )
