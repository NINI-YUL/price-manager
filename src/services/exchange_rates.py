"""Deep module for Phase2 snapshot refresh, validation, deduplication, and fallback."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, database_session, open_database
from src.database.repositories import ExchangeRateRepository
from src.models import (
    ExchangeRateRefreshResult,
    ExchangeRateSnapshot,
    ProviderRateBundle,
    RateFetchTrigger,
    RateRefreshStatus,
)


class RateProvider(Protocol):
    def fetch_latest(self) -> ProviderRateBundle: ...


class ExchangeRateService:
    def __init__(
        self,
        database_path: DatabasePath = DATABASE_PATH,
        *,
        provider: RateProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = database_path
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(UTC))

    def refresh(
        self,
        trigger: RateFetchTrigger,
        *,
        requested_at: datetime | None = None,
    ) -> ExchangeRateRefreshResult:
        requested = requested_at or self._clock()
        self._require_aware(requested, "requested_at")
        requested = requested.astimezone(UTC)
        provider_succeeded = False
        try:
            bundle = self._validated(self._provider.fetch_latest())
            provider_succeeded = True
            completed = self._completed_at(requested)
            with database_session(self._database_path) as connection:
                repository = ExchangeRateRepository(connection)
                existing = repository.find_snapshot(
                    provider=bundle.provider,
                    base_currency=bundle.base_currency,
                    updated_at=bundle.updated_at,
                )
                if existing is None:
                    snapshot = ExchangeRateSnapshot(
                        snapshot_id=self._snapshot_id(bundle.updated_at),
                        provider=bundle.provider,
                        base_currency=bundle.base_currency,
                        updated_at=bundle.updated_at,
                        next_update_at=bundle.next_update_at,
                        fetched_at=completed,
                        rates=dict(bundle.rates),
                    )
                    repository.add_snapshot(snapshot)
                    status = RateRefreshStatus.CREATED
                    message = "已获取最新汇率"
                else:
                    snapshot = existing
                    status = RateRefreshStatus.NOT_MODIFIED
                    message = "当前已是最新汇率"
                repository.add_fetch_log(
                    log_id=self._log_id(),
                    trigger=trigger,
                    status=status,
                    requested_at=requested,
                    completed_at=completed,
                    error_message=None,
                    snapshot_id=snapshot.snapshot_id,
                )
            return ExchangeRateRefreshResult(status=status, snapshot=snapshot, message=message)
        except Exception as error:
            if provider_succeeded:
                raise
            completed = self._completed_at(requested)
            message = f"汇率获取失败：{error}"
            with database_session(self._database_path) as connection:
                repository = ExchangeRateRepository(connection)
                snapshot = repository.latest_snapshot()
                repository.add_fetch_log(
                    log_id=self._log_id(),
                    trigger=trigger,
                    status=RateRefreshStatus.FAILED,
                    requested_at=requested,
                    completed_at=completed,
                    error_message=message,
                    snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
                )
            return ExchangeRateRefreshResult(
                status=RateRefreshStatus.FAILED,
                snapshot=snapshot,
                message=message,
            )

    def _completed_at(self, requested_at: datetime) -> datetime:
        completed = self._clock()
        self._require_aware(completed, "completed_at")
        return max(requested_at, completed.astimezone(UTC))

    def list_snapshots(
        self, *, limit: int = 100, offset: int = 0
    ) -> tuple[ExchangeRateSnapshot, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        if offset < 0:
            raise ValueError("offset 不能为负数")
        connection = open_database(self._database_path)
        connection.execute("PRAGMA query_only = ON")
        try:
            return ExchangeRateRepository(connection).list_snapshots(limit=limit, offset=offset)
        finally:
            connection.close()

    def next_refresh_at(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(UTC)
        self._require_aware(current, "now")
        current = current.astimezone(UTC)
        connection = open_database(self._database_path)
        connection.execute("PRAGMA query_only = ON")
        try:
            snapshot = ExchangeRateRepository(connection).latest_snapshot()
        finally:
            connection.close()
        if snapshot is None:
            return current
        return (snapshot.next_update_at + timedelta(minutes=10)).astimezone(UTC)

    def is_refresh_due(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        self._require_aware(current, "now")
        return current.astimezone(UTC) >= self.next_refresh_at(current)

    def prune_failed_logs(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        self._require_aware(current, "now")
        cutoff = current.astimezone(UTC) - timedelta(days=90)
        with database_session(self._database_path) as connection:
            return ExchangeRateRepository(connection).delete_failed_logs_before(cutoff)

    @classmethod
    def _validated(cls, bundle: ProviderRateBundle) -> ProviderRateBundle:
        if bundle.provider != "EXCHANGE_RATE_API":
            raise ValueError("不支持的汇率供应商")
        if bundle.base_currency != "USD":
            raise ValueError("汇率基准币种必须为 USD")
        cls._require_aware(bundle.updated_at, "time_last_update_utc")
        cls._require_aware(bundle.next_update_at, "time_next_update_utc")
        if bundle.next_update_at <= bundle.updated_at:
            raise ValueError("下一更新时间必须晚于汇率生效时间")
        rates: dict[str, Decimal] = {}
        for code, raw_rate in bundle.rates.items():
            currency = str(code).strip().upper()
            rate = Decimal(str(raw_rate))
            if len(currency) != 3 or not currency.isalpha() or currency != str(code):
                raise ValueError(f"非法币种代码：{code}")
            if not rate.is_finite() or rate <= 0:
                raise ValueError(f"{currency} 汇率必须是正数")
            rates[currency] = rate
        if rates.get("USD") != Decimal(1):
            raise ValueError("USD 汇率必须为 1")
        return ProviderRateBundle(
            provider=bundle.provider,
            base_currency=bundle.base_currency,
            updated_at=bundle.updated_at,
            next_update_at=bundle.next_update_at,
            rates=rates,
        )

    @staticmethod
    def _require_aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} 必须包含时区")

    @staticmethod
    def _snapshot_id(updated_at: datetime) -> str:
        utc_time = updated_at.astimezone(UTC)
        return f"FX_USD_{utc_time:%Y%m%dT%H%M%SZ}"

    @staticmethod
    def _log_id() -> str:
        return f"FXLOG_{uuid.uuid4().hex.upper()}"
