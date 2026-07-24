from __future__ import annotations

from pathlib import Path

from .models import ToolExecutionError, ToolTarget


class TargetPolicyError(ToolExecutionError):
    pass


def resolve_workspace_target(workspace_root: Path, raw_path: str) -> tuple[Path, ToolTarget]:
    root = workspace_root.resolve(strict=True)
    candidate = Path(raw_path.strip())
    if not raw_path.strip():
        raise TargetPolicyError("path must not be empty")
    try:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise TargetPolicyError("target is unavailable") from exc
    if not resolved.is_file() and not resolved.is_dir():
        raise TargetPolicyError("目标必须是普通文件或目录")
    kind = "file" if resolved.is_file() else "directory"
    return resolved, ToolTarget(kind=kind, capability="read", value=_target_value(root, resolved))


def resolve_file_target(workspace_root: Path, raw_path: str, *, operation: str) -> tuple[Path, ToolTarget]:
    """解析文件目标；写入允许目标尚不存在，但父目录必须存在。"""
    root = workspace_root.resolve(strict=True)
    if not raw_path.strip():
        raise TargetPolicyError("path must not be empty")
    candidate = Path(raw_path.strip())
    destination = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = destination.resolve(strict=False)
    except OSError as exc:
        raise TargetPolicyError("target is unavailable") from exc
    if operation == "read":
        if not resolved.exists():
            raise TargetPolicyError("target is unavailable")
        if not resolved.is_file():
            raise TargetPolicyError("target must be a regular file")
    else:
        parent = resolved.parent
        if not parent.exists() or not parent.is_dir():
            raise TargetPolicyError("parent directory is unavailable")
    return resolved, ToolTarget(kind="file", capability=operation, value=_target_value(root, resolved))


def _target_value(root: Path, resolved: Path) -> str:
    try:
        return resolved.relative_to(root).as_posix() or "."
    except ValueError:
        return resolved.as_posix()
