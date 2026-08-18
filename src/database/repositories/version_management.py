"""Persistence operations for P1-009 version management."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from src.models import (
    Channel,
    VersionEventReason,
    VersionStatus,
    VersionStatusEvent,
    VersionSummary,
)


class VersionManagementRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def list_summaries(self) -> tuple[VersionSummary, ...]:
        rows = self._connection.execute(
            self._summary_sql()
            + """
            ORDER BY CASE v.status WHEN 'ACTIVE' THEN 0 ELSE 1 END,
                     v.import_time DESC,
                     v.version_id DESC
            """
        )
        return tuple(self._summary(row) for row in rows)

    def get_summary(self, version_id: str) -> VersionSummary | None:
        row = self._connection.execute(
            self._summary_sql("WHERE v.version_id = ?"),
            (version_id,),
        ).fetchone()
        return None if row is None else self._summary(row)

    def get_active(self, channel: Channel) -> VersionSummary | None:
        row = self._connection.execute(
            self._summary_sql("WHERE v.channel = ? AND v.status = 'ACTIVE'"),
            (channel.value,),
        ).fetchone()
        return None if row is None else self._summary(row)

    def list_events(self, version_id: str) -> tuple[VersionStatusEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT event_id, version_id, channel, from_status, to_status,
                   replaced_version_id, reason, note, actor, created_time
            FROM version_status_events
            WHERE version_id = ?
            ORDER BY created_time DESC, id DESC
            """,
            (version_id,),
        )
        return tuple(self._event(row) for row in rows)

    def set_status(self, version_id: str, status: VersionStatus) -> None:
        cursor = self._connection.execute(
            "UPDATE price_versions SET status = ? WHERE version_id = ?",
            (status.value, version_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"version {version_id!r} does not exist")

    def add_event(
        self,
        *,
        event_id: str,
        version_id: str,
        channel: Channel,
        from_status: VersionStatus | None,
        to_status: VersionStatus,
        replaced_version_id: str | None,
        reason: VersionEventReason,
        note: str | None,
        actor: str,
        created_time: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO version_status_events
                (event_id, version_id, channel, from_status, to_status,
                 replaced_version_id, reason, note, actor, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                version_id,
                channel.value,
                from_status.value if from_status is not None else None,
                to_status.value,
                replaced_version_id,
                reason.value,
                note,
                actor,
                created_time,
            ),
        )

    @staticmethod
    def _summary_sql(where: str = "") -> str:
        return (
            """
            SELECT v.version_id, v.channel, v.source_file, v.source_sha256,
                   v.import_time, v.status, v.record_count,
                   COUNT(DISTINCT p.country_code) AS country_count,
                   COUNT(DISTINCT p.currency) AS currency_count,
                   COUNT(DISTINCT p.usd_tier) AS tier_count,
                   t.task_id, t.status AS task_status
            FROM price_versions AS v
            LEFT JOIN channel_prices AS p
              ON p.version_id = v.version_id AND p.channel = v.channel
            LEFT JOIN import_tasks AS t ON t.version_id = v.version_id
            """
            + where
            + """
            GROUP BY v.version_id, v.channel, v.source_file, v.source_sha256,
                     v.import_time, v.status, v.record_count, t.task_id, t.status
            """
        )

    @staticmethod
    def _summary(row: sqlite3.Row) -> VersionSummary:
        return VersionSummary(
            version_id=str(row["version_id"]),
            channel=Channel(str(row["channel"])),
            source_file=str(row["source_file"]),
            source_sha256=(
                str(row["source_sha256"]) if row["source_sha256"] is not None else None
            ),
            import_time=_parse_time(str(row["import_time"])),
            status=VersionStatus(str(row["status"])),
            record_count=int(row["record_count"]),
            country_count=int(row["country_count"]),
            currency_count=int(row["currency_count"]),
            tier_count=int(row["tier_count"]),
            task_id=str(row["task_id"]) if row["task_id"] is not None else None,
            task_status=(
                str(row["task_status"]) if row["task_status"] is not None else None
            ),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> VersionStatusEvent:
        from_status = row["from_status"]
        return VersionStatusEvent(
            event_id=str(row["event_id"]),
            version_id=str(row["version_id"]),
            channel=Channel(str(row["channel"])),
            from_status=VersionStatus(str(from_status)) if from_status is not None else None,
            to_status=VersionStatus(str(row["to_status"])),
            replaced_version_id=(
                str(row["replaced_version_id"])
                if row["replaced_version_id"] is not None
                else None
            ),
            reason=VersionEventReason(str(row["reason"])),
            note=str(row["note"]) if row["note"] is not None else None,
            actor=str(row["actor"]),
            created_time=_parse_time(str(row["created_time"])),
        )


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid ISO 8601 database timestamp: {value!r}") from error
