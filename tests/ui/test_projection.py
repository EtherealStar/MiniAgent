from uuid import uuid4

from miniagent.domain import Message, ReasoningPart, Role, TextPart, ToolResultPart, ToolUsePart
from miniagent.ui.projection import MessageLifecycle, SessionSnapshot, UiProjection
from miniagent.updates import (
    AssistantMessageCompleted,
    AssistantMessageStarted,
    AssistantPartDelta,
    InputQueued,
    InputWithdrawn,
    ToolResultCompleted,
    UserMessageCommitted,
)


def test_projection_moves_the_same_user_message_from_queued_to_completed():
    run_id = uuid4()
    message = Message.text(Role.USER, "queued")
    projection = UiProjection()

    projection.apply(InputQueued(run_id, message))
    assert projection.messages[0].lifecycle is MessageLifecycle.QUEUED

    projection.apply(UserMessageCommitted(message))
    assert projection.messages[0].message_id == message.message_id
    assert projection.messages[0].lifecycle is MessageLifecycle.COMPLETED


def test_withdraw_removes_only_a_queued_message():
    run_id = uuid4()
    queued = Message.text(Role.USER, "remove me")
    committed = Message.text(Role.USER, "keep me")
    projection = UiProjection(SessionSnapshot((committed,)))
    projection.apply(InputQueued(run_id, queued))

    projection.apply(InputWithdrawn(queued.message_id))

    assert [item.message_id for item in projection.messages] == [committed.message_id]


def test_completed_assistant_replaces_draft_and_preserves_part_order():
    message_id = uuid4()
    projection = UiProjection()
    projection.apply(AssistantMessageStarted(message_id))
    projection.apply(AssistantPartDelta(message_id, "reasoning", "think"))
    projection.apply(AssistantPartDelta(message_id, "text", "answer"))
    message = Message(
        role=Role.ASSISTANT,
        message_id=message_id,
        parts=(ReasoningPart("think"), TextPart("answer")),
    )

    projection.apply(AssistantMessageCompleted(message, "stop"))

    ui_message = projection.messages[0]
    assert [part.kind for part in ui_message.parts] == ["reasoning", "text"]
    assert ui_message.lifecycle is MessageLifecycle.COMPLETED


def test_snapshot_replaces_all_transient_projection_state():
    projection = UiProjection()
    projection.apply(InputQueued(uuid4(), Message.text(Role.USER, "old")))
    restored = Message.text(Role.ASSISTANT, "restored")

    dirty = projection.replace(SessionSnapshot((restored,)))

    assert dirty == {restored.message_id}
    assert [item.message_id for item in projection.messages] == [restored.message_id]


def _assistant_with_tool_use(tool_use_id: str = "tu-1") -> Message:
    return Message(
        role=Role.ASSISTANT,
        parts=(ToolUsePart("read", '{"path": "a.py"}', tool_use_id),),
    )


def _tool_result_message(tool_use_id: str, assistant_message_id, content: str, *, is_error=False) -> Message:
    return Message(
        role=Role.TOOL,
        parts=(ToolResultPart(tool_use_id, assistant_message_id, content, is_error=is_error),),
    )


def test_tool_result_merges_into_the_calling_tool_part():
    projection = UiProjection()
    assistant = _assistant_with_tool_use()
    projection.apply(AssistantMessageCompleted(assistant, "tool_calls"))
    tool_part = projection.messages[0].parts[0]
    assert tool_part.result is None  # 结果到达前处于"等待结果"状态

    projection.apply(ToolResultCompleted(_tool_result_message("tu-1", assistant.message_id, "文件内容")))

    # 结果内联进 assistant 块，不产生独立消息。
    assert len(projection.messages) == 1
    merged = projection.messages[0].parts[0]
    assert merged.result == "文件内容"
    assert merged.is_error is False


def test_tool_result_propagates_is_error():
    projection = UiProjection()
    assistant = _assistant_with_tool_use()
    projection.apply(AssistantMessageCompleted(assistant, "tool_calls"))

    projection.apply(ToolResultCompleted(_tool_result_message("tu-1", assistant.message_id, "爆炸了", is_error=True)))

    merged = projection.messages[0].parts[0]
    assert merged.result == "爆炸了"
    assert merged.is_error is True


def test_orphan_tool_result_stays_an_independent_message():
    projection = UiProjection()
    orphan = _tool_result_message("tu-未知", uuid4(), "无处可去")

    projection.apply(ToolResultCompleted(orphan))

    assert len(projection.messages) == 1
    assert projection.messages[0].role is Role.TOOL
    assert projection.messages[0].parts[0].content == "无处可去"


def test_snapshot_merges_tool_results_after_replace():
    assistant = _assistant_with_tool_use()
    result = _tool_result_message("tu-1", assistant.message_id, "历史结果")

    projection = UiProjection(SessionSnapshot((assistant, result)))

    assert len(projection.messages) == 1
    assert projection.messages[0].parts[0].result == "历史结果"

