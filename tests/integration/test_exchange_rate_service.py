from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError

import pytest

from src.database.connection import database_session
from src.database.schema import initialize_database
from src.models import ProviderRateBundle, RateFetchTrigger, RateRefreshStatus
from src.services import ExchangeRateService


class StaticRateProvider:
    def __init__(self, bundle: ProviderRateBundle) -> None:
        self.bundle = bundle

    def fetch_latest(self) -> ProviderRateBundle:
        return self.bundle


class FailingRateProvider:
    def fetch_latest(self) -> ProviderRateBundle:
        raise RuntimeError("network unavailable")


class ExceptionRateProvider:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def fetch_latest(self) -> ProviderRateBundle:
        raise self._error


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values: Iterator[datetime] = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def test_refresh_creates_one_snapshot_per_provider_update(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rates.db"
    initialize_database(database_path)
    bundle = ProviderRateBundle(
        provider="EXCHANGE_RATE_API",
        base_currency="USD",
        updated_at=datetime(2026, 8, 20, 0, 2, 31, tzinfo=UTC),
        next_update_at=datetime(2026, 8, 21, 0, 11, 41, tzinfo=UTC),
        rates={
            "USD": Decimal(1),
            "JPY": Decimal("147.123456"),
            "EUR": Decimal("0.867"),
        },
    )
    service = ExchangeRateService(database_path, provider=StaticRateProvider(bundle))
    requested_at = datetime(2026, 8, 20, 8, 20, tzinfo=UTC)

    first = service.refresh(RateFetchTrigger.MANUAL, requested_at=requested_at)
    second = service.refresh(RateFetchTrigger.AUTO, requested_at=requested_at)

    assert first.status is RateRefreshStatus.CREATED
    assert second.status is RateRefreshStatus.NOT_MODIFIED
    snapshots = service.list_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].snapshot_id == first.snapshot.snapshot_id
    assert snapshots[0].rates["JPY"] == Decimal("147.123456")
    assert snapshots[0].updated_at == bundle.updated_at


