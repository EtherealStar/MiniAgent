from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from ..domain import Message, ReasoningPart, Role, TextPart, ToolResultPart, ToolUsePart
from ..updates import (
    AssistantMessageCompleted,
    AssistantMessageDiscarded,
    AssistantMessageStarted,
    AssistantPartDelta,
    InputQueued,
    InputWithdrawn,
    ToolResultCompleted,
    UserMessageCommitted,
)


class MessageLifecycle(StrEnum):
    QUEUED = "queued"
    DRAFT = "draft"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UiPart:
    kind: str
    content: str
    part_id: UUID | None = None
    name: str | None = None
    tool_use_id: str | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class UiMessage:
    message_id: UUID
    role: Role
    parts: tuple[UiPart, ...]
    lifecycle: MessageLifecycle


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    messages: tuple[Message, ...]


class UiProjection:
    """由完整快照和可丢失更新派生出的非权威展示状态。"""

    def __init__(self, snapshot: SessionSnapshot | None = None) -> None:
        self._messages: list[UiMessage] = []
        if snapshot is not None:
            self.replace(snapshot)

    @property
    def messages(self) -> tuple[UiMessage, ...]:
        return tuple(self._messages)

    def replace(self, snapshot: SessionSnapshot) -> set[UUID]:
        self._messages = [self._from_message(message) for message in snapshot.messages]
        return {message.message_id for message in self._messages}

    def clear(self) -> set[UUID]:
        dirty = {message.message_id for message in self._messages}
        self._messages.clear()
        return dirty

    def apply(self, update: object) -> set[UUID]:
        if isinstance(update, InputQueued):
            self._upsert(self._from_message(update.message, MessageLifecycle.QUEUED))
            return {update.message.message_id}
        if isinstance(update, InputWithdrawn):
            index = self._index(update.message_id)
            if index is not None and self._messages[index].lifecycle is MessageLifecycle.QUEUED:
                self._messages.pop(index)
                return {update.message_id}
            return set()
        if isinstance(update, UserMessageCommitted):
            self._upsert(self._from_message(update.message))
            return {update.message.message_id}
        if isinstance(update, AssistantMessageStarted):
            self._upsert(UiMessage(update.message_id, Role.ASSISTANT, (), MessageLifecycle.DRAFT))
            return {update.message_id}
        if isinstance(update, AssistantPartDelta):
            index = self._index(update.message_id)
            if index is None:
                return set()
            message = self._messages[index]
            parts = list(message.parts)
            # 连续的同类 delta 属于同一个草稿 Part；终态到达后会由领域 Message 精确替换。
            if parts and parts[-1].kind == update.kind:
                current = parts[-1]
                parts[-1] = UiPart(current.kind, current.content + update.content)
            else:
                parts.append(UiPart(update.kind, update.content))
            self._messages[index] = UiMessage(message.message_id, message.role, tuple(parts), MessageLifecycle.DRAFT)
            return {update.message_id}
        if isinstance(update, AssistantMessageDiscarded):
            index = self._index(update.message_id)
            if index is not None:
                self._messages.pop(index)
                return {update.message_id}
            return set()
        if isinstance(update, (AssistantMessageCompleted, ToolResultCompleted)):
            self._upsert(self._from_message(update.message))
            return {update.message.message_id}
        return set()

    def _index(self, message_id: UUID) -> int | None:
        return next((index for index, item in enumerate(self._messages) if item.message_id == message_id), None)

    def _upsert(self, message: UiMessage) -> None:
        index = self._index(message.message_id)
        if index is None:
            self._messages.append(message)
        else:
            self._messages[index] = message

    @staticmethod
    def _from_message(message: Message, lifecycle: MessageLifecycle = MessageLifecycle.COMPLETED) -> UiMessage:
        parts: list[UiPart] = []
        for part in message.parts:
            if isinstance(part, TextPart):
                parts.append(UiPart("text", part.content, part.part_id))
            elif isinstance(part, ReasoningPart):
                parts.append(UiPart("reasoning", part.content, part.part_id))
            elif isinstance(part, ToolUsePart):
                parts.append(UiPart("tool", part.arguments, part.part_id, part.name, part.tool_use_id))
            elif isinstance(part, ToolResultPart):
                parts.append(UiPart("tool_result", part.content, part.part_id, tool_use_id=part.tool_use_id, is_error=part.is_error))
        return UiMessage(message.message_id, message.role, tuple(parts), lifecycle)

