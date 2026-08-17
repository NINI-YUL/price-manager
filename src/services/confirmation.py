"""P1-007 gates, archive preparation, and atomic version confirmation."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from src.config.settings import DATABASE_PATH, RUNTIME_PATHS
from src.database.connection import DatabasePath, database_session, open_database
from src.database.repositories import ImportTaskRepository, PriceVersionRepository
from src.models import (
    ConfirmationAssessment,
    ConfirmationError,
    ConfirmationResult,
    ImportPreview,
    ImportTaskStatus,
)
from src.services.source_archive import PreparedArchive, SourceArchiver


class ImportConfirmationService:
    def __init__(
        self,
        database_path: DatabasePath = DATABASE_PATH,
        *,
        archives_root: str | Path = RUNTIME_PATHS.archives,
        clock: Callable[[], datetime] | None = None,
        archiver: SourceArchiver | None = None,
    ) -> None:
        self._database_path = database_path
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._archiver = archiver or SourceArchiver(archives_root)

    def assess(self, preview: ImportPreview, *, task_id: str) -> ConfirmationAssessment:
        self._validate_preview(preview)
        with database_session(self._database_path) as connection:
            self._validate_task(connection, preview, task_id)
            versions = PriceVersionRepository(connection)
            duplicate = versions.find_by_source_sha256(preview.channel, str(preview.source_sha256))
            if duplicate is not None:
                version_id = str(duplicate["version_id"])
                raise ConfirmationError(
                    "C005",
                    f"the same source was already imported as {version_id}",
                    existing_version_id=version_id,
                )
            previous_version, previous_countries, previous_records = versions.active_coverage(
                preview.channel
            )
        return ConfirmationAssessment(
            task_id=task_id,
            channel=preview.channel,
            warning_count=preview.statistics.warning_count,
            previous_version_id=previous_version,
            previous_country_count=previous_countries,
            previous_record_count=previous_records,
            new_country_count=len({record.country_code for record in preview.records}),
            new_record_count=len(preview.records),
        )

    def confirm(
        self,
        preview: ImportPreview,
        *,
        task_id: str,
        accept_warnings: bool = False,
        accept_coverage_reduction: bool = False,
    ) -> ConfirmationResult:
        assessment = self.assess(preview, task_id=task_id)
        if assessment.warning_count and not accept_warnings:
            raise ConfirmationError(
                "C003",
                f"{assessment.warning_count} warning(s) require explicit confirmation",
            )
        if assessment.coverage_reduced and not accept_coverage_reduction:
            raise ConfirmationError(
                "C004",
                "the new snapshot has fewer countries or records than the active version",
            )

        expected_sha256 = str(preview.source_sha256)
        prepared: PreparedArchive | None = None
        connection: sqlite3.Connection | None = None
        try:
            prepared = self._archiver.prepare(
                channel=preview.channel,
                source_path=preview.source_path,
                expected_sha256=expected_sha256,
            )
            completed = self._normalised_now()
            completed_time = completed.isoformat()
            connection = open_database(self._database_path)
            connection.execute("BEGIN IMMEDIATE")
            self._validate_task(connection, preview, task_id)
            versions = PriceVersionRepository(connection)

            duplicate = versions.find_by_source_sha256(preview.channel, expected_sha256)
            if duplicate is not None:
                version_id = str(duplicate["version_id"])
                raise ConfirmationError(
                    "C005",
                    f"the same source was already imported as {version_id}",
                    existing_version_id=version_id,
                )

            previous_version_id, previous_countries, previous_records = versions.active_coverage(
                preview.channel
            )
            coverage_reduced = previous_version_id is not None and (
                len({record.country_code for record in preview.records}) < previous_countries
                or len(preview.records) < previous_records
            )
            if coverage_reduced and not accept_coverage_reduction:
                raise ConfirmationError(
                    "C004",
                    "the active version changed and coverage reduction requires confirmation",
                )

            version_id = versions.next_version_id(preview.channel, completed.date())
            archived_relative_path = self._archiver.finalize(
                prepared,
                channel=preview.channel,
                version_id=version_id,
            )
            versions.archive_active(preview.channel)
            versions.create_active(
                version_id=version_id,
                channel=preview.channel,
                source_file=archived_relative_path,
                source_sha256=expected_sha256,
                import_time=completed_time,
                record_count=len(preview.records),
            )
            versions.synchronize_google_products(preview.records)
            inserted = versions.add_prices(
                version_id=version_id,
                records=preview.records,
                created_time=completed_time,
            )
            if inserted != len(preview.records):
                raise ConfirmationError("C008", "not all preview prices were inserted")
            saved_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM channel_prices WHERE version_id = ?",
                    (version_id,),
                ).fetchone()[0]
            )
            if saved_count != len(preview.records):
                raise ConfirmationError("C008", "saved price count does not match preview")
            ImportTaskRepository(connection).mark_success(
                task_id=task_id,
                version_id=version_id,
                completed_time=completed_time,
            )
            connection.commit()
        except ConfirmationError as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if prepared is not None:
                prepared.cleanup()
            if error.code not in {"C003", "C004"}:
                self._record_retryable_error(task_id, str(error))
            raise
        except Exception as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if prepared is not None:
                prepared.cleanup()
            wrapped = ConfirmationError("C008", f"confirmation transaction failed: {error}")
            self._record_retryable_error(task_id, str(wrapped))
            raise wrapped from error
        finally:
            if connection is not None:
                connection.close()

        with database_session(self._database_path) as verified:
            version = PriceVersionRepository(verified).get(version_id)
            task = ImportTaskRepository(verified).get(task_id)
            if version is None or task is None:
                raise ConfirmationError("C008", "confirmed version cannot be reloaded")
            if str(task["version_id"]) != version_id:
                raise ConfirmationError("C008", "confirmed task is not linked to its version")

        return ConfirmationResult(
            task_id=task_id,
            channel=preview.channel,
            version_id=version_id,
            archived_version_id=previous_version_id,
            record_count=len(preview.records),
            country_count=len({record.country_code for record in preview.records}),
            archive_path=archived_relative_path,
            completed_time=completed_time,
        )

    def list_orphaned_archives(self) -> tuple[str, ...]:
        root = self._archiver.archives_root
        if not root.is_dir():
            return ()
        with database_session(self._database_path) as connection:
            version_ids = {
                str(row["version_id"])
                for row in connection.execute("SELECT version_id FROM price_versions")
            }
        orphans: list[str] = []
        for channel in ("google", "ios", "web"):
            channel_root = root / channel
            if not channel_root.is_dir():
                continue
            for directory in channel_root.iterdir():
                if directory.is_dir() and directory.name not in version_ids:
                    orphans.append(directory.relative_to(root).as_posix())
        return tuple(sorted(orphans))

    def _validate_preview(self, preview: ImportPreview) -> None:
        if preview.status is not ImportTaskStatus.CHECKING:
            raise ConfirmationError("C001", "only a CHECKING preview can be confirmed")
        if preview.source_sha256 is None:
            raise ConfirmationError("C001", "preview source digest is missing")
        if preview.has_blocking_errors:
            raise ConfirmationError("C002", "preview contains blocking errors")
        if not preview.records:
            raise ConfirmationError("C002", "preview has no accepted price records")
        if any(record.channel is not preview.channel for record in preview.records):
            raise ConfirmationError("C001", "preview contains records from another channel")
        keys = [record.natural_key for record in preview.records]
        if len(keys) != len(set(keys)):
            raise ConfirmationError("C002", "preview contains duplicate standard price keys")

    def _validate_task(
        self,
        connection: sqlite3.Connection,
        preview: ImportPreview,
        task_id: str,
    ) -> None:
        task = ImportTaskRepository(connection).get(task_id)
        if task is None:
            raise ConfirmationError("C001", f"import task {task_id!r} does not exist")
        if str(task["status"]) != ImportTaskStatus.CHECKING.value:
            raise ConfirmationError("C001", f"import task {task_id!r} is not CHECKING")
        if str(task["channel"]) != preview.channel.value:
            raise ConfirmationError("C001", "preview channel does not match import task")
        task_path = os.path.normcase(str(Path(str(task["file_path"])).expanduser().resolve()))
        preview_path = os.path.normcase(str(Path(preview.source_path).expanduser().resolve()))
        if task_path != preview_path:
            raise ConfirmationError("C001", "preview source path does not match import task")
        if task["version_id"] is not None:
            raise ConfirmationError("C001", "import task is already linked to a version")

    def _normalised_now(self) -> datetime:
        return self._clock().astimezone()

    def _record_retryable_error(self, task_id: str, message: str) -> None:
        try:
            with database_session(self._database_path) as connection:
                ImportTaskRepository(connection).set_retryable_error(
                    task_id=task_id,
                    error_message=message,
                )
        except (LookupError, sqlite3.Error):
            pass
