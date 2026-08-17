"""Stable source digests shared by confirmation and archive verification."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ios_bundle_sha256(root: Path) -> str:
    paths = tuple(path for path in root.rglob("*.csv") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        digest.update(f"{relative_path}|{file_sha256(path)}\n".encode())
    return digest.hexdigest()
