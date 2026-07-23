from collections import deque
from datetime import datetime, timezone
from uuid import uuid4

from miniagent.context import ContextBuilder
from miniagent.domain import Message, Role, StopReason, ToolResult
from miniagent.journal import JournalRecord, JournalRecordType, UserMessagePayload
from miniagent.loop import AgentLoop
from miniagent.ports import Cancellation
from miniagent.provider.events import ResponseCompleted, ResponseFailed, TextDelta, ToolUseDelta
from miniagent.repository import SessionRepository
from miniagent.session import SessionEngine


class ScriptedModel:
    def __init__(self, calls, timeline=None):
        self.calls = deque(calls)
        self.contexts = []
        self.timeline = timeline

    async def stream(self, context, tools, options, cancellation):
        if self.timeline is not None:
            self.timeline.append("model")
        self.contexts.append(context)
        for event in self.calls.popleft():
            yield event


class ReverseExecutor:
    def __init__(self, timeline=None):
        self.batches = []
        self.timeline = timeline

    async def submit_batch(self, batch, cancellation):
        if self.timeline is not None:
            self.timeline.append("tool")
        self.batches.append(batch)
        return tuple(
            ToolResult(tool.tool_use_id, batch.assistant_message_id, f"result-{tool.name}")
            for tool in reversed(batch.tool_uses)
        )


async def run_loop(tmp_path, model, *, max_turns=4, executor=None, cancellation=None, budget=16000, sync_file=None):
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "go")
    first = JournalRecord(
        1, JournalRecordType.USER_MESSAGE, session_id, run_id,
        datetime.now(timezone.utc), UserMessagePayload(user),
    )
    kwargs = {} if sync_file is None else {"sync_file": sync_file}
    opened = await SessionRepository(tmp_path, **kwargs).create_session(session_id, first)
    session = SessionEngine(opened)
    signal = cancellation or Cancellation()
    result = await AgentLoop(model, ContextBuilder(), executor, context_budget=budget).run(
        session.messages, user, "system", max_turns, session, signal, run_id
    )
    return result, session


async def test_user_fsync_precedes_model_and_single_turn_completes(tmp_path):
    timeline = []

    def sync_file(file_descriptor):
        timeline.append("fsync")

    model = ScriptedModel([[TextDelta("ok"), ResponseCompleted("stop")]], timeline)
    result, session = await run_loop(tmp_path, model, sync_file=sync_file)

    assert result.reason is StopReason.COMPLETED and result.turn_count == 1
    assert timeline.index("fsync") < timeline.index("model")
    assert [message.role for message in session.messages] == [Role.USER, Role.ASSISTANT]
    await session.close()


async def test_assistant_fsync_precedes_tools_and_results_keep_call_order(tmp_path):
    timeline = []

    def sync_file(file_descriptor):
        timeline.append("fsync")

    first = [
        ToolUseDelta(0, "a", name_fragment="first", arguments_fragment="{}"),
        ToolUseDelta(1, "b", name_fragment="second", arguments_fragment="{}"),
        ResponseCompleted("tool_calls"),
    ]
    model = ScriptedModel([first, [TextDelta("done"), ResponseCompleted("stop")]], timeline)
    executor = ReverseExecutor(timeline)
    result, session = await run_loop(tmp_path, model, executor=executor, sync_file=sync_file)

    assert result.reason is StopReason.COMPLETED and result.turn_count == 2
    assert timeline[timeline.index("tool") - 1] == "fsync"
    tools = [message.parts[0].tool_use_id for message in session.messages if message.role is Role.TOOL]
    assert tools == ["a", "b"]
    assert [message.parts[0].tool_use_id for message in model.contexts[1].messages if message.role is Role.TOOL] == ["a", "b"]
    await session.close()


async def test_interrupted_draft_is_not_committed(tmp_path):
    model = ScriptedModel([
        [TextDelta("partial"), ResponseFailed("connection_error", "lost")],
        [TextDelta("complete"), ResponseCompleted("stop")],
    ])
    result, session = await run_loop(tmp_path, model)
    assert result.turn_count == 2
    assert all("partial" not in getattr(part, "content", "") for message in session.messages for part in message.parts)
    await session.close()


async def test_missing_terminal_is_reported_in_stream_summary(tmp_path):
    from miniagent.trace import MemoryTraceSink, TraceEventType

    trace = MemoryTraceSink()
    model = ScriptedModel([[TextDelta("partial")], [TextDelta("done"), ResponseCompleted("stop")]])
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "go")
    first = JournalRecord(1, JournalRecordType.USER_MESSAGE, session_id, run_id, datetime.now(timezone.utc), UserMessagePayload(user))
    opened = await SessionRepository(tmp_path).create_session(session_id, first)
    session = SessionEngine(opened)
    await AgentLoop(model, ContextBuilder(), trace_sink=trace).run(
        session.messages, user, "system", 2, session, Cancellation(), run_id
    )
    summaries = [event for event in trace.events if event.event_type is TraceEventType.STREAM_SUMMARY]
    assert summaries[0].payload["terminal_received"] is False
    await session.close()


async def test_length_continuation_and_max_turns(tmp_path):
    model = ScriptedModel([
        [TextDelta("part1"), ResponseCompleted("length")],
        [TextDelta("part2"), ResponseCompleted("length")],
    ])
    result, session = await run_loop(tmp_path, model, max_turns=2)
    assistants = [message for message in session.messages if message.role is Role.ASSISTANT]
    assert result.reason is StopReason.MAX_TURNS and result.turn_count == 2
    assert assistants[1].continuation_of_message_id == assistants[0].message_id
    await session.close()


async def test_last_turn_tools_execute_then_max_turns(tmp_path):
    executor = ReverseExecutor()
    model = ScriptedModel([[ToolUseDelta(0, "a", name_fragment="read", arguments_fragment="{}"), ResponseCompleted("tool_calls")]])
    result, session = await run_loop(tmp_path, model, max_turns=1, executor=executor)
    assert result.reason is StopReason.MAX_TURNS
    assert len(executor.batches) == 1
    await session.close()


async def test_prompt_too_long_stops_when_compression_cannot_reduce_input(tmp_path):
    model = ScriptedModel([
        [ResponseFailed("client_error", "too long", provider_code="context_length_exceeded")],
        [ResponseFailed("client_error", "still too long", provider_code="context_length_exceeded")],
    ])
    result, session = await run_loop(tmp_path, model, budget=100)
    assert result.reason is StopReason.PROMPT_TOO_LONG and result.turn_count == 1
    assert len(model.contexts) == 1
    await session.close()


async def test_pre_cancel_does_not_start_model_call(tmp_path):
    cancellation = Cancellation()
    cancellation.cancel()
    model = ScriptedModel([])
    result, session = await run_loop(tmp_path, model, cancellation=cancellation)
    assert result.reason is StopReason.CANCELLED and result.turn_count == 0
    assert model.contexts == []
    await session.close()


async def test_assistant_commit_failure_does_not_start_tool(tmp_path):
    syncs = 0

    def fail_assistant(file_descriptor):
        nonlocal syncs
        syncs += 1
        if syncs == 2:
            raise OSError("disk full")

    executor = ReverseExecutor()
    model = ScriptedModel([[ToolUseDelta(0, "a", name_fragment="read", arguments_fragment="{}"), ResponseCompleted("tool_calls")]])
    result, session = await run_loop(tmp_path, model, executor=executor, sync_file=fail_assistant)
    assert result.reason is StopReason.EVENT_COMMIT_FAILED
    assert executor.batches == []
    await session.close()