def test_refresh_records_actual_fetch_and_completion_times(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rate-audit.db"
    initialize_database(database_path)
    requested = datetime(2026, 8, 20, 8, 20, tzinfo=UTC)
    completed = requested + timedelta(seconds=4)
    bundle = ProviderRateBundle(
        provider="EXCHANGE_RATE_API",
        base_currency="USD",
        updated_at=datetime(2026, 8, 20, 0, 2, 31, tzinfo=UTC),
        next_update_at=datetime(2026, 8, 21, 0, 11, 41, tzinfo=UTC),
        rates={"USD": Decimal(1), "JPY": Decimal(150)},
    )
    service = ExchangeRateService(
        database_path,
        provider=StaticRateProvider(bundle),
        clock=SequenceClock(completed),
    )

    result = service.refresh(RateFetchTrigger.MANUAL, requested_at=requested)

    assert result.snapshot.fetched_at == completed
    with database_session(database_path) as connection:
        log = connection.execute(
            "SELECT requested_at, completed_at FROM exchange_rate_fetch_logs"
        ).fetchone()
    assert datetime.fromisoformat(log[0]) == requested
    assert datetime.fromisoformat(log[1]) == completed


def test_refresh_failure_returns_latest_cached_snapshot(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rates.db"
    initialize_database(database_path)
    bundle = ProviderRateBundle(
        provider="EXCHANGE_RATE_API",
        base_currency="USD",
        updated_at=datetime(2026, 8, 20, 0, 2, 31, tzinfo=UTC),
        next_update_at=datetime(2026, 8, 21, 0, 11, 41, tzinfo=UTC),
        rates={"USD": Decimal(1), "JPY": Decimal("147.123456")},
    )
    cached = (
        ExchangeRateService(database_path, provider=StaticRateProvider(bundle))
        .refresh(
            RateFetchTrigger.MANUAL,
            requested_at=datetime(2026, 8, 20, 8, 20, tzinfo=UTC),
        )
        .snapshot
    )

    result = ExchangeRateService(database_path, provider=FailingRateProvider()).refresh(
        RateFetchTrigger.AUTO,
        requested_at=datetime(2026, 8, 21, 1, 0, tzinfo=UTC),
    )

    assert result.status is RateRefreshStatus.FAILED
    assert result.snapshot == cached
    assert "network unavailable" in result.message


def test_refresh_failure_without_cache_is_explicit(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rates-no-cache.db"
    initialize_database(database_path)

    result = ExchangeRateService(database_path, provider=FailingRateProvider()).refresh(
        RateFetchTrigger.AUTO,
        requested_at=datetime(2026, 8, 21, 1, 0, tzinfo=UTC),
    )

    assert result.status is RateRefreshStatus.FAILED
    assert result.snapshot is None
    assert "network unavailable" in result.message


@pytest.mark.parametrize(
    ("bundle", "message"),
    [
        (
            ProviderRateBundle(
                provider="OTHER",
                base_currency="USD",
                updated_at=datetime(2026, 8, 20, tzinfo=UTC),
                next_update_at=datetime(2026, 8, 21, tzinfo=UTC),
                rates={"USD": Decimal(1)},
            ),
            "不支持的汇率供应商",
        ),
        (
            ProviderRateBundle(
                provider="EXCHANGE_RATE_API",
                base_currency="EUR",
                updated_at=datetime(2026, 8, 20, tzinfo=UTC),
                next_update_at=datetime(2026, 8, 21, tzinfo=UTC),
                rates={"USD": Decimal(1)},
            ),
            "基准币种必须为 USD",
        ),
        (
            ProviderRateBundle(
                provider="EXCHANGE_RATE_API",
                base_currency="USD",
                updated_at=datetime(2026, 8, 20),  # noqa: DTZ001 - intentional invalid input
                next_update_at=datetime(2026, 8, 21, tzinfo=UTC),
                rates={"USD": Decimal(1)},
            ),
            "必须包含时区",
        ),
        (
            ProviderRateBundle(
                provider="EXCHANGE_RATE_API",
                base_currency="USD",
                updated_at=datetime(2026, 8, 20, tzinfo=UTC),
                next_update_at=datetime(2026, 8, 21, tzinfo=UTC),
                rates={"USD": Decimal(1), "JPY": Decimal(0)},
            ),
            "汇率必须是正数",
        ),
    ],
)
def test_invalid_provider_data_is_logged_as_failed_refresh(
    tmp_path: Path,
    bundle: ProviderRateBundle,
    message: str,
) -> None:
    database_path = tmp_path / "invalid-provider-data.db"
    initialize_database(database_path)

    result = ExchangeRateService(database_path, provider=StaticRateProvider(bundle)).refresh(
        RateFetchTrigger.AUTO,
        requested_at=datetime(2026, 8, 21, 1, 0, tzinfo=UTC),
    )

    assert result.status is RateRefreshStatus.FAILED
    assert result.snapshot is None
    assert message in result.message


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            TimeoutError("request timed out"),
            "timed out",
        ),
        (
            HTTPError("https://example.test", 503, "Service Unavailable", None, None),
            "503",
        ),
        (
            HTTPError("https://example.test", 429, "Too Many Requests", None, None),
            "429",
        ),
    ],
)
def test_transport_timeout_http_error_and_rate_limit_are_logged_failures(
    tmp_path: Path,
    error: Exception,
    message: str,
) -> None:
    database_path = tmp_path / f"transport-{message.replace(' ', '-')}.db"
    initialize_database(database_path)
    service = ExchangeRateService(database_path, provider=ExceptionRateProvider(error))

    result = service.refresh(
        RateFetchTrigger.AUTO,
        requested_at=datetime(2026, 8, 21, 1, 0, tzinfo=UTC),
    )

    assert result.status is RateRefreshStatus.FAILED
    assert result.snapshot is None
    assert message in result.message
    with database_session(database_path) as connection:
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM exchange_rate_snapshots"
        ).fetchone()[0]
        failed_count = connection.execute(
            "SELECT COUNT(*) FROM exchange_rate_fetch_logs WHERE status = 'FAILED'"
        ).fetchone()[0]
    assert snapshot_count == 0
    assert failed_count == 1


