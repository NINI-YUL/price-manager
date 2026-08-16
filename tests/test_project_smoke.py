from pathlib import Path

from src.config.settings import PROJECT_NAME, VERSION, RuntimePaths, ensure_runtime_directories
from src.main import window_title


def test_project_metadata_and_window_title() -> None:
    assert PROJECT_NAME == "多渠道本地化价格管理工具"
    assert VERSION == "0.1.0"
    assert window_title() == "多渠道本地化价格管理工具 v0.1.0"


def test_runtime_directories_are_created_idempotently(tmp_path: Path) -> None:
    paths = RuntimePaths.from_root(tmp_path)

    ensure_runtime_directories(paths)
    ensure_runtime_directories(paths)

    assert paths.data.is_dir()
    assert paths.archives.is_dir()
    assert paths.exports.is_dir()
