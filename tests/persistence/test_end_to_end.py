from collections import deque
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from miniagent.context import ContextManager
from miniagent.domain import Message, Role, StopReason, ToolResult
from miniagent.journal import JournalRecord, JournalRecordType, UserMessagePayload
from miniagent.loop import AgentLoop
from miniagent.ports import Cancellation
from miniagent.provider.events import ResponseCompleted, TextDelta, ToolUseDelta
from miniagent.repository import SessionRepository
from miniagent.session import SessionEngine
from miniagent.trace import JsonlTraceSink


class Model:
    def __init__(self, calls):
        self.calls = deque(calls)
        self.call_count = 0

    async def stream(self, context, tools, options, cancellation):
        self.call_count += 1
        for event in self.calls.popleft():
            yield event


class Tool:
    def __init__(self):
        self.call_count = 0

    def validate_batch(self, batch):
        pass

    async def submit_batch(self, batch, cancellation, pre_tool_use_outcomes=None):
        self.call_count += 1
        return tuple(
            ToolResult(use.tool_use_id, batch.assistant_message_id, "tool-output")
            for use in batch.tool_uses
        )


class BrokenTrace:
    async def emit(self, event):
        raise OSError("trace directory blocked")

    async def close(self, drain_timeout=1.0):
        raise OSError("trace directory blocked")


def user_record(session_id, run_id, message):
    return JournalRecord(
        1, JournalRecordType.USER_MESSAGE, session_id, run_id,
        datetime.now(timezone.utc), UserMessagePayload(message),
    )


async def test_two_runs_restore_exact_transcript_without_replaying_side_effects(tmp_path):
    session_id, first_run, second_run = uuid4(), uuid4(), uuid4()
    first_user = Message.text(Role.USER, "use tool")
    repository = SessionRepository(tmp_path)
    opened = await repository.create_session(
        session_id, user_record(session_id, first_run, first_user)
    )
    engine = SessionEngine(opened)
    model = Model([
        [ToolUseDelta(0, "call-1", name_fragment="demo", arguments_fragment="{}"), ResponseCompleted("tool_calls")],
        [TextDelta("first done"), ResponseCompleted("stop")],
        [TextDelta("second done"), ResponseCompleted("stop")],
    ])
    tool = Tool()
    loop = AgentLoop(model, ContextManager(), tool)

    first_result = await loop.run(
        engine.messages, first_user, "system", 3, engine, Cancellation(), first_run
    )
    second_user = Message.text(Role.USER, "again")
    await engine.commit_user(second_run, second_user)
    second_result = await loop.run(
        engine.messages, second_user, "system", 2, engine, Cancellation(), second_run
    )
    expected_messages = engine.messages
    expected_results = engine.run_results
    await engine.close()

    model_calls, tool_calls = model.call_count, tool.call_count
    restored_handle = await SessionRepository(tmp_path).open_session(session_id)
    restored = SessionEngine(restored_handle)

    assert restored.messages == expected_messages
    assert restored.run_results == expected_results
    assert first_result.reason is StopReason.COMPLETED
    assert second_result.reason is StopReason.COMPLETED
    assert model.call_count == model_calls == 3
    assert tool.call_count == tool_calls == 1
    assert await restored.recover_interrupted() is None
    await restored.close()


async def test_trace_failure_does_not_change_run_or_journal_authority(tmp_path):
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "trace independent")
    opened = await SessionRepository(tmp_path).create_session(
        session_id, user_record(session_id, run_id, user)
    )
    engine = SessionEngine(opened)
    result = await AgentLoop(
        Model([[TextDelta("ok"), ResponseCompleted("stop")]]),
        ContextManager(),
        trace_sink=BrokenTrace(),
    ).run(engine.messages, user, "system", 1, engine, Cancellation(), run_id)
    journal = (tmp_path / str(session_id) / "message.jsonl").read_bytes()
    await engine.close()

    restored = await SessionRepository(tmp_path).open_session(session_id)
    assert result.reason is StopReason.COMPLETED
    assert [record.record_type for record in restored.records] == [
        JournalRecordType.USER_MESSAGE,
        JournalRecordType.ASSISTANT_MESSAGE,
        JournalRecordType.RUN_TERMINATED,
    ]
    assert (tmp_path / str(session_id) / "message.jsonl").read_bytes() == journal
    await restored.close()


async def test_first_user_fsync_failure_leaves_no_visible_session_or_actions(tmp_path):
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "must persist first")
    model_calls = tool_calls = 0

    def fail_sync(file_descriptor):
        raise OSError("fsync failed")

    repository = SessionRepository(tmp_path, sync_file=fail_sync)
    try:
        await repository.create_session(session_id, user_record(session_id, run_id, user))
    except OSError:
        pass
    else:
        raise AssertionError("create_session 应传播 fsync 失败")

    assert await repository.list_sessions() == ()
    assert model_calls == tool_calls == 0


async def test_trace_queue_overflow_does_not_change_completed_journal(tmp_path):
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "overflow")
    opened = await SessionRepository(tmp_path / "sessions").create_session(
        session_id, user_record(session_id, run_id, user)
    )
    engine = SessionEngine(opened)
    gate = asyncio.Event()
    trace = JsonlTraceSink(tmp_path / "trace", queue_capacity=1, writer_gate=gate)
    result = await AgentLoop(
        Model([[TextDelta("ok"), ResponseCompleted("stop")]]),
        ContextManager(),
        trace_sink=trace,
    ).run(engine.messages, user, "system", 1, engine, Cancellation(), run_id)

    assert result.reason is StopReason.COMPLETED
    assert trace.dropped_count > 0
    assert [record.record_type for record in opened.records] == [
        JournalRecordType.USER_MESSAGE,
        JournalRecordType.ASSISTANT_MESSAGE,
        JournalRecordType.RUN_TERMINATED,
    ]
    gate.set()
    await trace.close()
    await engine.close()


async def test_half_tail_is_truncated_and_interrupted_run_is_closed_once(tmp_path):
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "interrupted")
    created = await SessionRepository(tmp_path).create_session(
        session_id, user_record(session_id, run_id, user)
    )
    await created.close()
    journal = tmp_path / str(session_id) / "message.jsonl"
    with journal.open("ab") as stream:
        stream.write(b'{"partial":')

    first_open = await SessionRepository(tmp_path).open_session(session_id)
    engine = SessionEngine(first_open)
    result = await engine.recover_interrupted()
    assert result is not None and result.reason is StopReason.PROCESS_INTERRUPTED
    await engine.close()

    second_open = await SessionRepository(tmp_path).open_session(session_id)
    restored = SessionEngine(second_open)
    assert await restored.recover_interrupted() is None
    assert restored.run_results[-1].reason is StopReason.PROCESS_INTERRUPTED
    await restored.close()
