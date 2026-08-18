import sqlite3
from pathlib import Path

from src.acceptance import (
    ACCEPTANCE_DATABASE_PATH,
    ACCEPTANCE_PATHS,
    ACCEPTANCE_TITLE_SUFFIX,
    initialize_acceptance_environment,
)
from src.config.settings import DATABASE_PATH, RUNTIME_PATHS, RuntimePaths


def test_default_acceptance_paths_are_isolated_from_formal_runtime() -> None:
    assert ACCEPTANCE_PATHS.root != RUNTIME_PATHS.root
    assert ACCEPTANCE_DATABASE_PATH != DATABASE_PATH
    assert ACCEPTANCE_PATHS.archives != RUNTIME_PATHS.archives
    assert ACCEPTANCE_TITLE_SUFFIX == "Phase1 独立验收环境"


def test_acceptance_environment_initializes_schema_and_seed_data(tmp_path: Path) -> None:
    paths = RuntimePaths.from_root(tmp_path / "acceptance_runtime")

    database_path = initialize_acceptance_environment(paths)
    second_database_path = initialize_acceptance_environment(paths)

    assert database_path == second_database_path
    assert database_path.is_file()
    assert paths.archives.is_dir()
    assert paths.exports.is_dir()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 191
        assert connection.execute("SELECT COUNT(*) FROM price_tiers").fetchone()[0] == 14
