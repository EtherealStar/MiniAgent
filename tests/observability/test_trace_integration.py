import json
from collections import deque
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from miniagent.context import ContextManager
from miniagent.domain import Message, Role
from miniagent.journal import JournalRecord, JournalRecordType, UserMessagePayload
from miniagent.loop import AgentLoop
from miniagent.ports import Cancellation
from miniagent.provider.events import ResponseCompleted, TextDelta, ToolUseDelta
from miniagent.repository import SessionRepository
from miniagent.session import SessionEngine
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.models import ExecutionTraits, ToolSpec
from miniagent.tools.registry import ToolRegistry
from miniagent.trace import MemoryTraceSink, TraceEventType


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


class Model:
    def __init__(self, calls):
        self.calls = deque(calls)

    async def stream(self, context, tools, options, cancellation):
        for event in self.calls.popleft():
            yield event


async def test_run_model_and_tool_spans_share_one_parent_tree_without_content(tmp_path):
    async def handler(args, context):
        return "private-result"

    spec = ToolSpec(
        "demo",
        Input,
        handler,
        classify=lambda args, targets: ExecutionTraits(True),
    )
    registry = ToolRegistry([spec])
    registry.freeze()
    trace = MemoryTraceSink()
    session_id, run_id = uuid4(), uuid4()
    user = Message.text(Role.USER, "private-prompt")
    opened = await SessionRepository(tmp_path / "sessions").create_session(
        session_id,
        JournalRecord(
            1, JournalRecordType.USER_MESSAGE, session_id, run_id,
            datetime.now(timezone.utc), UserMessagePayload(user),
        ),
    )
    session = SessionEngine(opened)
    executor = ToolExecutor(
        registry.enabled_view(), tmp_path, str(session_id), trace_sink=trace
    )
    arguments = json.dumps({"value": "private-argument", "correction_of_tool_use_id": None})
    model = Model([
        [ToolUseDelta(0, "call", name_fragment="demo", arguments_fragment=arguments), ResponseCompleted("tool_calls")],
        [TextDelta("done"), ResponseCompleted("stop")],
    ])

    await AgentLoop(
        model,
        ContextManager(),
        executor,
        tools=registry.enabled_view().specs,
        trace_sink=trace,
    ).run(session.messages, user, "system", 3, session, Cancellation(), run_id)

    starts = [
        event
        for event in trace.events
        if event.event_type is TraceEventType.SPAN_STARTED
    ]
    run = next(event for event in starts if event.payload["name"] == "agent.run")
    tool = next(event for event in starts if event.payload["name"] == "tool.call")
    turn = next(event for event in starts if event.context.span_id == tool.context.parent_span_id)
    model_call = next(
        event for event in starts
        if event.payload["name"] == "model.call"
        and event.context.parent_span_id == turn.context.span_id
    )
    assert turn.context.parent_span_id == run.context.span_id
    assert model_call.context.parent_span_id == turn.context.span_id
    assert tool.context.parent_span_id == turn.context.span_id
    assert {event.context.trace_id for event in (run, turn, model_call, tool)} == {run.context.trace_id}
    serialized_payloads = json.dumps([dict(event.payload) for event in trace.events])
    assert "private-prompt" not in serialized_payloads
    assert "private-argument" not in serialized_payloads
    assert "private-result" not in serialized_payloads
    await session.close()
