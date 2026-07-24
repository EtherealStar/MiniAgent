from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ..models import ExecutionContext, ExecutionTraits, ToolExecutionError, ToolOutput, ToolSpec
from ..policy import resolve_file_target


class WriteFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    path: str
    content: str
    expected_sha256: str | None = None

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > 256 * 1024:
            raise ValueError("content must be UTF-8 text no larger than 256 KiB")
        return value

    @field_validator("expected_sha256")
    @classmethod
    def valid_hash(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
            raise ValueError("expected_sha256 must be lowercase hexadecimal")
        return value


class WriteFileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    operation: Literal["created", "replaced"]
    byte_count: int
    sha256: str


class WriteFileData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WriteFileOutput(ToolOutput):
    metadata: WriteFileMetadata
    data: WriteFileData


def resolve_targets(args: WriteFileInput, workspace_root: Path):
    _, target = resolve_file_target(workspace_root, args.path, operation="write")
    return (target,)


def classify(args, targets):
    return ExecutionTraits(concurrency_safe=False)


def _write(destination: Path, content: str, expected: str | None):
    payload = content.encode("utf-8")
    existed = destination.exists()
    if expected is None and existed:
        raise ToolExecutionError("The destination already exists.", code="conflict")
    if expected is not None:
        if not existed or not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise ToolExecutionError("The destination changed since it was read.", code="conflict")
    operation = "replaced" if existed else "created"
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".miniagent-", dir=str(destination.parent))
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is None:
            try:
                os.link(temp_name, destination)
                os.unlink(temp_name)
            except FileExistsError as exc:
                raise ToolExecutionError("The destination already exists.", code="conflict") from exc
        else:
            os.replace(temp_name, destination)
            temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return operation, len(payload), hashlib.sha256(payload).hexdigest()


async def handler(args: WriteFileInput, context: ExecutionContext) -> WriteFileOutput:
    destination = Path(context.workspace_root) / context.targets[0].value
    try:
        operation, count, digest = await asyncio.to_thread(_write, destination, args.content, args.expected_sha256)
    except ToolExecutionError:
        raise
    except OSError as exc:
        raise ToolExecutionError("The destination could not be written.", code="operation_failed") from exc
    label = "Created" if operation == "created" else "Updated"
    return WriteFileOutput(
        content=f"{label} {context.targets[0].value} ({count} bytes, sha256: {digest}).",
        metadata=WriteFileMetadata(path=context.targets[0].value, operation=operation, byte_count=count, sha256=digest),
        data=WriteFileData(),
    )


SPEC = ToolSpec(
    name="write_file", description="Create or atomically replace one UTF-8 text file with conflict protection.",
    input_model=WriteFileInput, output_model=WriteFileOutput, handler=handler,
    prompt_ref="miniagent.tools.write_file.prompt:PROMPT", resolve_targets=resolve_targets,
    classify=classify, timeout_seconds=15.0,
)
write_file_spec = SPEC
