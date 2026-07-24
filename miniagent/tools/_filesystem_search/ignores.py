from __future__ import annotations

from pathlib import Path
from pathspec import PathSpec
from pathspec.patterns import GitWildMatchPattern

def load_ignore(root: Path) -> PathSpec:
    lines = []
    file = root / ".gitignore"
    if file.is_file():
        lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
    return PathSpec.from_lines(GitWildMatchPattern, lines)
