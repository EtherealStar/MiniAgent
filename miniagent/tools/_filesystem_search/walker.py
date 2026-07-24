from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

HARD_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

def walk(root: Path, workspace: Path, *, include_ignored: bool = False, explicit_mini: bool = False, cancellation=None) -> Iterator[tuple[str, Path, bool]]:
    """稳定、不可跟随链接的 walker；在每个边界检查取消，避免线程脱离运行。"""
    root = root.resolve(strict=True)
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        if cancellation is not None: cancellation.raise_if_cancelled()
        current = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in HARD_DIRS and (explicit_mini or d != ".mini"))
        for name in sorted(files):
            if cancellation is not None: cancellation.raise_if_cancelled()
            if name.endswith(".pyc"): continue
            path = current / name
            if path.is_symlink(): continue
            try: rel = path.relative_to(workspace).as_posix()
            except ValueError: continue
            yield rel, path, False
        for name in sorted(dirs):
            path = current / name
            if path.is_symlink(): continue
            try: rel = path.relative_to(workspace).as_posix()
            except ValueError: continue
            yield rel + "/", path, True
