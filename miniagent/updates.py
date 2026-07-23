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
