"""Persistence for immutable Phase2 exchange-rate snapshots and fetch logs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from src.models import ExchangeRateSnapshot, RateFetchTrigger, RateRefreshStatus


class ExchangeRateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find_snapshot(
        self,
        *,
        provider: str,
        base_currency: str,
        updated_at: datetime,
    ) -> ExchangeRateSnapshot | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM exchange_rate_snapshots
            WHERE provider = ? AND base_currency = ? AND provider_updated_at = ?
            """,
            (provider, base_currency, updated_at.isoformat()),
        ).fetchone()
        return None if row is None else self._snapshot(row)

    def get_snapshot(self, snapshot_id: str) -> ExchangeRateSnapshot | None:
        row = self._connection.execute(
            "SELECT * FROM exchange_rate_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return None if row is None else self._snapshot(row)

    def add_snapshot(self, snapshot: ExchangeRateSnapshot) -> None:
        rates_json = json.dumps(
            {code: str(rate) for code, rate in sorted(snapshot.rates.items())},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self._connection.execute(
            """
            INSERT INTO exchange_rate_snapshots
                (snapshot_id, provider, base_currency, provider_updated_at,
                 provider_next_update_at, fetched_at, rates_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.provider,
                snapshot.base_currency,
                snapshot.updated_at.isoformat(),
                snapshot.next_update_at.isoformat(),
                snapshot.fetched_at.isoformat(),
                rates_json,
            ),
        )

    def list_snapshots(self, *, limit: int, offset: int) -> tuple[ExchangeRateSnapshot, ...]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM exchange_rate_snapshots
            ORDER BY provider_updated_at DESC, snapshot_id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return tuple(self._snapshot(row) for row in rows)

    def latest_snapshot(self) -> ExchangeRateSnapshot | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM exchange_rate_snapshots
            ORDER BY provider_updated_at DESC, snapshot_id DESC
            LIMIT 1
            """
        ).fetchone()
        return None if row is None else self._snapshot(row)

    def add_fetch_log(
        self,
        *,
        log_id: str,
        trigger: RateFetchTrigger,
        status: RateRefreshStatus,
        requested_at: datetime,
        completed_at: datetime,
        error_message: str | None,
        snapshot_id: str | None,
    ) -> None:
        database_status = "SUCCESS" if status is RateRefreshStatus.CREATED else status.value
        self._connection.execute(
            """
            INSERT INTO exchange_rate_fetch_logs
                (log_id, trigger, status, requested_at, completed_at,
                 error_message, snapshot_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                trigger.value,
                database_status,
                requested_at.isoformat(),
                completed_at.isoformat(),
                error_message,
                snapshot_id,
            ),
        )

    def delete_failed_logs_before(self, cutoff: datetime) -> int:
        cursor = self._connection.execute(
            "DELETE FROM exchange_rate_fetch_logs WHERE status = 'FAILED' AND requested_at < ?",
            (cutoff.isoformat(),),
        )
        return cursor.rowcount

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> ExchangeRateSnapshot:

        raw_rates = json.loads(str(row["rates_json"]))
        return ExchangeRateSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            provider=str(row["provider"]),
            base_currency=str(row["base_currency"]),
            updated_at=datetime.fromisoformat(str(row["provider_updated_at"])),
            next_update_at=datetime.fromisoformat(str(row["provider_next_update_at"])),
            fetched_at=datetime.fromisoformat(str(row["fetched_at"])),
            rates={code: Decimal(str(rate)) for code, rate in raw_rates.items()},
        )
