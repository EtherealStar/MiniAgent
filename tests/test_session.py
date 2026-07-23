import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from miniagent.domain import AgentRunResult, ErrorInfo, Message, Role, StopReason
from miniagent.journal import JournalRecord, JournalRecordType, UserMessagePayload
from miniagent.repository import SessionRepository
from miniagent.session import EventCommitError, SessionEngine
from miniagent.context import ContextBuilder
from miniagent.loop import AgentLoop
from miniagent.provider.events import ResponseCompleted, TextDelta


def first_record(session_id, run_id, message):
    return JournalRecord(
        1,
        JournalRecordType.USER_MESSAGE,
        session_id,
        run_id,
        datetime.now(timezone.utc),
        UserMessagePayload(message),
    )


async def create_engine(tmp_path, *, sync_file=None, ui_sink=None):
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "hello")
    kwargs = {} if sync_file is None else {"sync_file": sync_file}
    opened = await SessionRepository(tmp_path, **kwargs).create_session(
        session_id, first_record(session_id, run_id, user)
    )
    return SessionEngine(opened, ui_sink=ui_sink), run_id, user


async def test_narrow_commits_update_projection_after_journal_and_ignore_ui_failure(tmp_path):
    async def broken_ui(update):
        raise ConnectionError("offline")

    engine, run_id, user = await create_engine(tmp_path, ui_sink=broken_ui)
    assistant = Message.text(Role.ASSISTANT, "world")

    await engine.commit_assistant(run_id, assistant, "stop")
    await engine.finish_run(run_id, AgentRunResult(StopReason.COMPLETED, 1, assistant.message_id))

    assert engine.messages == (user, assistant)
    assert len(engine.ui_delivery_errors) == 2
    await engine.close()


async def test_failed_commit_does_not_change_transcript(tmp_path):
    calls = 0

    def fail_second_sync(file_descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")

    engine, run_id, user = await create_engine(tmp_path, sync_file=fail_second_sync)

    with pytest.raises(EventCommitError, match="Journal 提交失败"):
        await engine.commit_assistant(run_id, Message.text(Role.ASSISTANT, "lost"), "stop")

    assert engine.messages == (user,)
    await engine.close()


async def test_run_terminal_persists_only_sanitized_error_information(tmp_path):
    engine, run_id, _ = await create_engine(tmp_path)
    secret = "Authorization: Bearer abc123\napi_key=hidden " + "x" * 700
    await engine.finish_run(
        run_id,
        AgentRunResult(StopReason.MODEL_UNAVAILABLE, 0, error=ErrorInfo("provider", secret)),
    )
    persisted = engine.run_results[-1].error
    assert persisted is not None
    assert "abc123" not in persisted.message and "hidden" not in persisted.message
    assert "\n" not in persisted.message and len(persisted.message) <= 512
    await engine.close()


async def test_interrupted_run_is_terminated_once_without_replaying_work(tmp_path):
    engine, run_id, user = await create_engine(tmp_path)
    session_id = engine.session_id
    await engine.close()

    first_open = await SessionRepository(tmp_path).open_session(session_id)
    restored = SessionEngine(first_open)
    result = await restored.recover_interrupted()
    assert result is not None and result.reason is StopReason.PROCESS_INTERRUPTED
    await restored.close()

    second_open = await SessionRepository(tmp_path).open_session(session_id)
    restored_again = SessionEngine(second_open)
    assert await restored_again.recover_interrupted() is None
    assert restored_again.messages == (user,)
    assert restored_again.run_results[-1].reason is StopReason.PROCESS_INTERRUPTED
    await restored_again.close()


async def test_queued_inputs_are_not_persisted_until_the_unique_worker_runs_them(tmp_path):
    class Model:
        def __init__(self):
            self.inputs = []

        async def stream(self, context, tools, options, cancellation):
            users = [message for message in context.messages if message.role is Role.USER]
            self.inputs.append(users[-1].parts[0].content)
            yield TextDelta("ok")
            yield ResponseCompleted("stop")

    engine, first_run, _ = await create_engine(tmp_path)
    await engine.finish_run(first_run, AgentRunResult(StopReason.COMPLETED, 0))
    first = await engine.submit("first queued")
    second = await engine.submit("second queued")
    assert [message.parts[0].content for message in engine.messages if message.role is Role.USER] == ["hello"]

    model = Model()
    loop = AgentLoop(model, ContextBuilder())
    await asyncio.gather(
        engine.run_next(loop, "system", 1),
        engine.run_next(loop, "system", 1),
    )

    assert model.inputs == ["first queued", "second queued"]
    users = [message.message_id for message in engine.messages if message.role is Role.USER]
    assert users[-2:] == [first.message.message_id, second.message.message_id]
    await engine.close()
