from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import ExecutionContext, ExecutionTraits, ResultPolicy, ToolExecutionError, ToolOutput, ToolSpec
from ..policy import resolve_file_target


class ReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=2000)

    @field_validator("path")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


class ReadFileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    sha256: str
    source_byte_count: int
    returned_byte_count: int
    returned_token_count: int
    newline: Literal["lf", "crlf", "cr", "mixed", "none"]
    offset: int
    limit: int
    start_line: int | None
    end_line: int | None
    returned_line_count: int
    next_offset: int
    has_more: bool


class ReadFileData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadFileOutput(ToolOutput):
    metadata: ReadFileMetadata
    data: ReadFileData


def resolve_targets(args: ReadFileInput, workspace_root: Path):
    _, target = resolve_file_target(workspace_root, args.path, operation="read")
    return (target,)


def classify(args, targets):
    return ExecutionTraits(concurrency_safe=True)


def _read(path: Path, offset: int, limit: int):
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ToolExecutionError("The file is not a supported UTF-8 text file.", code="unsupported_operation")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("The file is not a supported UTF-8 text file.", code="unsupported_operation") from exc
    has_crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if has_crlf and ("\n" in text.replace("\r\n", "" ) or "\r" in text.replace("\r\n", "")):
        newline = "mixed"
    elif has_crlf:
        newline = "crlf"
    elif "\r" in text:
        newline = "cr"
    elif "\n" in text:
        newline = "lf"
    else:
        newline = "none"
    lines = normalized.splitlines()
    page = lines[offset:offset + limit]
    content = "\n".join(f"{offset + index + 1} | {line}" for index, line in enumerate(page))
    if not page:
        content = "File is empty." if not lines else "No lines available at this offset."
    return raw, content, lines, newline


async def handler(args: ReadFileInput, context: ExecutionContext) -> ReadFileOutput:
    target = Path(context.workspace_root) / context.targets[0].value
    try:
        raw, content, lines, newline = await asyncio.to_thread(_read, target, args.offset, args.limit)
    except FileNotFoundError as exc:
        raise ToolExecutionError("The file is unavailable.", code="resource_unavailable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    selected = lines[args.offset:args.offset + args.limit]
    encoded = content.encode("utf-8")
    metadata = ReadFileMetadata(
        path=context.targets[0].value, sha256=digest, source_byte_count=len(raw),
        returned_byte_count=len(encoded), returned_token_count=len(content.split()), newline=newline,
        offset=args.offset, limit=args.limit, start_line=args.offset + 1 if selected else None,
        end_line=args.offset + len(selected) if selected else None, returned_line_count=len(selected),
        next_offset=args.offset + len(selected), has_more=args.offset + len(selected) < len(lines),
    )
    return ReadFileOutput(content=content, metadata=metadata, data=ReadFileData())


SPEC = ToolSpec(
    name="read_file", description="Read a UTF-8 text file by line range with stable line numbers.",
    input_model=ReadFileInput, output_model=ReadFileOutput, handler=handler,
    prompt_ref="miniagent.tools.read_file.prompt:PROMPT", resolve_targets=resolve_targets,
    classify=classify, timeout_seconds=15.0, result_policy=ResultPolicy(
        threshold_bytes=256 * 1024, hard_limit_bytes=256 * 1024,
        max_inline_bytes=256 * 1024, overflow_behavior="error", max_model_tokens=25_000,
    ),
)
read_file_spec = SPEC
