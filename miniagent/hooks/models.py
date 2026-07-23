from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias
from uuid import UUID

from ..domain import Message, ToolResult, ToolSpec, ToolUsePart
from ..ports import ModelContext


@dataclass(frozen=True, slots=True)
class PreModelCallContext:
    run_id: UUID
    turn_number: int
    model_context: ModelContext
    tool_view_id: str = ""


@dataclass(frozen=True, slots=True)
class AssistantMessageCompletedContext:
    run_id: UUID
    message: Message
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class PreToolUseContext:
    run_id: UUID
    assistant_message_id: UUID
    tool_use: ToolUsePart
    tool_spec: ToolSpec | None


@dataclass(frozen=True, slots=True)
class PostToolUseContext:
    run_id: UUID
    assistant_message_id: UUID
    result: ToolResult
    tool_message: Message | None = None


@dataclass(frozen=True, slots=True)
class ContinueModelCall:
    pass


@dataclass(frozen=True, slots=True)
class RequestCompression:
    reason: str = "hook_requested"


@dataclass(frozen=True, slots=True)
class AbortRun:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ContinueToolUse:
    pass


@dataclass(frozen=True, slots=True)
class RejectToolUse:
    code: str
    message: str
    field_errors: tuple[tuple[str, str], ...] = ()


PreModelCallResult: TypeAlias = ContinueModelCall | RequestCompression | AbortRun
PreToolUseResult: TypeAlias = ContinueToolUse | RejectToolUse


class PreModelCallHook(Protocol):
    async def __call__(self, context: PreModelCallContext) -> PreModelCallResult: ...


class AssistantMessageCompletedHook(Protocol):
    async def __call__(self, context: AssistantMessageCompletedContext) -> None: ...


class PreToolUseHook(Protocol):
    async def __call__(self, context: PreToolUseContext) -> PreToolUseResult: ...


class PostToolUseHook(Protocol):
    async def __call__(self, context: PostToolUseContext) -> None: ...


class TraceSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...