def test_refresh_due_uses_provider_next_update_plus_ten_minutes(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rates.db"
    initialize_database(database_path)
    bundle = ProviderRateBundle(
        provider="EXCHANGE_RATE_API",
        base_currency="USD",
        updated_at=datetime(2026, 8, 20, 0, 2, 31, tzinfo=UTC),
        next_update_at=datetime(2026, 8, 21, 0, 11, 41, tzinfo=UTC),
        rates={"USD": Decimal(1), "JPY": Decimal("147.123456")},
    )
    service = ExchangeRateService(database_path, provider=StaticRateProvider(bundle))

    assert service.is_refresh_due(bundle.next_update_at) is True
    service.refresh(
        RateFetchTrigger.AUTO,
        requested_at=datetime(2026, 8, 20, 8, 20, tzinfo=UTC),
    )
    assert service.is_refresh_due(bundle.next_update_at + timedelta(minutes=9, seconds=59)) is False
    assert service.is_refresh_due(bundle.next_update_at + timedelta(minutes=10)) is True


def test_failed_fetch_logs_can_be_pruned_after_ninety_days(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rates.db"
    initialize_database(database_path)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    bundle = ProviderRateBundle(
        provider="EXCHANGE_RATE_API",
        base_currency="USD",
        updated_at=datetime(2026, 5, 20, tzinfo=UTC),
        next_update_at=datetime(2026, 5, 21, tzinfo=UTC),
        rates={"USD": Decimal(1), "JPY": Decimal(150)},
    )
    success_service = ExchangeRateService(database_path, provider=StaticRateProvider(bundle))
    snapshot_before = success_service.refresh(
        RateFetchTrigger.AUTO,
        requested_at=now - timedelta(days=92),
    ).snapshot
    service = ExchangeRateService(database_path, provider=FailingRateProvider())
    service.refresh(RateFetchTrigger.AUTO, requested_at=now - timedelta(days=91))
    service.refresh(RateFetchTrigger.MANUAL, requested_at=now - timedelta(days=89))

    assert service.prune_failed_logs(now=now) == 1
    assert service.prune_failed_logs(now=now) == 0
    assert service.list_snapshots() == (snapshot_before,)


def test_snapshot_history_is_loaded_in_pages(tmp_path: Path) -> None:
    database_path = tmp_path / "exchange-rates.db"
    initialize_database(database_path)
    provider = StaticRateProvider(
        ProviderRateBundle(
            provider="EXCHANGE_RATE_API",
            base_currency="USD",
            updated_at=datetime(2026, 8, 20, tzinfo=UTC),
            next_update_at=datetime(2026, 8, 21, tzinfo=UTC),
            rates={"USD": Decimal(1), "JPY": Decimal(147)},
        )
    )
    service = ExchangeRateService(database_path, provider=provider)
    for day in range(20, 23):
        provider.bundle = ProviderRateBundle(
            provider="EXCHANGE_RATE_API",
            base_currency="USD",
            updated_at=datetime(2026, 8, day, tzinfo=UTC),
            next_update_at=datetime(2026, 8, day + 1, tzinfo=UTC),
            rates={"USD": Decimal(1), "JPY": Decimal(str(127 + day))},
        )
        service.refresh(
            RateFetchTrigger.AUTO,
            requested_at=datetime(2026, 8, day, 8, tzinfo=UTC),
        )

    first_page = service.list_snapshots(limit=2)
    second_page = service.list_snapshots(limit=2, offset=2)

    assert [snapshot.updated_at.day for snapshot in first_page] == [22, 21]
    assert [snapshot.updated_at.day for snapshot in second_page] == [20]
