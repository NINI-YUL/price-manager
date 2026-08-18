from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.database.connection import database_session, open_database
from src.database.repositories import PriceVersionRepository
from src.database.schema import initialize_database
from src.database.seed import seed_database
from src.models import ArchiveStatus, Channel, VersionManagementError
from src.services import VersionManagementService
from src.utils.source_hash import file_sha256

FIXED_TIME = datetime(2026, 8, 18, 9, 30, tzinfo=UTC)


def test_list_detail_and_archive_validation_cover_file_zip_and_missing(
    tmp_path: Path,
) -> None:
    database_path, archives = _environment(tmp_path)
    google_active = _google_archive(
        archives,
        "GOOGLE_V20260818_001",
        b"google-active",
    )
    google_history = _google_archive(
        archives,
        "GOOGLE_V20260817_001",
        b"google-history",
    )
    ios_archive = _ios_archive(archives, "IOS_V20260818_001")
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_V20260818_001",
            "GOOGLE",
            google_active,
            file_sha256(archives / google_active),
            "ACTIVE",
            (("US", "9.99", "USD"), ("JP", "9.99", "JPY")),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260817_001",
            "GOOGLE",
            google_history,
            file_sha256(archives / google_history),
            "ARCHIVED",
            (("US", "9.99", "USD"),),
        )
        _insert_version(
            connection,
            "IOS_V20260818_001",
            "IOS",
            ios_archive,
            "original-directory-digest",
            "ACTIVE",
            (("US", "9.99", "USD"),),
        )
        _insert_version(
            connection,
            "WEB_V20260817_001",
            "WEB",
            "web/WEB_V20260817_001/missing.xlsx",
            "missing",
            "ARCHIVED",
            (("US", "9.99", "USD"),),
        )
        connection.execute(
            """
            INSERT INTO import_tasks
                (task_id, channel, file_path, status, created_time,
                 completed_time, version_id)
            VALUES ('TASK-G', 'GOOGLE', 'google.xlsx', 'SUCCESS', ?, ?,
                    'GOOGLE_V20260818_001')
            """,
            (FIXED_TIME.isoformat(), FIXED_TIME.isoformat()),
        )

    service = _service(database_path, archives)
    versions = service.list_versions()

    assert {item.summary.version_id for item in versions[:2]} == {
        "GOOGLE_V20260818_001",
        "IOS_V20260818_001",
    }
    google = service.get_detail("GOOGLE_V20260818_001")
    ios = service.get_detail("IOS_V20260818_001")
    missing = service.get_detail("WEB_V20260817_001")
    assert google.summary.country_count == 2
    assert google.summary.currency_count == 2
    assert google.summary.tier_count == 1
    assert google.summary.task_id == "TASK-G"
    assert google.archive.status is ArchiveStatus.COMPLETE
    assert ios.archive.status is ArchiveStatus.COMPLETE
    assert "原始目录摘要" in ios.archive.detail
    assert missing.archive.status is ArchiveStatus.MISSING


def test_manual_activation_is_atomic_and_audited(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    active_source = _google_archive(archives, "GOOGLE_V20260818_001", b"active")
    history_source = _google_archive(archives, "GOOGLE_V20260817_001", b"history")
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_V20260818_001",
            "GOOGLE",
            active_source,
            file_sha256(archives / active_source),
            "ACTIVE",
            (("US", "9.99", "USD"), ("JP", "9.99", "JPY")),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260817_001",
            "GOOGLE",
            history_source,
            file_sha256(archives / history_source),
            "ARCHIVED",
            (("US", "9.99", "USD"),),
        )
    service = _service(database_path, archives)

    assessment = service.assess_activation("GOOGLE_V20260817_001")
    assert assessment.current is not None
    assert assessment.current.version_id == "GOOGLE_V20260818_001"
    assert assessment.target.record_count == 1
    result = service.activate("GOOGLE_V20260817_001", note="回退到已验证版本")

    assert result.activated_version_id == "GOOGLE_V20260817_001"
    assert result.archived_version_id == "GOOGLE_V20260818_001"
    with database_session(database_path) as connection:
        statuses = {
            row["version_id"]: row["status"]
            for row in connection.execute(
                "SELECT version_id, status FROM price_versions ORDER BY version_id"
            )
        }
        events = connection.execute(
            """
            SELECT version_id, from_status, to_status, replaced_version_id,
                   reason, note, actor
            FROM version_status_events
            ORDER BY id
            """
        ).fetchall()
    assert statuses == {
        "GOOGLE_V20260817_001": "ACTIVE",
        "GOOGLE_V20260818_001": "ARCHIVED",
    }
    assert [(event["version_id"], event["from_status"], event["to_status"]) for event in events] == [
        ("GOOGLE_V20260818_001", "ACTIVE", "ARCHIVED"),
        ("GOOGLE_V20260817_001", "ARCHIVED", "ACTIVE"),
    ]
    assert {event["reason"] for event in events} == {"MANUAL_ACTIVATION"}
    assert {event["note"] for event in events} == {"回退到已验证版本"}
    assert {event["actor"] for event in events} == {"LOCAL_USER"}


