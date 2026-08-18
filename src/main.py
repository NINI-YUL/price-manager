"""Application entry point for the price manager desktop tool."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from src.config.settings import (
    DATABASE_PATH,
    PROJECT_NAME,
    RUNTIME_PATHS,
    VERSION,
    ensure_runtime_directories,
)
from src.database.seed import seed_database


def window_title() -> str:
    """Return the stable title shown by the initial application window."""

    return f"{PROJECT_NAME} v{VERSION}"


def create_application(argv: Sequence[str] | None = None):
    """Create or reuse the Qt application without importing Qt at module import time."""

    from PySide6.QtWidgets import QApplication

    existing = QApplication.instance()
    if existing is not None:
        return existing
    return QApplication(list(argv) if argv is not None else sys.argv)


def create_main_window(
    database_path=DATABASE_PATH,
    *,
    archives_path=RUNTIME_PATHS.archives,
):
    """Build the Phase1 main window with import, price-library, and version navigation."""

    from PySide6.QtWidgets import QMainWindow

    from src.ui import ApplicationShell

    window = QMainWindow()
    window.setWindowTitle(window_title())
    window.setMinimumSize(1180, 720)
    window.setCentralWidget(
        ApplicationShell(database_path, archives_root=archives_path, parent=window)
    )
    return window


def main(argv: Sequence[str] | None = None) -> int:
    """Start the desktop application, or perform a non-blocking UI smoke test."""

    args = list(argv) if argv is not None else list(sys.argv)
    smoke_test = "--smoke-test" in args
    qt_args = [arg for arg in args if arg != "--smoke-test"]

    ensure_runtime_directories()
    seed_database()
    application = create_application(qt_args)
    window = create_main_window()
    window.show()

    if smoke_test:
        application.processEvents()
        window.close()
        return 0
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
