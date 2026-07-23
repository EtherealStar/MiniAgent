import asyncio
from uuid import uuid4

import pytest

from miniagent.context import SystemContext
from miniagent.domain import Message, Role, StopReason
from miniagent.hooks import (
    AbortRun,
    ContinueModelCall,
    HookDispatcher,
    HookRegistry,
    RequestCompression,
)
from miniagent.loop import AgentLoop
from miniagent.ports import Cancellation, ModelContext
from miniagent.provider.events import ResponseCompleted, TextDelta
from miniagent.updates import AssistantMessageStarted


class FakeContextManager:
    def __init__(self):
        self.before_calls = 0
        self.compression_calls = 0

    async def start_run(self, prompt_inputs):
        return SystemContext("system")

    async def before_model_call(self, working, environment, tools, session, **kwargs):
        self.before_calls += 1
        return ModelContext((Message.text(Role.SYSTEM, "initial"), *working.messages))

    async def request_compression(self, working, environment, tools, session, **kwargs):
        self.compression_calls += 1
        return ModelContext((Message.text(Role.SYSTEM, "compressed"), *working.messages), compression_applied=True)

    def record_actual_prompt_tokens(self, *args):
        pass


class ScriptedModel:
    def __init__(self):
        self.contexts = []

    async def stream(self, context, tools, options, cancellation):
        self.contexts.append(context)
        yield TextDelta("done")
        yield ResponseCompleted("stop")


class Committer:
    def __init__(self, user, timeline):
        self.session_id = uuid4()
        self.context_summaries = ()
        self.messages = [user]
        self.timeline = timeline
        self.updates = []
        self.result = None

    async def publish_live(self, update):
        self.updates.append(update)
        if isinstance(update, AssistantMessageStarted):
            self.timeline.append("assistant_started")

    async def commit_assistant(self, run_id, message, finish_reason):
        self.messages.append(message)

    async def commit_tool_result(self, run_id, message):
        self.messages.append(message)

    async def commit_context_summary(self, run_id, summary):
        self.context_summaries += (summary,)

    async def finish_run(self, run_id, result):
        self.result = result


async def run_with_hooks(*hooks, cancellation=None):
    timeline = []
    registry = HookRegistry()
    for hook in hooks:
        registry.register_pre_model_call(hook)
    user = Message.text(Role.USER, "go")
    committer = Committer(user, timeline)
    model = ScriptedModel()
    manager = FakeContextManager()
    result = await AgentLoop(
        model,
        manager,
        dispatcher=HookDispatcher(registry.freeze()),
    ).run(
        (user,),
        user,
        "system",
        2,
        committer,
        cancellation or Cancellation(),
        uuid4(),
    )
    return result, model, manager, committer, timeline


@pytest.mark.asyncio
async def test_continue_runs_before_started_and_real_model_call():
    seen = []

    async def observe(context):
        seen.append((context.turn_number, context.model_context.messages[0].parts[0].content))
        return ContinueModelCall()

    result, model, manager, committer, timeline = await run_with_hooks(observe)

    assert result.reason is StopReason.COMPLETED and result.turn_count == 1
    assert seen == [(1, "initial")]
    assert len(model.contexts) == manager.before_calls == 1
    assert timeline == ["assistant_started"]


@pytest.mark.asyncio
async def test_abort_and_hook_failure_do_not_start_model_or_turn():
    async def abort(context):
        return AbortRun("policy_denied", "blocked")

    aborted, model, _, committer, _ = await run_with_hooks(abort)
    assert aborted.reason is StopReason.HOOK_ABORTED and aborted.turn_count == 0
    assert aborted.error.category == "policy_denied"
    assert model.contexts == []
    assert not any(isinstance(update, AssistantMessageStarted) for update in committer.updates)

    async def broken(context):
        raise RuntimeError("boom")

    failed, model, _, _, _ = await run_with_hooks(broken)
    assert failed.reason is StopReason.HOOK_FAILED and failed.turn_count == 0
    assert failed.error.category == "hook_execution" and model.contexts == []


@pytest.mark.asyncio
async def test_one_requested_compression_rechecks_hooks_with_new_context():
    observed = []

    async def compress_once(context):
        system = context.model_context.messages[0].parts[0].content
        observed.append(system)
        return RequestCompression() if len(observed) == 1 else ContinueModelCall()

    result, model, manager, _, _ = await run_with_hooks(compress_once)

    assert result.reason is StopReason.COMPLETED and result.turn_count == 1
    assert observed == ["initial", "compressed"]
    assert manager.compression_calls == 1
    assert model.contexts[0].messages[0].parts[0].content == "compressed"


@pytest.mark.asyncio
async def test_repeated_compression_request_is_explicit_hook_failure():
    async def always_compress(context):
        return RequestCompression()

    result, model, manager, _, _ = await run_with_hooks(always_compress)

    assert result.reason is StopReason.HOOK_FAILED and result.turn_count == 0
    assert result.error.category == "hook_compression_loop"
    assert manager.compression_calls == 1 and model.contexts == []


@pytest.mark.asyncio
async def test_hook_cancellation_preserves_cancelled_semantics():
    async def cancelled(context):
        raise asyncio.CancelledError

    result, model, _, _, _ = await run_with_hooks(cancelled)
    assert result.reason is StopReason.CANCELLED and result.turn_count == 0
    assert model.contexts == []
