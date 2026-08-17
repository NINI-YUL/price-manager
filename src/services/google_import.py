"""Coordinate a Google preview with its import-task lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.adapters import GoogleAdapterConfig, GooglePriceAdapter
from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, database_session
from src.database.repositories import ImportTaskRepository, ReferenceDataRepository
from src.models import Channel, ImportPreview, ImportTaskStatus, IssueSeverity


class GoogleImportService:
    def __init__(
        self,
        database_path: DatabasePath = DATABASE_PATH,
        *,
        country_aliases: Mapping[str, str] | None = None,
        max_file_size_bytes: int = 50 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = database_path
        self._country_aliases = dict(country_aliases or {})
        self._max_file_size_bytes = max_file_size_bytes
        self._clock = clock or (lambda: datetime.now().astimezone())

    def preview(self, file_path: str | Path, *, task_id: str) -> ImportPreview:
        source_path = str(Path(file_path).expanduser().resolve())
        config = self._create_task_and_load_config(source_path, task_id)
        adapter = GooglePriceAdapter(config)
        try:
            result = adapter.parse(source_path)
        except Exception as error:
            self._mark_unexpected_failure(task_id, error)
            raise

        completed_time = (
            self._clock().isoformat() if result.status is ImportTaskStatus.FAILED else None
        )
        error_message = (
            next(
                (
                    issue.message
                    for issue in result.issues
                    if issue.severity is IssueSeverity.ERROR
                ),
                None,
            )
            if result.status is ImportTaskStatus.FAILED
            else None
        )
        with database_session(self._database_path) as connection:
            ImportTaskRepository(connection).update_result(
                task_id=task_id,
                status=result.status,
                error_count=result.statistics.error_count,
                warning_count=result.statistics.warning_count,
                completed_time=completed_time,
                error_message=error_message,
            )
        return result

    def _create_task_and_load_config(
        self, source_path: str, task_id: str
    ) -> GoogleAdapterConfig:
        with database_session(self._database_path) as connection:
            reference = ReferenceDataRepository(connection)
            countries = reference.list_countries()
            tiers = reference.list_price_tiers()
            if not countries or not tiers:
                raise RuntimeError("P1-003 reference data must be initialized before Google import")
            products = reference.list_channel_products(Channel.GOOGLE.value)
            ImportTaskRepository(connection).create(
                task_id=task_id,
                channel=Channel.GOOGLE,
                file_path=source_path,
                created_time=self._clock().isoformat(),
            )

        return GoogleAdapterConfig(
            supported_country_codes=frozenset(str(row["country_code"]) for row in countries),
            supported_currency_codes=frozenset(
                str(row["default_currency"]) for row in countries
            ),
            configured_tiers=frozenset(Decimal(str(row["usd_price"])) for row in tiers),
            product_tiers={
                str(row["product_id"]): Decimal(str(row["usd_tier"])) for row in products
            },
            country_aliases=self._country_aliases,
            max_file_size_bytes=self._max_file_size_bytes,
        )

    def _mark_unexpected_failure(self, task_id: str, error: Exception) -> None:
        with database_session(self._database_path) as connection:
            ImportTaskRepository(connection).update_result(
                task_id=task_id,
                status=ImportTaskStatus.FAILED,
                error_count=1,
                warning_count=0,
                completed_time=self._clock().isoformat(),
                error_message=f"unexpected Google import failure: {error}",
            )
