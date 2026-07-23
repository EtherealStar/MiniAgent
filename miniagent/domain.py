from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias
from uuid import UUID, uuid4


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ReasoningSource(StrEnum):
    STRUCTURED = "structured"
    THINK_TAG = "think_tag"


class ReasoningVisibility(StrEnum):
    COLLAPSED = "collapsed"
    HIDDEN = "hidden"
    VISIBLE = "visible"


class StopReason(StrEnum):
    COMPLETED = "COMPLETED"
    MAX_TURNS = "MAX_TURNS"
    PROMPT_TOO_LONG = "PROMPT_TOO_LONG"
    CANCELLED = "CANCELLED"
    PROCESS_INTERRUPTED = "PROCESS_INTERRUPTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    EVENT_COMMIT_FAILED = "EVENT_COMMIT_FAILED"


@dataclass(frozen=True, slots=True)
class TextPart:
    content: str
    part_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class ReasoningPart:
    content: str
    source: ReasoningSource = ReasoningSource.STRUCTURED
    visibility: ReasoningVisibility = ReasoningVisibility.COLLAPSED
    part_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class ToolUsePart:
    name: str
    arguments: str
    tool_use_id: str = field(default_factory=lambda: str(uuid4()))
    part_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.name or not self.tool_use_id:
            raise ValueError("工具名称和 tool_use_id 不能为空")


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    tool_use_id: str
    assistant_message_id: UUID
    content: str
    is_error: bool = False
    outcome_unknown: bool = False
    part_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.tool_use_id:
            raise ValueError("工具结果必须关联 tool_use_id")


Part: TypeAlias = TextPart | ReasoningPart | ToolUsePart | ToolResultPart


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    parts: tuple[Part, ...]
    message_id: UUID = field(default_factory=uuid4)
    continuation_of_message_id: UUID | None = None
    retry_of_message_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("消息至少包含一个 Part")
        tool_ids = [part.tool_use_id for part in self.parts if isinstance(part, ToolUsePart)]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("同一消息中 tool_use_id 不得重复")
        if self.role is Role.TOOL and not all(isinstance(p, ToolResultPart) for p in self.parts):
            raise ValueError("tool 消息只能包含 ToolResultPart")
        if self.role is not Role.TOOL and any(isinstance(p, ToolResultPart) for p in self.parts):
            raise ValueError("ToolResultPart 只能属于 tool 消息")

    @classmethod
    def text(cls, role: Role, content: str) -> Message:
        return cls(role=role, parts=(TextPart(content=content),))


@dataclass(frozen=True, slots=True)
class ContextSummary:
    covers_through_message_id: UUID
    resume_from_message_id: UUID | None
    summary: str
    summary_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    category: str
    message: str


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    reason: StopReason
    turn_count: int
    final_message_id: UUID | None = None
    error: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    function_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("工具名称不能为空")
        object.__setattr__(self, "function_schema", MappingProxyType(dict(self.function_schema)))


@dataclass(frozen=True, slots=True)
class ToolExecutionBatch:
    run_id: UUID
    assistant_message_id: UUID
    tool_uses: tuple[ToolUsePart, ...]
    trace_id: UUID | None = None
    parent_span_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_use_id: str
    assistant_message_id: UUID
    content: str
    is_error: bool = False
    outcome_unknown: bool = False
    tool_name: str = ""
    status: str = "success"
    attempts: int = 1
    failure: Any | None = None
    artifact: Any | None = None


def part_to_dict(part: Part) -> dict[str, Any]:
    data: dict[str, Any] = {"type": type(part).__name__, "part_id": str(part.part_id)}
    if isinstance(part, (TextPart, ReasoningPart)):
        data["content"] = part.content
    if isinstance(part, ReasoningPart):
        data.update(source=part.source.value, visibility=part.visibility.value)
    if isinstance(part, ToolUsePart):
        data.update(name=part.name, arguments=part.arguments, tool_use_id=part.tool_use_id)
    if isinstance(part, ToolResultPart):
        data.update(
            tool_use_id=part.tool_use_id,
            assistant_message_id=str(part.assistant_message_id),
            content=part.content,
            is_error=part.is_error,
            outcome_unknown=part.outcome_unknown,
        )
    return data


def message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "message_id": str(message.message_id),
        "role": message.role.value,
        "parts": [part_to_dict(part) for part in message.parts],
        "continuation_of_message_id": str(message.continuation_of_message_id) if message.continuation_of_message_id else None,
        "retry_of_message_id": str(message.retry_of_message_id) if message.retry_of_message_id else None,
    }


def message_from_dict(data: Mapping[str, Any]) -> Message:
    parts: list[Part] = []
    for raw in data["parts"]:
        common = {"part_id": UUID(raw["part_id"])}
        kind = raw["type"]
        if kind == "TextPart":
            parts.append(TextPart(content=raw["content"], **common))
        elif kind == "ReasoningPart":
            parts.append(ReasoningPart(content=raw["content"], source=ReasoningSource(raw["source"]), visibility=ReasoningVisibility(raw["visibility"]), **common))
        elif kind == "ToolUsePart":
            parts.append(ToolUsePart(name=raw["name"], arguments=raw["arguments"], tool_use_id=raw["tool_use_id"], **common))
        elif kind == "ToolResultPart":
            parts.append(ToolResultPart(tool_use_id=raw["tool_use_id"], assistant_message_id=UUID(raw["assistant_message_id"]), content=raw["content"], is_error=raw["is_error"], outcome_unknown=raw["outcome_unknown"], **common))
        else:
            raise ValueError(f"未知 Part 类型: {kind}")
    return Message(
        message_id=UUID(data["message_id"]),
        role=Role(data["role"]),
        parts=tuple(parts),
        continuation_of_message_id=UUID(data["continuation_of_message_id"]) if data.get("continuation_of_message_id") else None,
        retry_of_message_id=UUID(data["retry_of_message_id"]) if data.get("retry_of_message_id") else None,
    )

