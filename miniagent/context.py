from __future__ import annotations

from dataclasses import dataclass

from .domain import ContextSummary, Message, Role, TextPart, ToolResultPart
from .ports import ModelContext


@dataclass(frozen=True, slots=True)
class WorkingContext:
    messages: tuple[Message, ...]
    summary: ContextSummary | None = None


class ContextBuilder:
    def build(
        self,
        working: WorkingContext,
        system_prompt: str,
        budget: int,
        *,
        force_compress: bool = False,
    ) -> ModelContext:
        if budget <= 0:
            raise ValueError("上下文预算必须为正数")
        source = list(working.messages)
        projected = source
        if working.summary is not None:
            start = self._resume_index(source, working.summary)
            projected = source[start:]
        elif force_compress or self._size(source) + len(system_prompt) > budget:
            projected = self._fit_tail(source, max(1, budget // 2))

        messages: list[Message] = []
        if system_prompt:
            messages.append(Message.text(Role.SYSTEM, system_prompt))
        if working.summary is not None:
            messages.append(Message.text(Role.SYSTEM, "上下文摘要：" + working.summary.summary))
        elif projected is not source and source:
            covered = source[: len(source) - len(projected)]
            text = self._summarize(covered, max(8, budget // 4))
            if text:
                messages.append(Message.text(Role.SYSTEM, "上下文摘要：" + text))
        messages.extend(self._trim_tool_results(projected, max(64, budget // 4)))
        return ModelContext(messages=tuple(messages))

    def force_compress(self, working: WorkingContext, system_prompt: str, budget: int) -> ModelContext:
        return self.build(working, system_prompt, max(1, budget // 2), force_compress=True)

    @staticmethod
    def _resume_index(messages: list[Message], summary: ContextSummary) -> int:
        if summary.resume_from_message_id is not None:
            for index, message in enumerate(messages):
                if message.message_id == summary.resume_from_message_id:
                    return index
        for index, message in enumerate(messages):
            if message.message_id == summary.covers_through_message_id:
                return index + 1
        return 0

    @staticmethod
    def _size(messages: list[Message]) -> int:
        return sum(len(getattr(part, "content", getattr(part, "arguments", ""))) for message in messages for part in message.parts)

    def _fit_tail(self, messages: list[Message], budget: int) -> list[Message]:
        kept: list[Message] = []
        used = 0
        for message in reversed(messages):
            size = self._size([message])
            if kept and used + size > budget:
                break
            kept.append(message)
            used += size
        return list(reversed(kept))

    @staticmethod
    def _summarize(messages: list[Message], limit: int) -> str:
        chunks = [getattr(part, "content", "") for message in messages for part in message.parts]
        return " ".join(filter(None, chunks))[:limit]

    @staticmethod
    def _trim_tool_results(messages: list[Message], limit: int) -> list[Message]:
        result: list[Message] = []
        for message in messages:
            parts = tuple(
                ToolResultPart(
                    tool_use_id=part.tool_use_id,
                    assistant_message_id=part.assistant_message_id,
                    content=part.content[:limit],
                    is_error=part.is_error,
                    outcome_unknown=part.outcome_unknown,
                    part_id=part.part_id,
                ) if isinstance(part, ToolResultPart) and len(part.content) > limit else part
                for part in message.parts
            )
            result.append(Message(role=message.role, parts=parts, message_id=message.message_id, continuation_of_message_id=message.continuation_of_message_id, retry_of_message_id=message.retry_of_message_id))
        return result
