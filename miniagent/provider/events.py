from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class TextDelta:
    content: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    content: str


@dataclass(frozen=True, slots=True)
class ToolUseDelta:
    index: int
    tool_use_id_fragment: str = ""
    type_fragment: str = ""
    name_fragment: str = ""
    arguments_fragment: str = ""


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    finish_reason: str | None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ResponseFailed:
    category: str
    message: str
    status_code: int | None = None
    provider_code: str | None = None
    provider_type: str | None = None
    request_id: str | None = None


ModelEvent: TypeAlias = TextDelta | ReasoningDelta | ToolUseDelta | ResponseCompleted | ResponseFailed
