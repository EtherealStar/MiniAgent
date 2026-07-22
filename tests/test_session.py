from uuid import uuid4

import pytest

from miniagent.domain import Message, Role
from miniagent.events import AssistantMessageCompleted, AssistantMessageStarted, UserMessageRecorded
from miniagent.session import SessionEngine
from miniagent.storage import JsonlTranscriptStore


async def test_sequence_idempotence_and_ui_failure():
    async def broken_ui(event):
        raise ConnectionError("offline")

    session = SessionEngine(ui_sink=broken_ui)
    run_id, _ = session.begin_run()
    user = Message.text(Role.USER, "hello")
    payload = UserMessageRecorded(message=user)
    first = await session.emit(payload)
    duplicate = await session.emit(payload)
    message = Message.text(Role.ASSISTANT, "world")
    await session.emit(AssistantMessageStarted(message_id=message.message_id))
    await session.emit(AssistantMessageCompleted(message=message, finish_reason="stop"))
    assert duplicate is first
    assert [event.sequence for event in session.events] == [1, 2, 3]
    assert len(session.ui_delivery_errors) == 3
    assert session.replay_after(1) == session.events[1:]
    session.finish_run(run_id)


async def test_new_input_is_queued_and_cancel_is_scoped():
    session = SessionEngine()
    run_id, cancellation = session.begin_run()
    queued_id = await session.enqueue_input(Message.text(Role.USER, "later"))
    assert not cancellation.cancelled
    assert session.cancel(run_id)
    assert cancellation.cancelled
    queued = await session.next_input()
    assert queued.run_id == queued_id


async def test_recovery_discards_unfinished_draft_once():
    session = SessionEngine()
    run_id, _ = session.begin_run()
    message_id = uuid4()
    await session.emit(AssistantMessageStarted(message_id=message_id))
    session.finish_run(run_id)
    assert await session.recover_interrupted(run_id) == (message_id,)
    assert await session.recover_interrupted(run_id) == ()


async def test_jsonl_transcript_receives_accepted_events(tmp_path):
    path = tmp_path / "events.jsonl"
    session = SessionEngine(transcript_store=JsonlTranscriptStore(path))
    session.begin_run()
    await session.emit(UserMessageRecorded(message=Message.text(Role.USER, "持久化")))
    content = path.read_text(encoding="utf-8")
    assert '"sequence":1' in content
    assert '"type":"UserMessageRecorded"' in content
