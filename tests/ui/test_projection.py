from uuid import uuid4

from miniagent.domain import Message, ReasoningPart, Role, TextPart
from miniagent.ui.projection import MessageLifecycle, SessionSnapshot, UiProjection
from miniagent.updates import (
    AssistantMessageCompleted,
    AssistantMessageStarted,
    AssistantPartDelta,
    InputQueued,
    InputWithdrawn,
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

