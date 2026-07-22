from __future__ import annotations

from pathlib import Path, PurePath

from .models import ToolExecutionError, ToolTarget


class TargetPolicyError(ToolExecutionError):
    pass


def resolve_workspace_target(workspace_root: Path, raw_path: str) -> tuple[Path, ToolTarget]:
    root = workspace_root.resolve(strict=True)
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise TargetPolicyError("path 必须是 workspace 内的相对路径")
    if ".." in PurePath(raw_path).parts:
        raise TargetPolicyError("path 不能包含父目录跳转 '..'")
    try:
        resolved = (root / candidate).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise TargetPolicyError(f"目标不存在或无法访问: {raw_path}") from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise TargetPolicyError("目标解析后位于 workspace 外部") from exc
    if not resolved.is_file() and not resolved.is_dir():
        raise TargetPolicyError("目标必须是普通文件或目录")
    kind = "file" if resolved.is_file() else "directory"
    value = relative.as_posix() or "."
    return resolved, ToolTarget(kind=kind, operation="read", value=value)
