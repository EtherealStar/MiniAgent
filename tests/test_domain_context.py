from dataclasses import FrozenInstanceError

import pytest

from miniagent.context import ContextBuilder, WorkingContext
from miniagent.domain import (
    ContextSummary,
    Message,
    Role,
    TextPart,
    ToolResultPart,
    ToolUsePart,
    message_from_dict,
    message_to_dict,
)


def test_message_round_trip_and_immutability():
    message = Message.text(Role.USER, "hello")
    assert message_from_dict(message_to_dict(message)) == message
    with pytest.raises(FrozenInstanceError):
        message.role = Role.SYSTEM  # type: ignore[misc]


def test_duplicate_tool_use_id_is_rejected():
    with pytest.raises(ValueError, match="不得重复"):
        Message(role=Role.ASSISTANT, parts=(ToolUsePart("a", "{}", "same"), ToolUsePart("b", "{}", "same")))


def test_tool_result_must_belong_to_tool_message():
    assistant = Message(role=Role.ASSISTANT, parts=(ToolUsePart("a", "{}", "call"),))
    part = ToolResultPart("call", assistant.message_id, "ok")
    with pytest.raises(ValueError):
        Message(role=Role.USER, parts=(part,))


def test_context_summary_preserves_original_messages():
    first = Message.text(Role.USER, "old " * 100)
    second = Message.text(Role.ASSISTANT, "new")
    source = (first, second)
    summary = ContextSummary(first.message_id, second.message_id, "old summary")
    context = ContextBuilder().build(WorkingContext(source, summary), "system", 100)
    assert source == (first, second)
    assert context.messages[-1].message_id == second.message_id
    assert all(message.message_id != first.message_id for message in context.messages)


def test_tool_result_trimming_only_changes_projection():
    assistant = Message(role=Role.ASSISTANT, parts=(ToolUsePart("a", "{}", "call"),))
    result = Message(role=Role.TOOL, parts=(ToolResultPart("call", assistant.message_id, "x" * 1000),))
    context = ContextBuilder().build(WorkingContext((assistant, result)), "", 100)
    projected = context.messages[-1].parts[0]
    assert isinstance(projected, ToolResultPart)
    assert len(projected.content) < len(result.parts[0].content)
    assert len(result.parts[0].content) == 1000
