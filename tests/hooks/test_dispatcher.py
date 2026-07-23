import asyncio
from uuid import uuid4

import pytest

from miniagent.domain import Message, Role, ToolUsePart
from miniagent.hooks import (
    AbortRun,
    AssistantMessageCompletedContext,
    ContinueModelCall,
    ContinueToolUse,
    HookDispatcher,
    HookExecutionError,
    HookRegistry,
    PreToolUseContext,
    RejectToolUse,
)


class Trace:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_control_hooks_short_circuit_in_registration_order():
    calls = []

    async def first(context):
        calls.append("first")
        return ContinueToolUse()

    async def reject(context):
        calls.append("reject")
        return RejectToolUse("blocked", "denied")

    async def skipped(context):
        calls.append("skipped")
        return ContinueToolUse()

    registry = HookRegistry()
    for hook in (first, reject, skipped):
        registry.register_pre_tool_use(hook)
    dispatcher = HookDispatcher(registry.freeze())
    context = PreToolUseContext(uuid4(), uuid4(), ToolUsePart("tool", "{}"), None)

    result = await dispatcher.before_tool_use(context)

    assert isinstance(result, RejectToolUse)
    assert calls == ["first", "reject"]


@pytest.mark.asyncio
async def test_control_hook_invalid_result_is_explicit_failure():
    async def invalid(context):
        return None

    registry = HookRegistry()
    registry.register_pre_model_call(invalid)
    dispatcher = HookDispatcher(registry.freeze())

    with pytest.raises(HookExecutionError) as caught:
        await dispatcher.before_model_call(object())
    assert caught.value.phase == "pre_model_call"
    assert caught.value.index == 0


@pytest.mark.asyncio
async def test_notification_failure_is_traced_and_later_hook_runs():
    calls = []

    async def broken(context):
        calls.append("broken")
        raise ValueError("sensitive body")

    async def later(context):
        calls.append("later")

    registry = HookRegistry()
    registry.register_assistant_message_completed(broken)
    registry.register_assistant_message_completed(later)
    trace = Trace()
    dispatcher = HookDispatcher(registry.freeze(), trace)
    context = AssistantMessageCompletedContext(uuid4(), Message.text(Role.ASSISTANT, "secret"), "stop")

    await dispatcher.assistant_message_completed(context)

    assert calls == ["broken", "later"]
    assert trace.events[0]["event"] == "hook_notification_failed"
    assert trace.events[0]["exception_type"] == "ValueError"
    assert "sensitive body" not in str(trace.events[0])


@pytest.mark.asyncio
async def test_cancelled_error_is_not_wrapped():
    async def cancelled(context):
        raise asyncio.CancelledError

    registry = HookRegistry()
    registry.register_pre_tool_use(cancelled)
    dispatcher = HookDispatcher(registry.freeze())

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.before_tool_use(object())


@pytest.mark.asyncio
async def test_notification_trace_failure_does_not_change_committed_flow():
    calls = []

    async def broken(context):
        calls.append("broken")
        raise ValueError("notification failed")

    async def later(context):
        calls.append("later")

    class BrokenTrace:
        async def emit(self, event):
            raise OSError("trace unavailable")

    registry = HookRegistry()
    registry.register_assistant_message_completed(broken)
    registry.register_assistant_message_completed(later)
    dispatcher = HookDispatcher(registry.freeze(), BrokenTrace())
    context = AssistantMessageCompletedContext(
        uuid4(), Message.text(Role.ASSISTANT, "accepted"), "stop"
    )

    await dispatcher.assistant_message_completed(context)

    assert calls == ["broken", "later"]
