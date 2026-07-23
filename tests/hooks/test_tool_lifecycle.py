import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from miniagent.context import SystemContext
from miniagent.domain import Message, Role, StopReason
from miniagent.hooks import (
    ContinueToolUse,
    FastToolValidationHook,
    HookDispatcher,
    HookRegistry,
)
from miniagent.loop import AgentLoop
from miniagent.ports import Cancellation, ModelContext
from miniagent.provider.events import ResponseCompleted, TextDelta, ToolUseDelta
from miniagent.session import EventCommitError
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.models import ExecutionTraits, ToolSpec
from miniagent.tools.models import ToolProtocolError
from miniagent.tools.registry import ToolRegistry


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


class PassthroughContextManager:
    async def start_run(self, prompt_inputs):
        return SystemContext("system")

    async def before_model_call(self, working, environment, tools, session, **kwargs):
        return ModelContext(working.messages)

    async def request_compression(self, working, environment, tools, session, **kwargs):
        raise AssertionError("本场景不应压缩")

    def record_actual_prompt_tokens(self, *args):
        pass


class ScriptedModel:
    def __init__(self, calls):
        self.calls = list(calls)

    async def stream(self, context, tools, options, cancellation):
        for event in self.calls.pop(0):
            yield event


class Committer:
    def __init__(self, user, timeline):
        self.session_id = uuid4()
        self.context_summaries = ()
        self.messages = [user]
        self.timeline = timeline
        self.result = None

    async def publish_live(self, update):
        pass

    async def commit_assistant(self, run_id, message, finish_reason):
        self.messages.append(message)
        self.timeline.append("assistant_commit")

    async def commit_tool_result(self, run_id, message):
        self.messages.append(message)
        self.timeline.append(f"result_commit:{message.parts[0].tool_use_id}")

    async def commit_context_summary(self, run_id, summary):
        raise AssertionError("本场景不应提交摘要")

    async def finish_run(self, run_id, result):
        self.result = result