def test_missing_archive_warns_but_does_not_block_activation(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "WEB_V20260818_001",
            "WEB",
            "web/WEB_V20260818_001/active.xlsx",
            "active",
            "ACTIVE",
            (("US", "9.99", "USD"),),
        )
        _insert_version(
            connection,
            "WEB_V20260817_001",
            "WEB",
            "web/WEB_V20260817_001/missing.xlsx",
            "missing",
            "ARCHIVED",
            (("US", "9.99", "USD"),),
        )
    service = _service(database_path, archives)

    assessment = service.assess_activation("WEB_V20260817_001")
    assert assessment.archive.status is ArchiveStatus.MISSING
    result = service.activate("WEB_V20260817_001")

    assert result.activated_version_id == "WEB_V20260817_001"


def test_activation_failure_rolls_back_both_statuses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path, archives = _environment(tmp_path)
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "GOOGLE_V20260818_001",
            "GOOGLE",
            "google/current/source.xlsx",
            "current",
            "ACTIVE",
            (("US", "9.99", "USD"),),
        )
        _insert_version(
            connection,
            "GOOGLE_V20260817_001",
            "GOOGLE",
            "google/history/source.xlsx",
            "history",
            "ARCHIVED",
            (("US", "9.99", "USD"),),
        )

    def fail_event(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(
        "src.services.version_management.VersionManagementRepository.add_event",
        fail_event,
    )
    with pytest.raises(VersionManagementError) as caught:
        _service(database_path, archives).activate("GOOGLE_V20260817_001")
    assert caught.value.code == "V005"

    with database_session(database_path) as connection:
        statuses = {
            row["version_id"]: row["status"]
            for row in connection.execute("SELECT version_id, status FROM price_versions")
        }
        assert connection.execute("SELECT COUNT(*) FROM version_status_events").fetchone()[0] == 0
    assert statuses == {
        "GOOGLE_V20260818_001": "ACTIVE",
        "GOOGLE_V20260817_001": "ARCHIVED",
    }


def test_import_repository_records_activation_and_archive_events(tmp_path: Path) -> None:
    database_path, _archives = _environment(tmp_path)
    with database_session(database_path) as connection:
        versions = PriceVersionRepository(connection)
        versions.archive_active(Channel.GOOGLE)
        versions.create_active(
            version_id="GOOGLE_V20260818_001",
            channel=Channel.GOOGLE,
            source_file="google/source.xlsx",
            source_sha256="digest-1",
            import_time=FIXED_TIME.isoformat(),
            record_count=0,
        )
        versions.archive_active(Channel.GOOGLE)
        versions.create_active(
            version_id="GOOGLE_V20260818_002",
            channel=Channel.GOOGLE,
            source_file="google/source-2.xlsx",
            source_sha256="digest-2",
            import_time=FIXED_TIME.isoformat(),
            record_count=0,
        )
        events = connection.execute(
            """
            SELECT version_id, from_status, to_status, replaced_version_id, reason
            FROM version_status_events ORDER BY id
            """
        ).fetchall()

    assert [(row["version_id"], row["from_status"], row["to_status"]) for row in events] == [
        ("GOOGLE_V20260818_001", None, "ACTIVE"),
        ("GOOGLE_V20260818_001", "ACTIVE", "ARCHIVED"),
        ("GOOGLE_V20260818_002", None, "ACTIVE"),
    ]
    assert events[1]["replaced_version_id"] == "GOOGLE_V20260818_002"
    assert {row["reason"] for row in events} == {"IMPORT_CONFIRMATION"}


def test_v3_migration_creates_baseline_event(tmp_path: Path) -> None:
    database_path = tmp_path / "v3.db"
    connection = open_database(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE price_versions (
                id INTEGER PRIMARY KEY,
                version_id TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_sha256 TEXT,
                import_time TEXT NOT NULL,
                status TEXT NOT NULL,
                record_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (version_id, channel)
            );
            INSERT INTO price_versions
                (version_id, channel, source_file, import_time, status, record_count)
            VALUES ('WEB_V20260817_001', 'WEB', 'web/source.xlsx',
                    '2026-08-17T10:00:00+08:00', 'ACTIVE', 0);
            PRAGMA user_version = 3;
            """
        )
    finally:
        connection.close()

    initialize_database(database_path)
    initialize_database(database_path)

    with database_session(database_path) as migrated:
        event = migrated.execute("SELECT * FROM version_status_events").fetchone()
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 4
        assert event["event_id"] == "MIGRATION_WEB_V20260817_001"
        assert event["from_status"] is None
        assert event["to_status"] == "ACTIVE"
        assert event["reason"] == "MIGRATION_BASELINE"


def test_invalid_activation_and_archive_path_are_rejected(tmp_path: Path) -> None:
    database_path, archives = _environment(tmp_path)
    with database_session(database_path) as connection:
        _insert_version(
            connection,
            "WEB_V20260818_001",
            "WEB",
            "../outside.xlsx",
            "digest",
            "ACTIVE",
            (("US", "9.99", "USD"),),
        )
    service = _service(database_path, archives)

    detail = service.get_detail("WEB_V20260818_001")
    assert detail.archive.status is ArchiveStatus.UNREADABLE
    with pytest.raises(VersionManagementError) as active:
        service.activate("WEB_V20260818_001")
    assert active.value.code == "V003"
    with pytest.raises(VersionManagementError) as note:
        service.activate("UNKNOWN", note="x" * 201)
    assert note.value.code == "V004"


def _environment(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "versions.db"
    seed_database(database_path)
    archives = tmp_path / "archives"
    return database_path, archives


def _service(database_path: Path, archives: Path) -> VersionManagementService:
    return VersionManagementService(
        database_path,
        archives_root=archives,
        clock=lambda: FIXED_TIME,
    )


def _google_archive(archives: Path, version_id: str, content: bytes) -> str:
    relative = Path("google") / version_id / "source.xlsx"
    path = archives / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return relative.as_posix()


def _ios_archive(archives: Path, version_id: str) -> str:
    relative = Path("ios") / version_id / "source.zip"
    path = archives / relative
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0.99/prices.csv", "country,price\nUS,0.99\n")
    return relative.as_posix()


def _insert_version(
    connection,
    version_id: str,
    channel: str,
    source_file: str,
    source_sha256: str,
    status: str,
    prices: tuple[tuple[str, str, str], ...],
) -> None:
    connection.execute(
        """
        INSERT INTO price_versions
            (version_id, channel, source_file, source_sha256,
             import_time, status, record_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            channel,
            source_file,
            source_sha256,
            FIXED_TIME.isoformat(),
            status,
            len(prices),
        ),
    )
    connection.executemany(
        """
        INSERT INTO channel_prices
            (channel, country_code, usd_tier, currency, local_price,
             version_id, created_time)
        VALUES (?, ?, ?, ?, '1', ?, ?)
        """,
        (
            (channel, country, tier, currency, version_id, FIXED_TIME.isoformat())
            for country, tier, currency in prices
        ),
    )
