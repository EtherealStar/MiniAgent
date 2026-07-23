import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from miniagent.context import (
    AgentRunEnvironment,
    CompressionResult,
    ContextBudgetError,
    ContextManager,
    PromptInputs,
    ToolView,
    WorkingContext,
)
from miniagent.domain import ContextSummary, Message, Role, ToolResultPart, ToolUsePart
from miniagent.domain import ToolSpec as ProviderToolSpec
from miniagent.tools import build_default_registry


class ContentAwareCounter:
    def count_input(self, context, tools, model_name, tokenizer_encoding="o200k_base"):
        system = context.messages[0].parts[0].content
        if "压缩结果" in system:
            return 35
        if len(context.messages) == 2:
            return 15
        return 75


class RecordingCompressor:
    def __init__(self, text="压缩结果"):
        self.text = text
        self.calls = []

    async def compress(self, source_groups, environment, max_output_tokens):
        self.calls.append((source_groups, max_output_tokens))
        return CompressionResult(self.text, "stop")


class TruncatedCompressor:
    async def compress(self, source_groups, environment, max_output_tokens):
        return CompressionResult("partial", "length")


class EmptyCompressor:
    async def compress(self, source_groups, environment, max_output_tokens):
        return CompressionResult("", "stop")


class FailingCompressor:
    async def compress(self, source_groups, environment, max_output_tokens):
        raise RuntimeError("provider failed")


class CancelledCompressor:
    async def compress(self, source_groups, environment, max_output_tokens):
        raise asyncio.CancelledError


class CommitPort:
    def __init__(self, fail=False):
        self.fail = fail
        self.summaries = []
        self.updates = []

    async def commit_context_summary(self, summary):
        if self.fail:
            raise RuntimeError("journal failed")
        self.summaries.append(summary)

    async def publish_live(self, update):
        self.updates.append(update)


