"""Idempotent Phase1 SQLite schema initialization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, open_database

SCHEMA_VERSION = 1
BUSINESS_TABLES = frozenset(
    {
        "countries",
        "price_tiers",
        "channel_products",
        "channel_prices",
        "price_versions",
        "import_tasks",
    }
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY,
    country_code TEXT NOT NULL UNIQUE
        CHECK (length(country_code) = 2 AND country_code = upper(country_code)
               AND country_code GLOB '[A-Z][A-Z]'),
    name_cn TEXT NOT NULL CHECK (length(trim(name_cn)) > 0),
    name_en TEXT NOT NULL CHECK (length(trim(name_en)) > 0),
    default_currency TEXT NOT NULL
        CHECK (length(default_currency) = 3 AND default_currency = upper(default_currency)
               AND default_currency GLOB '[A-Z][A-Z][A-Z]')
);

CREATE TABLE IF NOT EXISTS price_tiers (
    id INTEGER PRIMARY KEY,
    usd_price NUMERIC NOT NULL UNIQUE CHECK (usd_price > 0)
);

CREATE TABLE IF NOT EXISTS channel_products (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('GOOGLE', 'IOS', 'WEB')),
    product_id TEXT NOT NULL CHECK (length(trim(product_id)) > 0),
    usd_tier NUMERIC NOT NULL,
    UNIQUE (channel, product_id),
    FOREIGN KEY (usd_tier) REFERENCES price_tiers(usd_price)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS price_versions (
    id INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE CHECK (length(trim(version_id)) > 0),
    channel TEXT NOT NULL CHECK (channel IN ('GOOGLE', 'IOS', 'WEB')),
    source_file TEXT NOT NULL CHECK (length(trim(source_file)) > 0),
    source_sha256 TEXT,
    import_time TEXT NOT NULL CHECK (length(trim(import_time)) > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    record_count INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    UNIQUE (version_id, channel)
);

CREATE TABLE IF NOT EXISTS channel_prices (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('GOOGLE', 'IOS', 'WEB')),
    country_code TEXT NOT NULL
        CHECK (length(country_code) = 2 AND country_code = upper(country_code)
               AND country_code GLOB '[A-Z][A-Z]'),
    usd_tier NUMERIC NOT NULL,
    currency TEXT NOT NULL
        CHECK (length(currency) = 3 AND currency = upper(currency)
               AND currency GLOB '[A-Z][A-Z][A-Z]'),
    local_price NUMERIC NOT NULL CHECK (local_price > 0),
    version_id TEXT NOT NULL,
    created_time TEXT NOT NULL CHECK (length(trim(created_time)) > 0),
    UNIQUE (version_id, channel, country_code, usd_tier, currency),
    FOREIGN KEY (country_code) REFERENCES countries(country_code)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (usd_tier) REFERENCES price_tiers(usd_price)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (version_id, channel) REFERENCES price_versions(version_id, channel)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS import_tasks (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE CHECK (length(trim(task_id)) > 0),
    channel TEXT NOT NULL CHECK (channel IN ('GOOGLE', 'IOS', 'WEB')),
    file_path TEXT NOT NULL CHECK (length(trim(file_path)) > 0),
    status TEXT NOT NULL CHECK (status IN ('PROCESSING', 'CHECKING', 'SUCCESS', 'FAILED')),
    error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    warning_count INTEGER NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
    created_time TEXT NOT NULL CHECK (length(trim(created_time)) > 0),
    completed_time TEXT,
    error_message TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_active_version_per_channel
ON price_versions(channel) WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS ix_channel_prices_lookup
ON channel_prices(channel, country_code, usd_tier);

CREATE INDEX IF NOT EXISTS ix_channel_prices_version
ON channel_prices(version_id);

CREATE INDEX IF NOT EXISTS ix_import_tasks_status_created
ON import_tasks(status, created_time);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the complete P1-002 schema atomically and idempotently."""

    try:
        connection.executescript(
            f"BEGIN IMMEDIATE;\n{SCHEMA_SQL}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def initialize_database(database_path: DatabasePath = DATABASE_PATH) -> Path:
    """Create or upgrade a database file and return its resolved path."""

    path = Path(database_path).expanduser().resolve()
    connection = open_database(path)
    try:
        initialize_schema(connection)
    finally:
        connection.close()
    return path


def list_business_tables(connection: sqlite3.Connection) -> set[str]:
    """Return only the six application tables, excluding SQLite internals."""

    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}
