import tomllib
from pathlib import Path

from src import __version__
from src.config.settings import PROJECT_NAME, VERSION, RuntimePaths, ensure_runtime_directories
from src.main import window_title


def test_project_metadata_and_window_title() -> None:
    project_metadata = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert PROJECT_NAME == "多渠道本地化价格管理工具"
    assert VERSION == "0.1.1"
    assert __version__ == VERSION
    assert project_metadata["project"]["version"] == VERSION
    assert window_title() == "多渠道本地化价格管理工具 v0.1.1"


def test_runtime_directories_are_created_idempotently(tmp_path: Path) -> None:
    paths = RuntimePaths.from_root(tmp_path)

    ensure_runtime_directories(paths)
    ensure_runtime_directories(paths)

    assert paths.data.is_dir()
    assert paths.archives.is_dir()
    assert paths.exports.is_dir()
