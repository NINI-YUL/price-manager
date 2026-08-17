"""Coordinate a third-party web price preview and import-task lifecycle."""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.adapters import WebAdapterConfig, WebPriceAdapter
from src.config.settings import DATABASE_PATH
from src.database.connection import DatabasePath, database_session
from src.database.repositories import ImportTaskRepository, ReferenceDataRepository
from src.database.seed import SEEDS_DIR
from src.models import Channel, ImportPreview, ImportTaskStatus, IssueSeverity

WEB_COUNTRY_ALIASES_PATH = SEEDS_DIR / "web_country_aliases.csv"


class WebImportService:
    def __init__(
        self,
        database_path: DatabasePath = DATABASE_PATH,
        *,
        aliases_path: Path = WEB_COUNTRY_ALIASES_PATH,
        max_file_size_bytes: int = 50 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = database_path
        self._aliases_path = aliases_path
        self._max_file_size_bytes = max_file_size_bytes
        self._clock = clock or (lambda: datetime.now().astimezone())

    def preview(self, file_path: str | Path, *, task_id: str) -> ImportPreview:
        source_path = str(Path(file_path).expanduser().resolve())
        config = self._create_task_and_load_config(source_path, task_id)
        adapter = WebPriceAdapter(config)
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
                (issue.message for issue in result.issues if issue.severity is IssueSeverity.ERROR),
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

    def _create_task_and_load_config(self, source_path: str, task_id: str) -> WebAdapterConfig:
        with database_session(self._database_path) as connection:
            reference = ReferenceDataRepository(connection)
            countries = reference.list_countries()
            tiers = reference.list_price_tiers()
            if not countries or not tiers:
                raise RuntimeError("P1-003 reference data must be initialized before web import")
            country_codes = {str(row["country_code"]) for row in countries}
            country_names = {str(row["name_cn"]): str(row["country_code"]) for row in countries}
            country_names.update(load_web_country_aliases(self._aliases_path, country_codes))
            ImportTaskRepository(connection).create(
                task_id=task_id,
                channel=Channel.WEB,
                file_path=source_path,
                created_time=self._clock().isoformat(),
            )

        return WebAdapterConfig(
            country_names=country_names,
            supported_currency_codes=frozenset(str(row["default_currency"]) for row in countries),
            configured_tiers=frozenset(Decimal(str(row["usd_price"])) for row in tiers),
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
                error_message=f"unexpected web import failure: {error}",
            )


def load_web_country_aliases(path: Path, supported_country_codes: set[str]) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ("alias", "country_code"):
            raise ValueError("web country alias headers must be alias,country_code")
        aliases: dict[str, str] = {}
        for row_number, row in enumerate(reader, start=2):
            alias = (row["alias"] or "").strip()
            country_code = (row["country_code"] or "").strip().upper()
            if not alias or country_code not in supported_country_codes:
                raise ValueError(f"invalid web country alias at row {row_number}")
            if alias in aliases:
                raise ValueError(f"duplicate web country alias {alias!r}")
            aliases[alias] = country_code
    return aliases
