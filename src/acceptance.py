"""Independent Phase1 GUI acceptance environment."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from src.config.settings import (
    DATABASE_FILENAME,
    PROJECT_ROOT,
    RuntimePaths,
    ensure_runtime_directories,
)
from src.database.seed import seed_database
from src.main import create_application, create_main_window, window_title

ACCEPTANCE_ROOT = PROJECT_ROOT / "acceptance_runtime"
ACCEPTANCE_PATHS = RuntimePaths.from_root(ACCEPTANCE_ROOT)
ACCEPTANCE_DATABASE_PATH = ACCEPTANCE_PATHS.data / DATABASE_FILENAME
ACCEPTANCE_TITLE_SUFFIX = "Phase1 独立验收环境"


def initialize_acceptance_environment(
    paths: RuntimePaths = ACCEPTANCE_PATHS,
) -> Path:
    """Create and seed runtime storage that is isolated from the formal environment."""

    ensure_runtime_directories(paths)
    database_path = paths.data / DATABASE_FILENAME
    seed_database(database_path)
    return database_path


def create_acceptance_window(paths: RuntimePaths = ACCEPTANCE_PATHS):
    """Build a clearly labelled main window backed by acceptance-only storage."""

    database_path = initialize_acceptance_environment(paths)
    window = create_main_window(database_path, archives_path=paths.archives)
    window.setWindowTitle(f"{window_title()} - {ACCEPTANCE_TITLE_SUFFIX}")
    return window


def main(argv: Sequence[str] | None = None) -> int:
    """Start the isolated GUI, or run a non-blocking acceptance smoke test."""

    args = list(argv) if argv is not None else list(sys.argv)
    smoke_test = "--smoke-test" in args
    qt_args = [arg for arg in args if arg != "--smoke-test"]

    application = create_application(qt_args)
    window = create_acceptance_window()
    window.show()

    if smoke_test:
        application.processEvents()
        window.close()
        return 0
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
