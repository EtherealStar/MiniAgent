from __future__ import annotations

import asyncio
import hashlib
import time
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from miniagent.documents import DocumentCache, DocumentRef

from ..models import (
    ExecutionContext, ExecutionErrorCode, ExecutionTraits, RetryPolicy, ToolExecutionError,
    ToolOutput, ToolSpec, ToolTarget,
)
from ..policy import resolve_file_target
from .client import MinerUClient

MAX_SOURCE_BYTES = 200 * 1024 * 1024
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


class ReadDocsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    path: str

    @field_validator("path")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


class ReadDocsMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_type: Literal["pdf", "doc", "docx"]
    cache_hit: bool
    model_version: Literal["vlm"]
    markdown_byte_count: int
    markdown_sha256: str


class ReadDocsData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document: DocumentRef


class ReadDocsOutput(ToolOutput):
    metadata: ReadDocsMetadata
    data: ReadDocsData


def resolve_targets(args: ReadDocsInput, workspace_root: Path) -> tuple[ToolTarget, ...]:
    resolved, source = resolve_file_target(workspace_root, args.path, operation="read")
    suffix = resolved.suffix.lower()
    if suffix not in {".pdf", ".doc", ".docx"}:
        raise ToolExecutionError("Only PDF, DOC, and DOCX documents are supported.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION)
    size = resolved.stat().st_size
    if size == 0:
        raise ToolExecutionError("The document is empty.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION)
    if size > MAX_SOURCE_BYTES:
        raise ToolExecutionError("The document exceeds MinerU limits.", code=ExecutionErrorCode.RESOURCE_EXHAUSTED)
    return (source, ToolTarget("external_service", "write", "mineru.net"))


def classify(args: ReadDocsInput, targets: tuple[ToolTarget, ...]) -> ExecutionTraits:
    return ExecutionTraits(concurrency_safe=False)


def _source_path(context: ExecutionContext) -> Path:
    value = Path(context.targets[0].value)
    return value if value.is_absolute() else context.workspace_root / value


def _validate_and_hash(path: Path, source_type: str) -> str:
    with path.open("rb") as source:
        header = source.read(8)
    if source_type == "pdf":
        if header[:5] != b"%PDF-":
            raise ToolExecutionError("The document content does not match its PDF suffix.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION)
    elif source_type == "doc":
        if header != OLE_SIGNATURE:
            raise ToolExecutionError("The document content does not match its DOC suffix.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION)
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
            if "[Content_Types].xml" not in names or not any(name.startswith("word/") for name in names):
                raise ToolExecutionError("The document content does not match its DOCX suffix.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION)
        except zipfile.BadZipFile as exc:
            raise ToolExecutionError("The document content does not match its DOCX suffix.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION) from exc
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _output(ref: DocumentRef, source_type: str, cache_hit: bool) -> ReadDocsOutput:
    return ReadDocsOutput(
        content=f"Document converted to Markdown. Use read_file with path {ref.path}, offset, and limit to read it.",
        metadata=ReadDocsMetadata(
            source_type=source_type, cache_hit=cache_hit, model_version="vlm",
            markdown_byte_count=ref.byte_count, markdown_sha256=ref.sha256,
        ),
        data=ReadDocsData(document=ref),
    )


async def handler(args: ReadDocsInput, context: ExecutionContext) -> ReadDocsOutput:
    path = _source_path(context)
    source_type = path.suffix.lower().removeprefix(".")
    source_sha256 = await asyncio.to_thread(_validate_and_hash, path, source_type)
    cache: DocumentCache = context.capability("document_cache")
    cached = await asyncio.to_thread(cache.lookup, context.session_id, source_sha256)
    if cached is not None:
        return _output(cached, source_type, True)

    client: MinerUClient = context.capability("mineru_client")
    markdown = await client.convert(
        path, model_version="vlm", cancellation=context.cancellation, deadline=time.monotonic() + 290,
    )
    try:
        ref = await asyncio.to_thread(
            cache.commit, context.session_id, source_sha256, source_type, "vlm", markdown
        )
    finally:
        markdown.unlink(missing_ok=True)
    return _output(ref, source_type, False)


SPEC = ToolSpec(
    name="read_docs",
    description="Convert a PDF or Word document to a session-scoped Markdown document with MinerU.",
    input_model=ReadDocsInput, output_model=ReadDocsOutput, handler=handler,
    prompt_ref="miniagent.tools.read_docs.prompt:PROMPT", resolve_targets=resolve_targets,
    classify=classify, retry_policy=RetryPolicy(max_attempts=1), timeout_seconds=300.0,
)
