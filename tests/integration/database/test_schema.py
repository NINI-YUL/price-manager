from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from src.database.connection import database_session, open_database
from src.database.repositories import ReferenceDataRepository
from src.database.schema import BUSINESS_TABLES, initialize_database, list_business_tables


def test_schema_initialization_is_idempotent_and_reference_tables_are_empty(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "schema.db"

    initialize_database(database_path)
    initialize_database(database_path)

    with database_session(database_path) as connection:
        assert list_business_tables(connection) == BUSINESS_TABLES
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM price_tiers").fetchone()[0] == 0


def test_connection_enables_foreign_keys(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "foreign-keys.db")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_reference_repository_inserts_and_queries_by_natural_key(tmp_path: Path) -> None:
    database_path = tmp_path / "repository.db"
    initialize_database(database_path)

    with database_session(database_path) as connection:
        repository = ReferenceDataRepository(connection)
        repository.add_country(
            country_code="JP", name_cn="日本", name_en="Japan", default_currency="JPY"
        )
        repository.add_price_tier(Decimal("9.99"))

        assert repository.get_country("JP")["default_currency"] == "JPY"
        assert Decimal(str(repository.get_price_tier(Decimal("9.99"))["usd_price"])) == Decimal(
            "9.99"
        )


def test_transaction_rolls_back_all_writes_on_error(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        repository = ReferenceDataRepository(connection)
        repository.add_country(
            country_code="US",
            name_cn="美国",
            name_en="United States",
            default_currency="USD",
        )
        repository.add_country(
            country_code="US",
            name_cn="重复",
            name_en="Duplicate",
            default_currency="USD",
        )

    with database_session(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 0


def test_foreign_keys_and_price_checks_reject_invalid_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "constraints.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_products (channel, product_id, usd_tier)
            VALUES ('GOOGLE', 'missing-tier', 9.99)
            """
        )

    _seed_price_dependencies(database_path)

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price, version_id, created_time)
            VALUES ('IOS', 'JP', 9.99, 'JPY', 1500, 'GOOGLE_V20260816_001',
                    '2026-08-16T23:00:00+08:00')
            """
        )

    with database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price, version_id, created_time)
            VALUES ('GOOGLE', 'JP', 9.99, 'JPY', 1500, 'GOOGLE_V20260816_001',
                    '2026-08-16T23:00:00+08:00')
            """
        )
        saved = connection.execute(
            """
            SELECT local_price FROM channel_prices
            WHERE version_id = 'GOOGLE_V20260816_001' AND country_code = 'JP'
            """
        ).fetchone()
        assert Decimal(str(saved["local_price"])) == Decimal(1500)

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price, version_id, created_time)
            VALUES ('GOOGLE', 'JP', 9.99, 'JPY', -1, 'GOOGLE_V20260816_001',
                    '2026-08-16T23:00:00+08:00')
            """
        )


def test_only_one_active_version_is_allowed_per_channel(tmp_path: Path) -> None:
    database_path = tmp_path / "active-version.db"
    initialize_database(database_path)

    with database_session(database_path) as connection:
        _insert_version(connection, "GOOGLE_V20260816_001", "GOOGLE")
        _insert_version(connection, "IOS_V20260816_001", "IOS")

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        _insert_version(connection, "GOOGLE_V20260816_002", "GOOGLE")


