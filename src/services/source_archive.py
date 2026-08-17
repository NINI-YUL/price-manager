"""Prepare and finalize verified, non-destructive source archives."""

from __future__ import annotations

import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from src.models import Channel, ConfirmationError
from src.utils.source_hash import file_sha256, ios_bundle_sha256


@dataclass(slots=True)
class PreparedArchive:
    pending_directory: Path
    archived_name: str
    final_directory: Path | None = None

    def cleanup(self) -> None:
        targets = (self.final_directory, self.pending_directory)
        for target in targets:
            if target is not None and target.exists():
                try:
                    shutil.rmtree(target)
                except OSError:
                    pass


class SourceArchiver:
    def __init__(self, archives_root: str | Path) -> None:
        self._archives_root = Path(archives_root).expanduser().resolve()

    @property
    def archives_root(self) -> Path:
        return self._archives_root

    def prepare(
        self,
        *,
        channel: Channel,
        source_path: str | Path,
        expected_sha256: str,
    ) -> PreparedArchive:
        source = Path(source_path).expanduser().resolve()
        pending_root = self._archives_root / ".pending"
        pending_directory = pending_root / uuid.uuid4().hex
        pending_directory.mkdir(parents=True, exist_ok=False)
        prepared: PreparedArchive | None = None
        try:
            if channel is Channel.IOS:
                archived_name = "source.zip"
                archive_file = pending_directory / archived_name
                self._archive_ios(source, archive_file, expected_sha256)
            else:
                archived_name = source.name
                archive_file = pending_directory / archived_name
                self._archive_file(source, archive_file, expected_sha256)
            prepared = PreparedArchive(pending_directory, archived_name)
            return prepared
        except ConfirmationError:
            raise
        except Exception as error:
            raise ConfirmationError("C007", f"cannot prepare source archive: {error}") from error
        finally:
            if prepared is None and pending_directory.exists():
                shutil.rmtree(pending_directory)

    def finalize(
        self,
        prepared: PreparedArchive,
        *,
        channel: Channel,
        version_id: str,
    ) -> str:
        final_directory = self._archives_root / channel.value.lower() / version_id
        if final_directory.exists():
            raise ConfirmationError(
                "C007", f"archive directory already exists for version {version_id}"
            )
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            prepared.pending_directory.replace(final_directory)
        except OSError as error:
            raise ConfirmationError("C007", f"cannot finalize source archive: {error}") from error
        prepared.final_directory = final_directory
        relative = final_directory.relative_to(self._archives_root) / prepared.archived_name
        return relative.as_posix()

    def _archive_file(self, source: Path, target: Path, expected_sha256: str) -> None:
        if not source.is_file():
            raise ConfirmationError("C006", "source file no longer exists")
        if file_sha256(source) != expected_sha256:
            raise ConfirmationError("C006", "source file changed after preview")
        shutil.copy2(source, target)
        if file_sha256(target) != expected_sha256:
            raise ConfirmationError("C007", "archived file digest does not match preview")

    def _archive_ios(self, source: Path, target: Path, expected_sha256: str) -> None:
        if not source.is_dir():
            raise ConfirmationError("C006", "iOS source directory no longer exists")
        if ios_bundle_sha256(source) != expected_sha256:
            raise ConfirmationError("C006", "iOS source directory changed after preview")
        files = tuple(
            path for path in source.rglob("*") if path.is_file() and not path.is_symlink()
        )
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(files, key=lambda item: item.relative_to(source).as_posix()):
                archive.write(path, path.relative_to(source).as_posix())
        if ios_bundle_sha256(source) != expected_sha256:
            raise ConfirmationError("C006", "iOS source directory changed during archive")
        with zipfile.ZipFile(target) as archive:
            bad_file = archive.testzip()
            if bad_file is not None:
                raise ConfirmationError("C007", f"archived ZIP is damaged at {bad_file}")
