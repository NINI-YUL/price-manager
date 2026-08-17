"""Persistence for the import-task lifecycle used by channel services."""

from __future__ import annotations

import sqlite3

from src.models import Channel, ImportTaskStatus


class ImportTaskRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(
        self,
        *,
        task_id: str,
        channel: Channel,
        file_path: str,
        created_time: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO import_tasks
                (task_id, channel, file_path, status, created_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                channel.value,
                file_path,
                ImportTaskStatus.PROCESSING.value,
                created_time,
            ),
        )

    def update_result(
        self,
        *,
        task_id: str,
        status: ImportTaskStatus,
        error_count: int,
        warning_count: int,
        completed_time: str | None,
        error_message: str | None,
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE import_tasks
            SET status = ?, error_count = ?, warning_count = ?,
                completed_time = ?, error_message = ?
            WHERE task_id = ?
            """,
            (
                status.value,
                error_count,
                warning_count,
                completed_time,
                error_message,
                task_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"import task {task_id!r} does not exist")

    def get(self, task_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM import_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