def prompt_inputs():
    return PromptInputs(
        identity="identity",
        behavior_rules="behavior",
        risk_constraints="risk",
        validation_rules="validation",
        workspace_state="workspace",
        agents_md="agents",
        current_time=datetime(2026, 7, 23, 9, 30, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )


def environment(system_context, current_user, *, window=100):
    return AgentRunEnvironment(
        model_name="test-model",
        system_context=system_context,
        context_window=window,
        reserved_output_tokens=5,
        current_user_message_id=current_user.message_id,
        run_id=uuid4(),
    )


async def test_prompt_snapshot_summary_and_visible_tools_have_fixed_order():
    manager = ContextManager(token_counter=ContentAwareCounter())
    frozen = await manager.start_run(prompt_inputs())
    old = Message.text(Role.USER, "old")
    current = Message.text(Role.USER, "current")
    summaries = (
        ContextSummary(old.message_id, current.message_id, "summary one"),
        ContextSummary(current.message_id, None, "summary two"),
    )
    tools = ToolView.from_definitions((
        ("read", "read files", {"type": "function", "function": {"name": "read"}}, "read prompt"),
    ))

    context = await manager.before_model_call(
        WorkingContext(summaries=summaries, messages=(old, current)),
        environment(frozen, current, window=1000),
        tools,
        CommitPort(),
    )

    system = context.messages[0].parts[0].content
    ordered = ["identity", "behavior", "risk", "validation", "workspace", "agents", "summary one", "summary two", "read files", "read prompt"]
    assert [system.index(value) for value in ordered] == sorted(system.index(value) for value in ordered)
    assert "run_id" not in system and "Git" not in system and "operating system" not in system
    assert context.tool_schemas[0]["function"]["name"] == "read"


def test_tool_view_from_runtime_specs_keeps_index_prompt_and_schema_in_sync():
    registry = build_default_registry()
    runtime_view = ToolView.from_specs(registry.enabled_view().specs)
    provider_view = ToolView.from_specs((ProviderToolSpec(
        "read", {"type": "function", "function": {"name": "read"}}
    ),))

    assert runtime_view.definitions[0].name == "grep"
    assert runtime_view.definitions[0].description
    assert runtime_view.definitions[0].prompt == "[TODO: tool prompt]"
    assert runtime_view.function_schemas()[0]["function"]["name"] == "grep"
    assert provider_view.definitions[0].name == "read"
    assert provider_view.function_schemas()[0]["function"]["name"] == "read"


async def test_compression_keeps_tool_exchange_whole_and_excludes_current_user():
    compressor = RecordingCompressor()
    manager = ContextManager(token_counter=ContentAwareCounter(), compressor=compressor)
    frozen = await manager.start_run(prompt_inputs())
    old_user = Message.text(Role.USER, "old request")
    assistant = Message(role=Role.ASSISTANT, parts=(ToolUsePart("read", "{}", "call-1"),))
    result = Message(role=Role.TOOL, parts=(ToolResultPart("call-1", assistant.message_id, "result"),))
    current = Message.text(Role.USER, "current request")
    session = CommitPort()

    context = await manager.before_model_call(
        WorkingContext(messages=(old_user, assistant, result, current)),
        environment(frozen, current),
        ToolView(),
        session,
    )

    assert context.compression_applied is True
    assert len(compressor.calls) == 1
    source = tuple(message for group in compressor.calls[0][0] for message in group.messages)
    assert assistant in source and result in source
    assert current not in source
    assert session.summaries[0].covers_through_message_id == result.message_id
    assert context.estimated_total_tokens == 40


async def test_current_run_tool_exchange_can_be_compressed_without_covering_user_text():
    compressor = RecordingCompressor()
    manager = ContextManager(token_counter=ContentAwareCounter(), compressor=compressor)
    frozen = await manager.start_run(prompt_inputs())
    current = Message.text(Role.USER, "current request")
    assistant = Message(role=Role.ASSISTANT, parts=(ToolUsePart("read", "{}", "call-1"),))
    result = Message(role=Role.TOOL, parts=(ToolResultPart("call-1", assistant.message_id, "result"),))
    session = CommitPort()

    context = await manager.before_model_call(
        WorkingContext(messages=(current, assistant, result)),
        environment(frozen, current),
        ToolView(),
        session,
    )

    source = tuple(message for group in compressor.calls[0][0] for message in group.messages)
    assert source == (assistant, result)
    assert session.summaries[0].resume_from_message_id == current.message_id
    assert [message.message_id for message in context.messages[1:]] == [current.message_id]


async def test_compression_above_target_but_below_failure_line_continues_with_diagnostic():
    class DegradedCounter(ContentAwareCounter):
        def count_input(self, context, tools, model_name, tokenizer_encoding="o200k_base"):
            if "压缩结果" in context.messages[0].parts[0].content:
                return 55
            return super().count_input(context, tools, model_name)

    manager = ContextManager(token_counter=DegradedCounter(), compressor=RecordingCompressor())
    frozen = await manager.start_run(prompt_inputs())
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")

    context = await manager.before_model_call(
        WorkingContext(messages=(old, current)),
        environment(frozen, current),
        ToolView(),
        CommitPort(),
    )

    assert context.estimated_total_tokens == 60
    assert context.diagnostics == ("compression_target_unreachable",)


async def test_failed_summary_commit_never_returns_candidate_context():
    manager = ContextManager(token_counter=ContentAwareCounter(), compressor=RecordingCompressor())
    frozen = await manager.start_run(prompt_inputs())
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")

    with pytest.raises(ContextBudgetError, match="提交"):
        await manager.before_model_call(
            WorkingContext(messages=(old, current)),
            environment(frozen, current),
            ToolView(),
            CommitPort(fail=True),
        )


async def test_compression_length_finish_and_insufficient_reduction_are_not_committed():
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")
    working = WorkingContext(messages=(old, current))

    truncated_session = CommitPort()
    truncated = ContextManager(
        token_counter=ContentAwareCounter(),
        compressor=TruncatedCompressor(),
    )
    frozen = await truncated.start_run(prompt_inputs())
    with pytest.raises(ContextBudgetError, match="截断"):
        await truncated.before_model_call(
            working, environment(frozen, current), ToolView(), truncated_session
        )
    assert truncated_session.summaries == []

    class NeverReducedCounter(ContentAwareCounter):
        def count_input(self, context, tools, model_name, tokenizer_encoding="o200k_base"):
            if "压缩结果" in context.messages[0].parts[0].content:
                return 75
            if len(context.messages) == 2:
                return 15
            return 75

    insufficient_session = CommitPort()
    insufficient = ContextManager(
        token_counter=NeverReducedCounter(), compressor=RecordingCompressor()
    )
    frozen = await insufficient.start_run(prompt_inputs())
    with pytest.raises(ContextBudgetError, match="仍达到"):
        await insufficient.before_model_call(
            working, environment(frozen, current), ToolView(), insufficient_session
        )
    assert insufficient_session.summaries == []


@pytest.mark.parametrize("compressor", (EmptyCompressor(), FailingCompressor()))
async def test_empty_or_failed_compression_is_not_committed(compressor):
    manager = ContextManager(token_counter=ContentAwareCounter(), compressor=compressor)
    frozen = await manager.start_run(prompt_inputs())
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")
    session = CommitPort()
    with pytest.raises(ContextBudgetError):
        await manager.before_model_call(
            WorkingContext(messages=(old, current)),
            environment(frozen, current),
            ToolView(),
            session,
        )
    assert session.summaries == []


async def test_cancelled_compression_publishes_failure_and_propagates_cancellation():
    manager = ContextManager(
        token_counter=ContentAwareCounter(), compressor=CancelledCompressor()
    )
    frozen = await manager.start_run(prompt_inputs())
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")
    session = CommitPort()
    with pytest.raises(asyncio.CancelledError):
        await manager.before_model_call(
            WorkingContext(messages=(old, current)),
            environment(frozen, current),
            ToolView(),
            session,
        )
    assert session.summaries == []
    assert session.updates[-1].reason == "compression_cancelled"


def test_real_token_counter_includes_schema_and_reserved_output():
    from miniagent.context import TiktokenTokenCounter
    from miniagent.ports import ModelContext

    context = ModelContext((Message.text(Role.SYSTEM, "system"), Message.text(Role.USER, "hello")))
    counter = TiktokenTokenCounter()
    without_tools = counter.count_input(context, ToolView(), "gpt-4o")
    with_tools = counter.count_input(
        context,
        ToolView.from_definitions((("read", "read", {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}, None),)),
        "gpt-4o",
    )
    assert without_tools > 0
    assert with_tools > without_tools
    assert with_tools + 123 > with_tools


@pytest.mark.parametrize(("input_tokens", "expected_calls"), ((74, 0), (75, 1), (76, 1)))
async def test_compression_threshold_is_exactly_eighty_percent(input_tokens, expected_calls):
    class BoundaryCounter:
        def count_input(self, context, tools, model_name, tokenizer_encoding="o200k_base"):
            if "压缩结果" in context.messages[0].parts[0].content or len(context.messages) == 2:
                return 20
            return input_tokens

    compressor = RecordingCompressor()
    manager = ContextManager(token_counter=BoundaryCounter(), compressor=compressor)
    frozen = await manager.start_run(prompt_inputs())
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")
    await manager.before_model_call(
        WorkingContext(messages=(old, current)),
        environment(frozen, current),
        ToolView(),
        CommitPort(),
    )
    assert len(compressor.calls) == expected_calls


async def test_actual_provider_usage_calibrates_the_next_preflight():
    class ConstantCounter:
        def count_input(self, context, tools, model_name, tokenizer_encoding="o200k_base"):
            return 10

    manager = ContextManager(token_counter=ConstantCounter())
    frozen = await manager.start_run(prompt_inputs())
    current = Message.text(Role.USER, "current")
    env = environment(frozen, current, window=1000)
    first = await manager.before_model_call(
        WorkingContext(messages=(current,)), env, ToolView(), CommitPort()
    )
    manager.record_actual_prompt_tokens(env.run_id, first.estimated_input_tokens, 30)
    second = await manager.before_model_call(
        WorkingContext(messages=(current,)), env, ToolView(), CommitPort()
    )
    assert first.estimated_total_tokens == 15
    assert second.estimated_total_tokens == 35


async def test_explicit_hook_compression_runs_below_automatic_threshold():
    class BelowThresholdCounter:
        def count_input(self, context, tools, model_name, tokenizer_encoding="o200k_base"):
            return 10

    compressor = RecordingCompressor()
    manager = ContextManager(
        token_counter=BelowThresholdCounter(),
        compressor=compressor,
    )
    frozen = await manager.start_run(prompt_inputs())
    old, current = Message.text(Role.USER, "old"), Message.text(Role.USER, "current")
    session = CommitPort()

    initial = await manager.before_model_call(
        WorkingContext(messages=(old, current)),
        environment(frozen, current),
        ToolView(),
        session,
    )
    compressed = await manager.request_compression(
        WorkingContext(messages=(old, current)),
        environment(frozen, current),
        ToolView(),
        session,
    )

    assert initial.compression_applied is False
    assert compressed.compression_applied is True
    assert len(compressor.calls) == len(session.summaries) == 1
