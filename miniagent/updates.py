from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .domain import Message


@dataclass(frozen=True, slots=True)
class InputQueued:
    run_id: UUID
    message: Message


@dataclass(frozen=True, slots=True)
class InputWithdrawn:
    message_id: UUID


@dataclass(frozen=True, slots=True)
class UserMessageCommitted:
    message: Message


@dataclass(frozen=True, slots=True)
class AssistantMessageStarted:
    message_id: UUID
    continuation_of_message_id: UUID | None = None
    retry_of_message_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AssistantPartDelta:
    message_id: UUID
    kind: str
    content: str


@dataclass(frozen=True, slots=True)
class ToolUseDetected:
    message_id: UUID
    tool_use_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class AssistantMessageCompleted:
    message: Message
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class AssistantMessageDiscarded:
    message_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class ToolResultCompleted:
    message: Message


@dataclass(frozen=True, slots=True)
class RunTerminated:
    reason: str
    turn_count: int


@dataclass(frozen=True, slots=True)
class CompressionStarted:
    trigger_model_call_id: UUID | None
    source_boundary_message_id: UUID


@dataclass(frozen=True, slots=True)
class CompressionCompleted:
    summary_id: UUID
    covers_through_message_id: UUID
    resume_from_message_id: UUID | None
    source_token_count: int
    summary_token_count: int
    target_unreachable: bool = False


@dataclass(frozen=True, slots=True)
class CompressionFailed:
    reason: str
    measured_token_count: int
    protected_token_count: int