class RecordingExecutor(ToolExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.results = ()
        self.submit_calls = 0

    async def submit_batch(self, *args, **kwargs):
        self.submit_calls += 1
        self.results = await super().submit_batch(*args, **kwargs)
        return self.results


class FailingCommitter(Committer):
    def __init__(self, user, timeline, phase):
        super().__init__(user, timeline)
        self.phase = phase

    async def commit_assistant(self, run_id, message, finish_reason):
        if self.phase == "assistant":
            raise EventCommitError("assistant commit failed")
        await super().commit_assistant(run_id, message, finish_reason)

    async def commit_tool_result(self, run_id, message):
        if self.phase == "tool":
            raise EventCommitError("tool commit failed")
        await super().commit_tool_result(run_id, message)


def make_runtime(tmp_path, timeline):
    handler_calls = []

    async def handler(args, context):
        handler_calls.append(context.tool_use_id)
        timeline.append(f"handler:{context.tool_use_id}")
        return args.value

    spec = ToolSpec(
        "echo",
        Input,
        handler,
        classify=lambda args, targets: ExecutionTraits(concurrency_safe=False),
    )
    registry = ToolRegistry([spec])
    registry.freeze()
    view = registry.enabled_view()
    executor = RecordingExecutor(view, tmp_path, "session")
    return view.specs, executor, handler_calls


@pytest.mark.asyncio
async def test_full_tool_lifecycle_orders_commits_hooks_and_handler(tmp_path):
    timeline = []
    tools, executor, handler_calls = make_runtime(tmp_path, timeline)

    async def assistant_notification(context):
        timeline.append("assistant_hook")

    async def post_notification(context):
        timeline.append(f"post_hook:{context.result.tool_use_id}")

    async def observe_pre_tool(context):
        timeline.append(f"pre_hook:{context.tool_use.tool_use_id}")
        return ContinueToolUse()

    registry = HookRegistry()
    registry.register_assistant_message_completed(assistant_notification)
    registry.register_pre_tool_use(observe_pre_tool)
    registry.register_pre_tool_use(FastToolValidationHook())
    registry.register_post_tool_use(post_notification)
    dispatcher = HookDispatcher(registry.freeze())
    invalid = json.dumps({"correction_of_tool_use_id": None})
    valid = json.dumps({"value": "ok", "correction_of_tool_use_id": None})
    model = ScriptedModel([
        [
            ToolUseDelta(0, "bad", name_fragment="echo", arguments_fragment=invalid),
            ToolUseDelta(1, "good", name_fragment="echo", arguments_fragment=valid),
            ResponseCompleted("tool_calls"),
        ],
        [TextDelta("done"), ResponseCompleted("stop")],
    ])
    user = Message.text(Role.USER, "go")
    committer = Committer(user, timeline)

    result = await AgentLoop(
        model,
        PassthroughContextManager(),
        executor,
        tools,
        dispatcher=dispatcher,
    ).run((user,), user, "system", 2, committer, Cancellation(), uuid4())

    assert result.reason is StopReason.COMPLETED
    assert handler_calls == ["good"]
    assert executor.results[0].failure.stage == "fast_validation"
    assert executor.results[0].attempts == 0
    assert timeline[:10] == [
        "assistant_commit",
        "assistant_hook",
        "pre_hook:bad",
        "pre_hook:good",
        "handler:good",
        "result_commit:bad",
        "post_hook:bad",
        "result_commit:good",
        "post_hook:good",
        "assistant_commit",
    ]


@pytest.mark.asyncio
async def test_pre_tool_hook_failure_stops_before_executor_and_handler(tmp_path):
    timeline = []
    tools, executor, handler_calls = make_runtime(tmp_path, timeline)

    async def broken(context):
        raise RuntimeError("broken preflight")

    registry = HookRegistry()
    registry.register_pre_tool_use(broken)
    model = ScriptedModel([[
        ToolUseDelta(
            0,
            "call",
            name_fragment="echo",
            arguments_fragment=json.dumps({"value": "ok", "correction_of_tool_use_id": None}),
        ),
        ResponseCompleted("tool_calls"),
    ]])
    user = Message.text(Role.USER, "go")
    committer = Committer(user, timeline)

    result = await AgentLoop(
        model,
        PassthroughContextManager(),
        executor,
        tools,
        dispatcher=HookDispatcher(registry.freeze()),
    ).run((user,), user, "system", 1, committer, Cancellation(), uuid4())

    assert result.reason is StopReason.HOOK_FAILED
    assert executor.submit_calls == 0 and handler_calls == []


@pytest.mark.asyncio
async def test_notifications_only_run_after_their_fact_is_committed(tmp_path):
    assistant_notifications = 0
    post_notifications = 0

    async def assistant_hook(context):
        nonlocal assistant_notifications
        assistant_notifications += 1

    async def post_hook(context):
        nonlocal post_notifications
        post_notifications += 1

    registry = HookRegistry()
    registry.register_assistant_message_completed(assistant_hook)
    registry.register_pre_tool_use(FastToolValidationHook())
    registry.register_post_tool_use(post_hook)
    dispatcher = HookDispatcher(registry.freeze())

    user = Message.text(Role.USER, "go")
    assistant_failure = FailingCommitter(user, [], "assistant")
    result = await AgentLoop(
        ScriptedModel([[TextDelta("done"), ResponseCompleted("stop")]]),
        PassthroughContextManager(),
        dispatcher=dispatcher,
    ).run((user,), user, "system", 1, assistant_failure, Cancellation(), uuid4())
    assert result.reason is StopReason.EVENT_COMMIT_FAILED
    assert assistant_notifications == post_notifications == 0

    timeline = []
    tools, executor, _ = make_runtime(tmp_path, timeline)
    tool_failure = FailingCommitter(user, timeline, "tool")
    arguments = json.dumps({"value": "ok", "correction_of_tool_use_id": None})
    model = ScriptedModel([[
        ToolUseDelta(0, "call", name_fragment="echo", arguments_fragment=arguments),
        ResponseCompleted("tool_calls"),
    ]])
    result = await AgentLoop(
        model,
        PassthroughContextManager(),
        executor,
        tools,
        dispatcher=dispatcher,
    ).run((user,), user, "system", 1, tool_failure, Cancellation(), uuid4())
    assert result.reason is StopReason.EVENT_COMMIT_FAILED
    assert assistant_notifications == 1 and post_notifications == 0


@pytest.mark.asyncio
async def test_reused_tool_use_id_is_rejected_before_second_pre_tool_hook(tmp_path):
    timeline = []
    tools, executor, _ = make_runtime(tmp_path, timeline)
    hook_calls = 0

    async def observe(context):
        nonlocal hook_calls
        hook_calls += 1
        return ContinueToolUse()

    registry = HookRegistry()
    registry.register_pre_tool_use(observe)
    arguments = json.dumps({"value": "ok", "correction_of_tool_use_id": None})
    model = ScriptedModel([
        [
            ToolUseDelta(0, "same", name_fragment="echo", arguments_fragment=arguments),
            ResponseCompleted("tool_calls"),
        ],
        [
            ToolUseDelta(0, "same", name_fragment="echo", arguments_fragment=arguments),
            ResponseCompleted("tool_calls"),
        ],
    ])
    user = Message.text(Role.USER, "go")
    committer = Committer(user, timeline)

    with pytest.raises(ToolProtocolError, match="重复"):
        await AgentLoop(
            model,
            PassthroughContextManager(),
            executor,
            tools,
            dispatcher=HookDispatcher(registry.freeze()),
        ).run((user,), user, "system", 2, committer, Cancellation(), uuid4())

    assert hook_calls == 1


@pytest.mark.asyncio
async def test_notification_cancellation_keeps_the_accepted_assistant_identity():
    async def cancelled(context):
        raise asyncio.CancelledError

    registry = HookRegistry()
    registry.register_assistant_message_completed(cancelled)
    user = Message.text(Role.USER, "go")
    committer = Committer(user, [])
    result = await AgentLoop(
        ScriptedModel([[TextDelta("accepted"), ResponseCompleted("stop")]]),
        PassthroughContextManager(),
        dispatcher=HookDispatcher(registry.freeze()),
    ).run((user,), user, "system", 1, committer, Cancellation(), uuid4())

    assert result.reason is StopReason.CANCELLED
    assert result.final_message_id == committer.messages[-1].message_id
