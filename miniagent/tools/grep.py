from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import ExecutionContext, ExecutionTraits, ResultPolicy, ToolSpec
from .policy import resolve_workspace_target


MAX_LINE_CHARS = 500


class GrepInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str
    path: str = "."
    include: str | None = None
    case_sensitive: bool = True
    max_matches: int = Field(default=100, ge=1, le=1000)

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        if not value:
            raise ValueError("pattern 不能为空")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern 不是合法正则表达式: {exc}") from exc
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value or Path(value).is_absolute():
            raise ValueError("path 必须是非空相对路径")
        return value

    @field_validator("include")
    @classmethod
    def validate_include(cls, value: str | None) -> str | None:
        if value is not None and (not value or Path(value).is_absolute() or ".." in Path(value).parts):
            raise ValueError("include 必须是搜索根目录下的单个相对 glob")
        return value


def resolve_grep_targets(args: BaseModel, workspace_root: Path):
    assert isinstance(args, GrepInput)
    _, target = resolve_workspace_target(workspace_root, args.path)
    return (target,)


def classify_grep(args: BaseModel, targets) -> ExecutionTraits:
    return ExecutionTraits(concurrency_safe=True)


@dataclass(slots=True)
class _Summary:
    scanned: int = 0
    skipped_binary: int = 0
    skipped_non_utf8: int = 0
    skipped_unreadable: int = 0
    truncated: bool = False


def _candidate_files(root: Path, workspace: Path, include: str | None) -> list[tuple[str, Path]]:
    if root.is_file():
        if include is not None and not fnmatch.fnmatchcase(root.name, include):
            return []
        return [(root.relative_to(workspace).as_posix(), root)]
    candidates: list[tuple[str, Path]] = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in {".git", ".mini"})
        for filename in sorted(files):
            path = Path(directory) / filename
            relative_search = path.relative_to(root)
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(workspace)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            relative_text = relative_search.as_posix()
            if include is not None and not fnmatch.fnmatchcase(relative_text, include):
                continue
            candidates.append((resolved.relative_to(workspace).as_posix(), resolved))
    return sorted(set(candidates), key=lambda item: item[0])


def _scan(args: GrepInput, context: ExecutionContext) -> str:
    root = context.workspace_root / context.targets[0].value
    flags = 0 if args.case_sensitive else re.IGNORECASE
    pattern = re.compile(args.pattern, flags)
    summary = _Summary()
    matches: list[str] = []
    for relative, path in _candidate_files(root, context.workspace_root, args.include):
        context.cancellation.raise_if_cancelled()
        try:
            data = path.read_bytes()
        except OSError:
            summary.skipped_unreadable += 1
            continue
        if b"\x00" in data:
            summary.skipped_binary += 1
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            summary.skipped_non_utf8 += 1
            continue
        summary.scanned += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) is None:
                continue
            display = line
            if len(display) > MAX_LINE_CHARS:
                display = f"{display[:MAX_LINE_CHARS]}... [line truncated]"
            matches.append(f"{relative}:{line_number}:{display}")
            if len(matches) >= args.max_matches:
                summary.truncated = True
                break
        if summary.truncated:
            break
    header = matches if matches else ["No matches"]
    header.append(
        "Summary: "
        f"matches={len(matches)}, scanned={summary.scanned}, skipped_binary={summary.skipped_binary}, "
        f"skipped_non_utf8={summary.skipped_non_utf8}, skipped_unreadable={summary.skipped_unreadable}, "
        f"truncated={str(summary.truncated).lower()}"
    )
    return "\n".join(header)


async def grep_handler(args: BaseModel, context: ExecutionContext) -> str:
    assert isinstance(args, GrepInput)
    scan = asyncio.create_task(asyncio.to_thread(_scan, args, context))
    try:
        return await asyncio.shield(scan)
    except asyncio.CancelledError:
        # to_thread 不能强制终止；等待已收到取消信号的扫描线程退出，避免脱离 AgentRun。
        await scan
        raise


grep_spec = ToolSpec(
    name="grep",
    description="在 workspace 内递归搜索 UTF-8 文本文件。",
    input_model=GrepInput,
    handler=grep_handler,
    resolve_targets=resolve_grep_targets,
    classify=classify_grep,
    timeout_seconds=30.0,
    result_policy=ResultPolicy(threshold_bytes=20 * 1024, hard_limit_bytes=50 * 1024),
)
