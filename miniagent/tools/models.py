from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from pydantic import BaseModel


SYSTEM_RESULT_HARD_LIMIT_BYTES = 50 * 1024


@dataclass(frozen=True, slots=True)
class ToolTarget:
    kind: str
    operation: str
    value: str


@dataclass(frozen=True, slots=True)
class ExecutionTraits:
    concurrency_safe: bool = False


@dataclass(frozen=True, slots=True)
class FieldError:
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ToolFailure:
    code: str
    stage: str
    message: str
    field_errors: tuple[FieldError, ...] = ()
    correctable: bool = False
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    path: str
    byte_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    retry_delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts 必须在 1 到 3 之间")
        if self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能为负数")


@dataclass(frozen=True, slots=True)
class ResultPolicy:
    threshold_bytes: int = 50 * 1024
    hard_limit_bytes: int = 50 * 1024
    preview_bytes: int = 2048

    def __post_init__(self) -> None:
        if self.threshold_bytes <= 0 or self.hard_limit_bytes <= 0:
            raise ValueError("结果阈值必须为正数")
        if self.hard_limit_bytes > SYSTEM_RESULT_HARD_LIMIT_BYTES:
            raise ValueError("工具不能提高系统结果硬上限")
        if self.threshold_bytes > self.hard_limit_bytes:
            raise ValueError("工具结果阈值不能超过系统硬上限")


class TraceSink(Protocol):
    async def emit(self, event: Mapping[str, object]) -> None: ...


class ArtifactStore(Protocol):
    def persist(self, session_id: str, tool_use_id: str, content: str) -> ArtifactRef: ...


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    session_id: str
    run_id: str
    tool_use_id: str
    workspace_root: Path
    cancellation: Any
    trace_sink: TraceSink
    artifact_store: ArtifactStore
    targets: tuple[ToolTarget, ...] = ()


class ToolExecutionError(Exception):
    def __init__(self, message: str, *, transient: bool = False, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        self.outcome_unknown = outcome_unknown


ToolHandler = Callable[[BaseModel, ExecutionContext], Awaitable[str]]
TargetResolver = Callable[[BaseModel, Path], tuple[ToolTarget, ...]]
ExecutionClassifier = Callable[[BaseModel, tuple[ToolTarget, ...]], ExecutionTraits]


def no_targets(args: BaseModel, workspace_root: Path) -> tuple[ToolTarget, ...]:
    return ()


def serial_execution(args: BaseModel, targets: tuple[ToolTarget, ...]) -> ExecutionTraits:
    return ExecutionTraits()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    input_model: type[BaseModel]
    handler: ToolHandler
    description: str = ""
    prompt_ref: str | None = None
    resolve_targets: TargetResolver = no_targets
    classify: ExecutionClassifier = serial_execution
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_seconds: float | None = None
    result_policy: ResultPolicy = field(default_factory=ResultPolicy)
    function_schema: Mapping[str, object] | None = None

    def with_schema(self, schema: Mapping[str, object]) -> ToolSpec:
        return replace(self, function_schema=schema)


class ToolRegistryError(ValueError):
    pass


class ToolProtocolError(RuntimeError):
    pass
