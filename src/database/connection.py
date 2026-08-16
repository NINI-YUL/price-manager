"""SQLite connection lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DatabasePath = str | Path


def open_database(database_path: DatabasePath, *, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    """Open a configured SQLite connection; the caller owns and closes it."""

    database_name = str(database_path)
    if database_name != ":memory:":
        Path(database_name).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_name, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def database_session(database_path: DatabasePath) -> Iterator[sqlite3.Connection]:
    """Commit a successful unit of work, roll it back on error, and always close it."""

    connection = open_database(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
