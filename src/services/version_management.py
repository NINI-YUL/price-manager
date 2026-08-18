"""P1-009 version catalog, archive inspection, and atomic activation."""

from __future__ import annotations

import sqlite3
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from src.config.settings import DATABASE_PATH, RUNTIME_PATHS
from src.database.connection import DatabasePath, open_database
from src.database.repositories.version_management import VersionManagementRepository
from src.models import (
    ArchiveInspection,
    ArchiveStatus,
    Channel,
    VersionActivationAssessment,
    VersionActivationResult,
    VersionDetail,
    VersionEventReason,
    VersionManagementError,
    VersionStatus,
)
from src.utils.source_hash import file_sha256


class VersionManagementService:
    def __init__(
        self,
        database_path: DatabasePath = DATABASE_PATH,
        *,
        archives_root: str | Path = RUNTIME_PATHS.archives,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = database_path
        self._archives_root = Path(archives_root).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now().astimezone())

    def list_versions(self) -> tuple[VersionDetail, ...]:
        try:
            connection = open_database(self._database_path)
            connection.execute("PRAGMA query_only = ON")
            try:
                repository = VersionManagementRepository(connection)
                return tuple(
                    VersionDetail(
                        summary=summary,
                        archive=self._inspect(summary),
                        events=(),
                    )
                    for summary in repository.list_summaries()
                )
            finally:
                connection.close()
        except VersionManagementError:
            raise
        except Exception as error:
            raise VersionManagementError("V001", f"无法读取版本列表：{error}") from error

    def get_detail(self, version_id: str) -> VersionDetail:
        try:
            connection = open_database(self._database_path)
            connection.execute("PRAGMA query_only = ON")
            try:
                repository = VersionManagementRepository(connection)
                summary = repository.get_summary(version_id)
                if summary is None:
                    raise VersionManagementError("V002", f"版本 {version_id} 不存在")
                events = repository.list_events(version_id)
            finally:
                connection.close()
            return VersionDetail(
                summary=summary,
                archive=self._inspect(summary),
                events=events,
            )
        except VersionManagementError:
            raise
        except Exception as error:
            raise VersionManagementError("V001", f"无法读取版本详情：{error}") from error

    def assess_activation(self, version_id: str) -> VersionActivationAssessment:
        try:
            connection = open_database(self._database_path)
            connection.execute("PRAGMA query_only = ON")
            try:
                repository = VersionManagementRepository(connection)
                target = repository.get_summary(version_id)
                if target is None:
                    raise VersionManagementError("V002", f"版本 {version_id} 不存在")
                if target.status is VersionStatus.ACTIVE:
                    raise VersionManagementError("V003", f"版本 {version_id} 已经生效")
                current = repository.get_active(target.channel)
            finally:
                connection.close()
            return VersionActivationAssessment(
                current=current,
                target=target,
                archive=self._inspect(target),
            )
        except VersionManagementError:
            raise
        except Exception as error:
            raise VersionManagementError("V001", f"无法评估版本切换：{error}") from error

    def activate(self, version_id: str, *, note: str = "") -> VersionActivationResult:
        clean_note = note.strip()
        if len(clean_note) > 200:
            raise VersionManagementError("V004", "操作备注不能超过 200 个字符")
        completed = self._clock().astimezone()
        completed_time = completed.isoformat()
        connection: sqlite3.Connection | None = None
        try:
            connection = open_database(self._database_path)
            connection.execute("BEGIN IMMEDIATE")
            repository = VersionManagementRepository(connection)
            target = repository.get_summary(version_id)
            if target is None:
                raise VersionManagementError("V002", f"版本 {version_id} 不存在")
            if target.status is VersionStatus.ACTIVE:
                raise VersionManagementError("V003", f"版本 {version_id} 已经生效")
            current = repository.get_active(target.channel)
            current_id = current.version_id if current is not None else None
            if current is not None:
                repository.set_status(current.version_id, VersionStatus.ARCHIVED)
            repository.set_status(target.version_id, VersionStatus.ACTIVE)
            if current is not None:
                repository.add_event(
                    event_id=_event_id(),
                    version_id=current.version_id,
                    channel=current.channel,
                    from_status=VersionStatus.ACTIVE,
                    to_status=VersionStatus.ARCHIVED,
                    replaced_version_id=target.version_id,
                    reason=VersionEventReason.MANUAL_ACTIVATION,
                    note=clean_note or None,
                    actor="LOCAL_USER",
                    created_time=completed_time,
                )
            repository.add_event(
                event_id=_event_id(),
                version_id=target.version_id,
                channel=target.channel,
                from_status=VersionStatus.ARCHIVED,
                to_status=VersionStatus.ACTIVE,
                replaced_version_id=current_id,
                reason=VersionEventReason.MANUAL_ACTIVATION,
                note=clean_note or None,
                actor="LOCAL_USER",
                created_time=completed_time,
            )
            connection.commit()
            return VersionActivationResult(
                channel=target.channel,
                activated_version_id=target.version_id,
                archived_version_id=current_id,
                completed_time=completed_time,
            )
        except VersionManagementError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except Exception as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise VersionManagementError("V005", f"版本切换失败，未产生状态变化：{error}") from error
        finally:
            if connection is not None:
                connection.close()

    def archive_folder(self, version_id: str) -> Path:
        detail = self.get_detail(version_id)
        folder = detail.archive.path.parent
        if not folder.is_dir():
            raise VersionManagementError("V006", "归档目录不存在")
        return folder

    def _inspect(self, summary) -> ArchiveInspection:
        path = (self._archives_root / summary.source_file).resolve()
        try:
            path.relative_to(self._archives_root)
        except ValueError:
            return ArchiveInspection(
                summary.version_id,
                ArchiveStatus.UNREADABLE,
                path,
                None,
                None,
                "归档路径超出配置目录，已拒绝访问",
            )
        if not path.is_file():
            return ArchiveInspection(
                summary.version_id,
                ArchiveStatus.MISSING,
                path,
                None,
                None,
                "归档文件不存在",
            )
        try:
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).astimezone()
            if summary.channel is Channel.IOS:
                with zipfile.ZipFile(path) as archive:
                    bad_file = archive.testzip()
                if bad_file is not None:
                    return ArchiveInspection(
                        summary.version_id,
                        ArchiveStatus.DAMAGED,
                        path,
                        stat.st_size,
                        modified,
                        f"ZIP 在 {bad_file} 处损坏",
                    )
                detail = "ZIP 完整；数据库摘要为原始目录摘要，不与 ZIP 摘要直接比较"
                status = ArchiveStatus.COMPLETE
            elif summary.source_sha256 is None:
                detail = "数据库缺少来源 SHA-256，无法完成校验"
                status = ArchiveStatus.UNREADABLE
            elif file_sha256(path) != summary.source_sha256:
                detail = "归档文件 SHA-256 与导入来源摘要不一致"
                status = ArchiveStatus.DIGEST_MISMATCH
            else:
                detail = "归档文件 SHA-256 与导入来源摘要一致"
                status = ArchiveStatus.COMPLETE
            return ArchiveInspection(
                summary.version_id,
                status,
                path,
                stat.st_size,
                modified,
                detail,
            )
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            status = (
                ArchiveStatus.DAMAGED
                if summary.channel is Channel.IOS
                else ArchiveStatus.UNREADABLE
            )
            return ArchiveInspection(
                summary.version_id,
                status,
                path,
                None,
                None,
                f"归档校验失败：{error}",
            )


def _event_id() -> str:
    return f"EVT_{uuid.uuid4().hex.upper()}"
