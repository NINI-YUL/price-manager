"""Domain models for P1-009 version management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from src.models.import_preview import Channel
from src.models.price_library import VersionStatus


class ArchiveStatus(StrEnum):
    COMPLETE = "COMPLETE"
    MISSING = "MISSING"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    DAMAGED = "DAMAGED"
    UNREADABLE = "UNREADABLE"


class VersionEventReason(StrEnum):
    IMPORT_CONFIRMATION = "IMPORT_CONFIRMATION"
    MANUAL_ACTIVATION = "MANUAL_ACTIVATION"
    MIGRATION_BASELINE = "MIGRATION_BASELINE"


@dataclass(frozen=True, slots=True)
class VersionSummary:
    version_id: str
    channel: Channel
    source_file: str
    source_sha256: str | None
    import_time: datetime
    status: VersionStatus
    record_count: int
    country_count: int
    currency_count: int
    tier_count: int
    task_id: str | None
    task_status: str | None


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    version_id: str
    status: ArchiveStatus
    path: Path
    size_bytes: int | None
    modified_time: datetime | None
    detail: str

    @property
    def has_issue(self) -> bool:
        return self.status is not ArchiveStatus.COMPLETE


@dataclass(frozen=True, slots=True)
class VersionStatusEvent:
    event_id: str
    version_id: str
    channel: Channel
    from_status: VersionStatus | None
    to_status: VersionStatus
    replaced_version_id: str | None
    reason: VersionEventReason
    note: str | None
    actor: str
    created_time: datetime


@dataclass(frozen=True, slots=True)
class VersionDetail:
    summary: VersionSummary
    archive: ArchiveInspection
    events: tuple[VersionStatusEvent, ...]


@dataclass(frozen=True, slots=True)
class VersionActivationAssessment:
    current: VersionSummary | None
    target: VersionSummary
    archive: ArchiveInspection


@dataclass(frozen=True, slots=True)
class VersionActivationResult:
    channel: Channel
    activated_version_id: str
    archived_version_id: str | None
    completed_time: str


class VersionManagementError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
