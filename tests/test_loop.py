from collections import deque

from miniagent.context import ContextBuilder
from miniagent.domain import Message, Role, StopReason, ToolResult
from miniagent.events import AssistantMessageDiscarded, AssistantMessageStarted, ToolResultRecorded
from miniagent.loop import AgentLoop
from miniagent.ports import Cancellation
from miniagent.provider.events import ResponseCompleted, ResponseFailed, TextDelta, ToolUseDelta
from miniagent.session import SessionEngine
from miniagent.session import EventCommitError


class ScriptedModel:
    def __init__(self, calls):
        self.calls = deque(calls)
        self.contexts = []

    async def stream(self, context, tools, options, cancellation):
        self.contexts.append(context)
        for event in self.calls.popleft():
            yield event


class ReverseExecutor:
    def __init__(self):
        self.batches = []

    async def submit_batch(self, batch, cancellation):
        self.batches.append(batch)
        return tuple(
            ToolResult(tool.tool_use_id, batch.assistant_message_id, f"result-{tool.name}")
            for tool in reversed(batch.tool_uses)
        )


async def run_loop(model, *, max_turns=4, executor=None, cancellation=None, budget=16000):
    session = SessionEngine()
    run_id, session_cancel = session.begin_run()
    signal = cancellation or session_cancel
    result = await AgentLoop(model, ContextBuilder(), executor, context_budget=budget).run(
        (), Message.text(Role.USER, "go"), "system", max_turns, session, signal, run_id
    )
    return result, session


async def test_single_turn_completion():
    result, session = await run_loop(ScriptedModel([[TextDelta("ok"), ResponseCompleted("stop")]]))
    assert result.reason is StopReason.COMPLETED and result.turn_count == 1
    assert [message.role for message in session.messages] == [Role.USER, Role.ASSISTANT]


async def test_tool_results_are_recorded_in_call_order_and_continue():
    first = [
        ToolUseDelta(0, "a", name_fragment="first", arguments_fragment="{}"),
        ToolUseDelta(1, "b", name_fragment="second", arguments_fragment="{}"),
        ResponseCompleted("tool_calls"),
    ]
    model = ScriptedModel([first, [TextDelta("done"), ResponseCompleted("stop")]])
    executor = ReverseExecutor()
    result, session = await run_loop(model, executor=executor)
    assert result.reason is StopReason.COMPLETED and result.turn_count == 2
    recorded = [event.payload.message.parts[0].tool_use_id for event in session.events if isinstance(event.payload, ToolResultRecorded)]
    assert recorded == ["a", "b"]
    assert [message.parts[0].tool_use_id for message in model.contexts[1].messages if message.role is Role.TOOL] == ["a", "b"]


async def test_interrupted_draft_is_discarded_with_new_message_id():
    model = ScriptedModel([
        [TextDelta("partial"), ResponseFailed("connection_error", "lost")],
        [TextDelta("complete"), ResponseCompleted("stop")],
    ])
    result, session = await run_loop(model)
    starts = [event.payload.message_id for event in session.events if isinstance(event.payload, AssistantMessageStarted)]
    assert result.turn_count == 2 and len(set(starts)) == 2
    assert len([event for event in session.events if isinstance(event.payload, AssistantMessageDiscarded)]) == 1
    assert all("partial" not in getattr(part, "content", "") for message in session.messages for part in message.parts)


async def test_length_continuation_and_max_turns():
    model = ScriptedModel([
        [TextDelta("part1"), ResponseCompleted("length")],
        [TextDelta("part2"), ResponseCompleted("length")],
    ])
    result, session = await run_loop(model, max_turns=2)
    assistants = [message for message in session.messages if message.role is Role.ASSISTANT]
    assert result.reason is StopReason.MAX_TURNS and result.turn_count == 2
    assert assistants[1].continuation_of_message_id == assistants[0].message_id


async def test_last_turn_tools_execute_then_max_turns():
    executor = ReverseExecutor()
    model = ScriptedModel([[ToolUseDelta(0, "a", name_fragment="read", arguments_fragment="{}"), ResponseCompleted("tool_calls")]])
    result, session = await run_loop(model, max_turns=1, executor=executor)
    assert result.reason is StopReason.MAX_TURNS
    assert len(executor.batches) == 1
    assert any(isinstance(event.payload, ToolResultRecorded) for event in session.events)


async def test_prompt_too_long_compresses_at_most_once():
    model = ScriptedModel([
        [ResponseFailed("client_error", "too long", provider_code="context_length_exceeded")],
        [ResponseFailed("client_error", "still too long", provider_code="context_length_exceeded")],
    ])
    initial = tuple(Message.text(Role.USER, "x" * 100) for _ in range(4))
    session = SessionEngine()
    run_id, cancellation = session.begin_run()
    result = await AgentLoop(model, ContextBuilder(), context_budget=100).run(
        initial, Message.text(Role.USER, "go"), "system", 5, session, cancellation, run_id
    )
    assert result.reason is StopReason.PROMPT_TOO_LONG and result.turn_count == 2


async def test_pre_cancel_does_not_start_model_call():
    cancellation = Cancellation()
    cancellation.cancel()
    model = ScriptedModel([])
    result, _ = await run_loop(model, cancellation=cancellation)
    assert result.reason is StopReason.CANCELLED and result.turn_count == 0
    assert model.contexts == []


async def test_event_commit_failure_is_structured():
    class RejectingSink:
        def __init__(self):
            self.count = 0

        async def emit(self, payload):
            self.count += 1
            if self.count == 2:
                raise EventCommitError("disk full")

    model = ScriptedModel([[TextDelta("unused"), ResponseCompleted("stop")]])
    result = await AgentLoop(model, ContextBuilder()).run(
        (), Message.text(Role.USER, "go"), "", 2, RejectingSink(), Cancellation()
    )
    assert result.reason is StopReason.EVENT_COMMIT_FAILED
    assert result.turn_count == 0 and model.contexts == []
