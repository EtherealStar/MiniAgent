from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from ..domain import Message, Part, ReasoningPart, Role, TextPart, ToolResultPart, ToolUsePart
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
    # 工具结果内容；None 表示该 ToolUse 仍在等待结果。
    result: str | None = None


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
    """由完整快照和可丢失更新派生出的非权威展示状态。

    领域规则规定 ToolResultPart 只能属于 Role.TOOL 的独立消息；展示层
    要求工具结果内联在发起调用的 assistant 块中（textual-ui.md §9.3，
    完成顺序不改变布局）。因此投影按 tool_use_id 把 TOOL 消息合并进
    对应的 tool part，无法配对的保留为独立消息。
    """

    def __init__(self, snapshot: SessionSnapshot | None = None) -> None:
        self._messages: list[UiMessage] = []
        self._tool_uses: dict[str, UUID] = {}
        if snapshot is not None:
            self.replace(snapshot)

    @property
    def messages(self) -> tuple[UiMessage, ...]:
        return tuple(self._messages)

    def replace(self, snapshot: SessionSnapshot) -> set[UUID]:
        self._messages = []
        self._tool_uses = {}
        dirty: set[UUID] = set()
        for message in snapshot.messages:
            dirty |= self._ingest(message, MessageLifecycle.COMPLETED)
        return dirty

    def clear(self) -> set[UUID]:
        dirty = {message.message_id for message in self._messages}
        self._messages.clear()
        self._tool_uses.clear()
        return dirty

    def apply(self, update: object) -> set[UUID]:
        if isinstance(update, InputQueued):
            return self._ingest(update.message, MessageLifecycle.QUEUED)
        if isinstance(update, InputWithdrawn):
            index = self._index(update.message_id)
            if index is not None and self._messages[index].lifecycle is MessageLifecycle.QUEUED:
                self._messages.pop(index)
                return {update.message_id}
            return set()
        if isinstance(update, UserMessageCommitted):
            return self._ingest(update.message, MessageLifecycle.COMPLETED)
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
                parts[-1] = replace(current, content=current.content + update.content)
            else:
                parts.append(UiPart(update.kind, update.content))
            self._messages[index] = replace(message, parts=tuple(parts))
            return {update.message_id}
        if isinstance(update, AssistantMessageDiscarded):
            index = self._index(update.message_id)
            if index is not None:
                self._messages.pop(index)
                return {update.message_id}
            return set()
        if isinstance(update, (AssistantMessageCompleted, ToolResultCompleted)):
            return self._ingest(update.message, MessageLifecycle.COMPLETED)
        return set()

    def _ingest(self, message: Message, lifecycle: MessageLifecycle) -> set[UUID]:
        if message.role is Role.TOOL:
            return self._merge_tool_results(message)
        ui = UiMessage(
            message.message_id,
            message.role,
            tuple(self._to_ui_part(part) for part in message.parts),
            lifecycle,
        )
        for part in ui.parts:
            if part.kind == "tool" and part.tool_use_id:
                self._tool_uses[part.tool_use_id] = ui.message_id
        self._upsert(ui)
        return {ui.message_id}

    def _merge_tool_results(self, message: Message) -> set[UUID]:
        dirty: set[UUID] = set()
        leftover: list[UiPart] = []
        for part in message.parts:
            ui_part = self._to_ui_part(part)
            target_id = self._tool_uses.get(ui_part.tool_use_id or "")
            index = self._index(target_id) if target_id is not None else None
            if index is None:
                # 结果找不到发起它的 ToolUse（例如历史边界），保留为独立消息。
                leftover.append(ui_part)
                continue
            target = self._messages[index]
            updated = tuple(
                replace(existing, result=ui_part.content, is_error=ui_part.is_error)
                if existing.kind == "tool" and existing.tool_use_id == ui_part.tool_use_id
                else existing
                for existing in target.parts
            )
            self._messages[index] = replace(target, parts=updated)
            dirty.add(target.message_id)
        if leftover:
            self._upsert(UiMessage(message.message_id, Role.TOOL, tuple(leftover), MessageLifecycle.COMPLETED))
            dirty.add(message.message_id)
        return dirty

    def _index(self, message_id: UUID | None) -> int | None:
        if message_id is None:
            return None
        return next((index for index, item in enumerate(self._messages) if item.message_id == message_id), None)

    def _upsert(self, message: UiMessage) -> None:
        index = self._index(message.message_id)
        if index is None:
            self._messages.append(message)
        else:
            self._messages[index] = message

    @staticmethod
    def _to_ui_part(part: Part) -> UiPart:
        if isinstance(part, TextPart):
            return UiPart("text", part.content, part.part_id)
        if isinstance(part, ReasoningPart):
            return UiPart("reasoning", part.content, part.part_id)
        if isinstance(part, ToolUsePart):
            return UiPart("tool", part.arguments, part.part_id, part.name, part.tool_use_id)
        if isinstance(part, ToolResultPart):
            return UiPart("tool_result", part.content, part.part_id, tool_use_id=part.tool_use_id, is_error=part.is_error)
        raise TypeError(f"未知的 Part 类型：{type(part)!r}")
