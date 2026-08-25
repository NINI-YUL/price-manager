"""Central, side-effect-free project settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_NAME = "多渠道本地化价格管理工具"
VERSION = "0.1.1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_FILENAME = "price_manager.db"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Filesystem locations used by the local desktop application."""

    root: Path
    data: Path
    archives: Path
    exports: Path

    @classmethod
    def from_root(cls, root: Path) -> RuntimePaths:
        resolved_root = root.resolve()
        return cls(
            root=resolved_root,
            data=resolved_root / "data",
            archives=resolved_root / "archives",
            exports=resolved_root / "exports",
        )


RUNTIME_PATHS = RuntimePaths.from_root(PROJECT_ROOT)
DATABASE_PATH = RUNTIME_PATHS.data / DATABASE_FILENAME


def ensure_runtime_directories(paths: RuntimePaths = RUNTIME_PATHS) -> None:
    """Create the three task-approved runtime directories when missing."""

    for directory in (paths.data, paths.archives, paths.exports):
        directory.mkdir(parents=True, exist_ok=True)
