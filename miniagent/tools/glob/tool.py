from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .._filesystem_search import compile_pattern, walk
from ..models import ExecutionContext, ExecutionTraits, ResultPolicy, ToolOutput, ToolSpec, ToolTarget
from ..policy import resolve_workspace_target

class GlobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pattern: str
    path: str = "."
    kind: Literal["any", "file", "directory"] = "any"
    include_ignored: bool = False
    max_results: int = Field(default=100, ge=1, le=1000)

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        if len(value) > 512: raise ValueError("pattern is too long")
        compile_pattern(value)
        return value

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        if not value or Path(value).is_absolute(): raise ValueError("path must be a relative directory")
        return value

class GlobMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    kind: Literal["file", "directory"]
class GlobMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    search_root: str; match_count: int; truncated: bool
class GlobData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    matches: list[GlobMatch]
class GlobOutput(ToolOutput):
    metadata: GlobMetadata
    data: GlobData

def resolve_targets(args: BaseModel, workspace_root: Path) -> tuple[ToolTarget, ...]:
    assert isinstance(args, GlobInput)
    resolved, target = resolve_workspace_target(workspace_root, args.path)
    if not resolved.is_dir(): raise ValueError("path must name a directory")
    return (ToolTarget("directory", "read", target.value, scope="subtree"),)

def classify(args, targets): return ExecutionTraits(concurrency_safe=True)

def _scan(args: GlobInput, context: ExecutionContext) -> GlobOutput:
    root = (context.workspace_root / context.targets[0].value).resolve()
    matcher = compile_pattern(args.pattern)
    matches: list[GlobMatch] = []
    truncated = False
    for workspace_rel, path, is_dir in walk(root, context.workspace_root, include_ignored=args.include_ignored, explicit_mini=root.name == ".mini", cancellation=context.cancellation):
        local = path.relative_to(root).as_posix() + ("/" if is_dir else "")
        match_name = local[:-1] if is_dir else local
        if not matcher.fullmatch(match_name): continue
        kind = "directory" if is_dir else "file"
        if args.kind != "any" and args.kind != kind: continue
        if len(matches) >= args.max_results: truncated = True; break
        matches.append(GlobMatch(path=workspace_rel, kind=kind))
    paths = [item.path for item in matches]
    content = "\n".join(paths) if paths else "No paths matched."
    if truncated: content += "\n[Results truncated; narrow the search.]"
    return GlobOutput(content=content, metadata=GlobMetadata(search_root=context.targets[0].value, match_count=len(matches), truncated=truncated), data=GlobData(matches=matches))

async def handler(args: BaseModel, context: ExecutionContext) -> GlobOutput:
    assert isinstance(args, GlobInput)
    task = asyncio.create_task(asyncio.to_thread(_scan, args, context))
    try: return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise

glob_spec = SPEC = ToolSpec(name="glob", description="Discover files and directories by glob pattern within a workspace directory.", input_model=GlobInput, output_model=GlobOutput, handler=handler, prompt_ref="miniagent.tools.glob.prompt:PROMPT", resolve_targets=resolve_targets, classify=classify, timeout_seconds=20.0, result_policy=ResultPolicy(threshold_bytes=20*1024, hard_limit_bytes=50*1024))