@pytest.mark.parametrize("status", ["PENDING", "DONE", ""])
def test_import_task_rejects_unknown_status(tmp_path: Path, status: str) -> None:
    database_path = tmp_path / f"task-{status or 'empty'}.db"
    initialize_database(database_path)

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO import_tasks
                (task_id, channel, file_path, status, created_time)
            VALUES (?, 'GOOGLE', 'sample.xlsx', ?, '2026-08-16T23:00:00+08:00')
            """,
            (f"TASK-{status or 'EMPTY'}", status),
        )


def test_schema_declares_required_indexes(tmp_path: Path) -> None:
    database_path = tmp_path / "indexes.db"
    initialize_database(database_path)

    expected = {
        "ux_active_version_per_channel",
        "ix_channel_prices_lookup",
        "ix_channel_prices_version",
        "ix_import_tasks_status_created",
        "ix_price_versions_channel_sha256",
        "ux_import_tasks_version",
        "ix_version_status_events_version_created",
        "ix_version_status_events_channel_created",
    }
    with database_session(database_path) as connection:
        actual = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert expected.issubset(actual)


def test_adjustment_mode_constraint_and_v1_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    connection = open_database(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE channel_prices (
                id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL,
                country_code TEXT NOT NULL,
                usd_tier NUMERIC NOT NULL,
                currency TEXT NOT NULL,
                local_price NUMERIC NOT NULL,
                version_id TEXT NOT NULL,
                created_time TEXT NOT NULL
            );
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price,
                 version_id, created_time)
            VALUES ('IOS', 'US', 0.99, 'USD', 0.99, 'OLD', '2026-08-17T00:00:00+08:00');
            PRAGMA user_version = 1;
            """
        )
    finally:
        connection.close()

    initialize_database(database_path)
    initialize_database(database_path)

    with database_session(database_path) as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(channel_prices)")}
        assert "adjustment_mode" in columns
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 4
        saved = migrated.execute("SELECT * FROM channel_prices").fetchone()
        assert saved["local_price"] == 0.99
        assert saved["adjustment_mode"] is None


def test_adjustment_mode_rejects_unknown_value(tmp_path: Path) -> None:
    database_path = tmp_path / "adjustment-constraint.db"
    initialize_database(database_path)
    _seed_price_dependencies(database_path)

    with pytest.raises(sqlite3.IntegrityError), database_session(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_prices
                (channel, country_code, usd_tier, currency, local_price,
                 adjustment_mode, version_id, created_time)
            VALUES ('GOOGLE', 'JP', 9.99, 'JPY', 1500, 'UNKNOWN',
                    'GOOGLE_V20260816_001', '2026-08-17T00:00:00+08:00')
            """
        )


def test_v2_import_task_migration_adds_version_link_atomically(tmp_path: Path) -> None:
    database_path = tmp_path / "v2-task-migration.db"
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
            CREATE TABLE import_tasks (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                created_time TEXT NOT NULL,
                completed_time TEXT,
                error_message TEXT
            );
            INSERT INTO import_tasks
                (task_id, channel, file_path, status, created_time)
            VALUES ('OLD-TASK', 'GOOGLE', 'old.xlsx', 'CHECKING',
                    '2026-08-17T00:00:00+08:00');
            PRAGMA user_version = 2;
            """
        )
    finally:
        connection.close()

    initialize_database(database_path)
    initialize_database(database_path)

    with database_session(database_path) as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(import_tasks)")}
        assert "version_id" in columns
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            migrated.execute(
                "SELECT version_id FROM import_tasks WHERE task_id = 'OLD-TASK'"
            ).fetchone()[0]
            is None
        )


def test_failed_schema_upgrade_rolls_back(tmp_path: Path) -> None:
    database_path = tmp_path / "broken-v1.db"
    connection = open_database(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE channel_prices (id INTEGER PRIMARY KEY);
            PRAGMA user_version = 1;
            """
        )
    finally:
        connection.close()

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(database_path)

    with database_session(database_path) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(channel_prices)")}
        assert columns == {"id"}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def _seed_price_dependencies(database_path: Path) -> None:
    with database_session(database_path) as connection:
        repository = ReferenceDataRepository(connection)
        repository.add_country(
            country_code="JP", name_cn="日本", name_en="Japan", default_currency="JPY"
        )
        repository.add_price_tier(Decimal("9.99"))
        _insert_version(connection, "GOOGLE_V20260816_001", "GOOGLE")


def _insert_version(connection: sqlite3.Connection, version_id: str, channel: str) -> None:
    connection.execute(
        """
        INSERT INTO price_versions
            (version_id, channel, source_file, import_time, status)
        VALUES (?, ?, 'sample.xlsx', '2026-08-16T23:00:00+08:00', 'ACTIVE')
        """,
        (version_id, channel),
    )
