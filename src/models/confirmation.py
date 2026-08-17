"""Immutable results and guard errors for P1-007 confirmation."""

from __future__ import annotations

from dataclasses import dataclass

from src.models.import_preview import Channel


class ConfirmationError(RuntimeError):
    def __init__(self, code: str, message: str, *, existing_version_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.existing_version_id = existing_version_id


@dataclass(frozen=True, slots=True)
class ConfirmationAssessment:
    task_id: str
    channel: Channel
    warning_count: int
    previous_version_id: str | None
    previous_country_count: int
    previous_record_count: int
    new_country_count: int
    new_record_count: int

    @property
    def coverage_reduced(self) -> bool:
        if self.previous_version_id is None:
            return False
        return (
            self.new_country_count < self.previous_country_count
            or self.new_record_count < self.previous_record_count
        )


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    task_id: str
    channel: Channel
    version_id: str
    archived_version_id: str | None
    record_count: int
    country_count: int
    archive_path: str
    completed_time: str
