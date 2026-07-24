from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from miniagent.trace import TraceSink


SYSTEM_RESULT_HARD_LIMIT_BYTES = 50 * 1024


@dataclass(frozen=True, slots=True)
class ToolTarget:
    kind: str
    capability: str
    value: str
    scope: str = "exact"

    def __post_init__(self) -> None:
        if self.kind not in {"file", "directory", "external_service", "session_state", "artifact", "document"}:
            raise ValueError("unsupported target kind")
        if self.capability not in {"read", "write", "delete"}:
            raise ValueError("unsupported target capability")
        if self.scope not in {"exact", "subtree"}:
            raise ValueError("unsupported target scope")
        if not self.value:
            raise ValueError("target value must not be empty")


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


class ExecutionErrorCode(StrEnum):
    OPERATION_FAILED = "operation_failed"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    INVALID_RESPONSE = "invalid_response"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    DOMAIN_ERROR = "domain_error"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    CONFLICT = "conflict"


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str
    metadata: dict[str, object] = Field(default_factory=dict)
    data: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreToolUseOutcome:
    """AgentLoop 在启动批次前得到的单个 ToolUse 预检决定。"""

    tool_use_id: str
    rejection_code: str | None = None
    message: str = ""
    field_errors: tuple[FieldError, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.rejection_code is None


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
    # 读取类工具可以声明超限即失败，避免把截断结果误报为成功。
    max_inline_bytes: int | None = None
    overflow_behavior: str = "externalize"
    max_model_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.threshold_bytes <= 0 or self.hard_limit_bytes <= 0:
            raise ValueError("结果阈值必须为正数")
        if self.hard_limit_bytes > SYSTEM_RESULT_HARD_LIMIT_BYTES and self.overflow_behavior != "error":
            raise ValueError("工具不能提高系统结果硬上限")
        if self.threshold_bytes > self.hard_limit_bytes:
            raise ValueError("工具结果阈值不能超过系统硬上限")
        if self.max_inline_bytes is not None and self.max_inline_bytes <= 0:
            raise ValueError("max_inline_bytes 必须为正数")
        if self.overflow_behavior not in {"externalize", "error"}:
            raise ValueError("overflow_behavior 无效")
        if self.max_model_tokens is not None and self.max_model_tokens <= 0:
            raise ValueError("max_model_tokens 必须为正数")


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
    runtime_capabilities: Mapping[str, Any] = field(default_factory=dict)

    def capability(self, name: str) -> Any:
        try:
            return self.runtime_capabilities[name]
        except KeyError as exc:
            raise RuntimeError(f"runtime capability is unavailable: {name}") from exc


class ToolExecutionError(Exception):
    def __init__(
        self,
        safe_message: str,
        *,
        transient: bool = False,
        outcome_unknown: bool = False,
        code: ExecutionErrorCode | str = ExecutionErrorCode.OPERATION_FAILED,
    ) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.transient = transient
        self.outcome_unknown = outcome_unknown
        self.code = ExecutionErrorCode(code)


ToolHandler = Callable[[BaseModel, ExecutionContext], Awaitable[ToolOutput]]
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
    output_model: type[BaseModel] = ToolOutput
    output_schema: Mapping[str, object] | None = None

    def with_schema(self, schema: Mapping[str, object]) -> ToolSpec:
        return replace(self, function_schema=schema)


class ToolRegistryError(ValueError):
    pass


class ToolProtocolError(RuntimeError):
    pass
