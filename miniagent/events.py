from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TypeAlias
from uuid import UUID, uuid4

from .domain import Message


@dataclass(frozen=True, slots=True, kw_only=True)
class EventPayloadBase:
    event_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class UserMessageRecorded(EventPayloadBase):
    message: Message


@dataclass(frozen=True, slots=True)
class AssistantMessageStarted(EventPayloadBase):
    message_id: UUID
    continuation_of_message_id: UUID | None = None
    retry_of_message_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AssistantPartDelta(EventPayloadBase):
    message_id: UUID
    kind: str
    content: str


@dataclass(frozen=True, slots=True)
class ToolUseDetected(EventPayloadBase):
    message_id: UUID
    tool_use_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class AssistantMessageCompleted(EventPayloadBase):
    message: Message
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class AssistantMessageDiscarded(EventPayloadBase):
    message_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class ToolResultRecorded(EventPayloadBase):
    message: Message


@dataclass(frozen=True, slots=True)
class RunTerminated(EventPayloadBase):
    reason: str
    turn_count: int


EventPayload: TypeAlias = (
    UserMessageRecorded
    | AssistantMessageStarted
    | AssistantPartDelta
    | ToolUseDetected
    | AssistantMessageCompleted
    | AssistantMessageDiscarded
    | ToolResultRecorded
    | RunTerminated
)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    event_id: UUID
    session_id: UUID
    run_id: UUID
    sequence: int
    occurred_at: datetime
    payload: EventPayload

    @classmethod
    def create(cls, session_id: UUID, run_id: UUID, sequence: int, payload: EventPayload) -> SessionEvent:
        return cls(
            event_id=payload.event_id,
            session_id=session_id,
            run_id=run_id,
            sequence=sequence,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
        )
