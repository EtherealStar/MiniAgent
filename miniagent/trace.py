from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4


class TraceStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


class TraceEventType(StrEnum):
    SPAN_STARTED = "span_started"
    SPAN_FINISHED = "span_finished"
    STREAM_SUMMARY = "stream_summary"
    ATTEMPT_STARTED = "attempt_started"
    RETRY_SCHEDULED = "retry_scheduled"
    TOOL_FINISHED = "tool_finished"


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None
    session_id: UUID
    run_id: UUID
    message_id: UUID | None = None

    def child(self, *, message_id: UUID | None = None) -> TraceContext:
        return TraceContext(
            self.trace_id,
            uuid4(),
            self.span_id,
            self.session_id,
            self.run_id,
            self.message_id if message_id is None else message_id,
        )


@dataclass(frozen=True, slots=True)
class TraceEvent:
    event_type: TraceEventType
    context: TraceContext
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_record_id: UUID
    trace_sequence: int
    occurred_at: datetime
    event: TraceEvent


class TraceSink(Protocol):
    async def emit(self, event: TraceEvent) -> None: ...

    async def close(self, drain_timeout: float = 1.0) -> None: ...


class NullTraceSink:
    async def emit(self, event: TraceEvent) -> None:
        return None

    async def close(self, drain_timeout: float = 1.0) -> None:
        return None


class MemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def emit(self, event: TraceEvent) -> None:
        self.events.append(event)

    async def close(self, drain_timeout: float = 1.0) -> None:
        return None


class BestEffortTraceSink:
    def __init__(self, delegate: TraceSink) -> None:
        self.delegate = delegate
        self.failed_count = 0

    async def emit(self, event: TraceEvent) -> None:
        try:
            await self.delegate.emit(event)
        except Exception:
            self.failed_count += 1

    async def close(self, drain_timeout: float = 1.0) -> None:
        try:
            await self.delegate.close(drain_timeout)
        except Exception:
            self.failed_count += 1


@dataclass(slots=True)
class TraceSpan:
    recorder: TraceRecorder
    name: str
    context: TraceContext
    started_ns: int
    _finished: bool = False

    async def finish(self, status: TraceStatus = TraceStatus.OK, **payload: object) -> None:
        if self._finished:
            return
        self._finished = True
        elapsed_ms = (time.monotonic_ns() - self.started_ns) / 1_000_000
        await self.recorder.emit(
            TraceEventType.SPAN_FINISHED,
            self.context,
            {"name": self.name, "status": status.value, "duration_ms": elapsed_ms, **payload},
        )


class TraceRecorder:
    def __init__(self, sink: TraceSink) -> None:
        self.sink = sink if isinstance(sink, BestEffortTraceSink) else BestEffortTraceSink(sink)

    async def start_span(
        self,
        name: str,
        context: TraceContext,
        **payload: object,
    ) -> TraceSpan:
        started_ns = time.monotonic_ns()
        await self.emit(TraceEventType.SPAN_STARTED, context, {"name": name, **payload})
        return TraceSpan(self, name, context, started_ns)

    async def emit(
        self,
        event_type: TraceEventType,
        context: TraceContext,
        payload: Mapping[str, object],
    ) -> None:
        await self.sink.emit(TraceEvent(event_type, context, payload))

class JsonlTraceSink:
    def __init__(
        self,
        trace_directory: Path,
        *,
        queue_capacity: int = 1024,
        max_file_bytes: int = 8 * 1024 * 1024,
        writer_gate: asyncio.Event | None = None,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("Trace queue_capacity 必须为正数")
        if max_file_bytes <= 0:
            raise ValueError("Trace max_file_bytes 必须为正数")
        self.trace_directory = Path(trace_directory)
        self.max_file_bytes = max_file_bytes
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue(queue_capacity)
        self._writer_gate = writer_gate
        self._writer_task: asyncio.Task[None] | None = None
        self._closed = False
        self._sequence = 0
        self._file_number: int | None = None
        self._stream = None
        self._stream_size = 0
        self.dropped_count = 0
        self.failed_count = 0

    async def emit(self, event: TraceEvent) -> None:
        if self._closed:
            self.dropped_count += 1
            return
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer(), name="miniagent-trace-writer")
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_count += 1

    async def _writer(self) -> None:
        if self._writer_gate is not None:
            await self._writer_gate.wait()
        try:
            while True:
                event = await self._queue.get()
                try:
                    self._sequence += 1
                    record = TraceRecord(uuid4(), self._sequence, datetime.now(timezone.utc), event)
                    encoded = self._encode(record)
                    await asyncio.to_thread(self._write_line, encoded)
                except Exception:
                    self.failed_count += 1
                finally:
                    self._queue.task_done()
        finally:
            await asyncio.to_thread(self._close_stream)

    @staticmethod
    def _encode(record: TraceRecord) -> bytes:
        context = record.event.context
        value = {
            "trace_schema_version": 1,
            "trace_record_id": str(record.trace_record_id),
            "trace_sequence": record.trace_sequence,
            "occurred_at": record.occurred_at.isoformat().replace("+00:00", "Z"),
            "event_type": record.event.event_type.value,
            "trace_id": str(context.trace_id),
            "span_id": str(context.span_id),
            "parent_span_id": str(context.parent_span_id) if context.parent_span_id else None,
            "session_id": str(context.session_id),
            "run_id": str(context.run_id),
            "message_id": str(context.message_id) if context.message_id else None,
            "payload": dict(record.event.payload),
        }
        return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def _write_line(self, encoded: bytes) -> None:
        if self._stream is None:
            self._open_next_file()
        if self._stream_size and self._stream_size + len(encoded) > self.max_file_bytes:
            self._close_stream()
            self._open_next_file()
        written = self._stream.write(encoded)
        if written != len(encoded):
            raise OSError("Trace 未完整写入")
        self._stream.flush()
        self._stream_size += len(encoded)

    def _open_next_file(self) -> None:
        self.trace_directory.mkdir(parents=True, exist_ok=True)
        if self._file_number is None:
            numbers = [
                int(path.stem)
                for path in self.trace_directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9].jsonl")
                if path.stem.isdigit()
            ]
            self._file_number = max(numbers, default=0) + 1
        else:
            self._file_number += 1
        path = self.trace_directory / f"{self._file_number:06d}.jsonl"
        self._stream = path.open("xb")
        self._stream_size = 0

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        try:
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            stream.close()

    async def close(self, drain_timeout: float = 1.0) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._writer_task
        if task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            self.dropped_count += self._queue.qsize()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Trace 的最终 flush/fsync 失败也不能越过 best-effort 边界。
            self.failed_count += 1


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
)


def sanitize_error(error: BaseException | Mapping[str, object]) -> dict[str, object]:
    if isinstance(error, BaseException):
        message = str(error)
        error_type = type(error).__name__
        category = "internal"
        source: Mapping[str, object] = {}
    else:
        source = error
        message = str(source.get("message", ""))
        error_type = str(source.get("type", "Error"))
        category = str(source.get("category", "unknown"))
    message = _CONTROL_CHARACTERS.sub(" ", message)
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub(r"\1[REDACTED]", message)
    return {
        "category": category,
        "type": error_type,
        "retryable": bool(source.get("retryable", False)),
        "provider_code": source.get("provider_code"),
        "status_code": source.get("status_code"),
        "request_id": source.get("request_id"),
        "cancelled": bool(source.get("cancelled", False)),
        "message": message[:512],
    }
