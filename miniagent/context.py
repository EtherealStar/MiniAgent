from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

import tiktoken

from .domain import (
    ContextSummary,
    Message,
    ReasoningPart,
    Role,
    TextPart,
    ToolResultPart,
    ToolUsePart,
)
from .ports import ModelContext
from .updates import CompressionCompleted, CompressionFailed, CompressionStarted


CONTEXT_COMPRESSION_PROMPT_PLACEHOLDER = "[TODO: context compression prompt]"
TOOL_PROMPT_PLACEHOLDER = "[TODO: tool prompt]"


class ContextContractError(ValueError):
    pass


class ContextBudgetError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        measured_token_count: int | None = None,
        protected_token_count: int | None = None,
    ) -> None:
        super().__init__(message)
        self.measured_token_count = measured_token_count
        self.protected_token_count = protected_token_count


class CompressionFailureReason(StrEnum):
    NO_COMPRESSIBLE_GROUP = "no_compressible_message_group"
    PROTECTED_CONTEXT_TOO_LARGE = "protected_context_too_large"
    COMPRESSOR_ERROR = "compressor_error"
    CANCELLED = "compression_cancelled"
    OUTPUT_TRUNCATED = "compression_output_truncated"
    EMPTY_SUMMARY = "empty_summary"
    INSUFFICIENT = "compression_insufficient"
    SUMMARY_COMMIT_FAILED = "summary_commit_failed"


class ContextDiagnostic(StrEnum):
    COMPRESSION_TARGET_UNREACHABLE = "compression_target_unreachable"


@dataclass(frozen=True, slots=True)
class WorkingContext:
    messages: tuple[Message, ...]
    summaries: tuple[ContextSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptInputs:
    identity: str = ""
    behavior_rules: str = ""
    risk_constraints: str = ""
    validation_rules: str = ""
    workspace_state: str = ""
    agents_md: str = ""
    current_time: datetime | None = None
    timezone_name: str = ""
    supporting_materials: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.current_time is not None and self.current_time.utcoffset() is None:
            raise ContextContractError("current_time 必须包含时区")


@dataclass(frozen=True, slots=True)
class SystemContext:
    text: str


@dataclass(frozen=True, slots=True)
class AgentRunEnvironment:
    model_name: str
    system_context: SystemContext
    context_window: int
    reserved_output_tokens: int
    current_user_message_id: UUID
    run_id: UUID
    tokenizer_encoding: str = "o200k_base"
    provider: object | None = None
    cancellation: object | None = None
    trace_recorder: object | None = None
    trace_context: object | None = None

    def __post_init__(self) -> None:
        if not self.model_name:
            raise ContextContractError("model_name 不能为空")
        if self.context_window <= 0:
            raise ContextContractError("context_window 必须为正整数")
        if self.reserved_output_tokens < 0:
            raise ContextContractError("reserved_output_tokens 不能为负数")
        if self.reserved_output_tokens >= self.context_window:
            raise ContextContractError("reserved_output_tokens 必须小于 context_window")
        if not self.tokenizer_encoding:
            raise ContextContractError("tokenizer_encoding 不能为空")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    def freeze(item: object) -> object:
        if isinstance(item, Mapping):
            return MappingProxyType({str(key): freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        if isinstance(item, tuple):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(deepcopy(dict(value)))  # type: ignore[return-value]


def _plain_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_value(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    function_schema: Mapping[str, object]
    prompt: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ContextContractError("工具名称不能为空")
        object.__setattr__(self, "function_schema", _freeze_mapping(self.function_schema))
        function = self.function_schema.get("function")
        if isinstance(function, Mapping):
            schema_name = function.get("name")
            if schema_name is not None and schema_name != self.name:
                raise ContextContractError("ToolView 名称必须与 function schema 一致")


@dataclass(frozen=True, slots=True)
class ToolView:
    definitions: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        names = [definition.name for definition in self.definitions]
        if len(names) != len(set(names)):
            raise ContextContractError("ToolView 中的工具名称不得重复")

    @classmethod
    def from_definitions(
        cls,
        definitions: Sequence[tuple[str, str, Mapping[str, object], str | None]],
    ) -> ToolView:
        return cls(tuple(ToolDefinition(*definition) for definition in definitions))

    @classmethod
    def from_specs(cls, specs: Sequence[object]) -> ToolView:
        definitions: list[ToolDefinition] = []
        for spec in specs:
            schema = getattr(spec, "function_schema", None) or {}
            definitions.append(ToolDefinition(
                name=str(getattr(spec, "name")),
                description=str(getattr(spec, "description", "")),
                function_schema=schema,
                # 具体工具 prompt 尚未实现；先保证三联结构与可见工具集合一致。
                prompt=TOOL_PROMPT_PLACEHOLDER,
            ))
        return cls(tuple(definitions))

    def function_schemas(self) -> tuple[Mapping[str, object], ...]:
        return tuple(definition.function_schema for definition in self.definitions)


@dataclass(frozen=True, slots=True)
class MessageGroup:
    messages: tuple[Message, ...]

    def __post_init__(self) -> None:
        if not self.messages:
            raise ContextContractError("MessageGroup 不能为空")


@dataclass(frozen=True, slots=True)
class CompressionResult:
    text: str
    finish_reason: str | None = None


class TokenCounter(Protocol):
    def count_input(
        self,
        context: ModelContext,
        tools: ToolView,
        model_name: str,
        tokenizer_encoding: str = "o200k_base",
    ) -> int: ...


class ContextCompressor(Protocol):
    async def compress(
        self,
        source_groups: tuple[MessageGroup, ...],
        environment: AgentRunEnvironment,
        max_output_tokens: int,
    ) -> CompressionResult | str: ...


class ContextCommitPort(Protocol):
    async def commit_context_summary(self, summary: ContextSummary) -> None: ...
    async def publish_live(self, update: object) -> None: ...


class TiktokenTokenCounter:
    """按 Chat Completions 封装近似计算完整输入，schema 使用规范 JSON。"""

    def __init__(self, fallback_encoding: str = "o200k_base") -> None:
        self._fallback_encoding = fallback_encoding

    def count_input(
        self,
        context: ModelContext,
        tools: ToolView,
        model_name: str,
        tokenizer_encoding: str = "o200k_base",
    ) -> int:
        try:
            encoding = tiktoken.encoding_for_model(model_name)
        except KeyError:
            try:
                encoding = tiktoken.get_encoding(tokenizer_encoding or self._fallback_encoding)
            except ValueError as exc:
                raise ContextContractError("无法加载 tokenizer encoding") from exc

        total = 3
        for message in context.messages:
            total += 3 + len(encoding.encode(message.role.value))
            for part in message.parts:
                if isinstance(part, (TextPart, ReasoningPart)):
                    total += len(encoding.encode(part.content))
                elif isinstance(part, ToolUsePart):
                    total += len(encoding.encode(part.name))
                    total += len(encoding.encode(part.arguments))
                    total += len(encoding.encode(part.tool_use_id))
                elif isinstance(part, ToolResultPart):
                    total += len(encoding.encode(part.tool_use_id))
                    total += len(encoding.encode(part.content))
        for schema in tools.function_schemas():
            canonical = json.dumps(
                _plain_value(schema), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            total += 4 + len(encoding.encode(canonical))
        return total


class ContextManager:
    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
        compressor: ContextCompressor | None = None,
    ) -> None:
        self._token_counter = token_counter or TiktokenTokenCounter()
        self._compressor = compressor
        self._usage_adjustments: dict[UUID, int] = {}

    async def start_run(self, prompt_inputs: PromptInputs) -> SystemContext:
        sections = (
            ("Identity", prompt_inputs.identity),
            ("Behavior Rules", prompt_inputs.behavior_rules),
            ("Risk and Constraints", prompt_inputs.risk_constraints),
            ("Validation and Reporting", prompt_inputs.validation_rules),
            ("Workspace State", prompt_inputs.workspace_state),
            ("AGENTS.md", prompt_inputs.agents_md),
            ("Current Time", self._format_time(prompt_inputs)),
            ("Supporting Materials", "\n".join(prompt_inputs.supporting_materials)),
        )
        return SystemContext(self._join_sections(sections))

    async def before_model_call(
        self,
        working: WorkingContext,
        environment: AgentRunEnvironment,
        tools: ToolView,
        session: ContextCommitPort,
        *,
        trigger_model_call_id: UUID | None = None,
    ) -> ModelContext:
        self._validate_working(working, environment.current_user_message_id)
        initial = self._assemble(working, environment, tools)
        initial = self._measure(initial, environment, tools)
        if not self._at_or_above(initial.estimated_total_tokens, environment.context_window, 80):
            return initial

        groups = self._compressible_groups(working, environment.current_user_message_id)
        compressor = self._compressor
        if compressor is None and environment.provider is not None:
            compressor = ModelContextCompressor(environment.provider)
        if not groups or compressor is None:
            await self._fail(
                session,
                CompressionFailureReason.NO_COMPRESSIBLE_GROUP,
                initial.estimated_total_tokens,
                initial.estimated_total_tokens,
            )
            raise ContextBudgetError(
                "上下文达到预算上限，但不存在可用的完整压缩消息组",
                measured_token_count=initial.estimated_total_tokens,
                protected_token_count=initial.estimated_total_tokens,
            )

        selected = self._select_groups(working, groups, environment, tools)
        boundary = selected[-1].messages[-1]
        protected = self._context_after_boundary(
            working, environment, tools, boundary.message_id, candidate_summary=None
        )
        protected = self._measure(protected, environment, tools)
        if self._at_or_above(protected.estimated_total_tokens, environment.context_window, 80):
            await self._fail(
                session,
                CompressionFailureReason.PROTECTED_CONTEXT_TOO_LARGE,
                initial.estimated_total_tokens,
                protected.estimated_total_tokens,
            )
            raise ContextBudgetError(
                "受保护上下文本身已达到预算上限",
                measured_token_count=initial.estimated_total_tokens,
                protected_token_count=protected.estimated_total_tokens,
            )

        await session.publish_live(CompressionStarted(
            trigger_model_call_id=trigger_model_call_id or uuid4(),
            source_boundary_message_id=boundary.message_id,
        ))
        target_tokens = environment.context_window // 2
        max_output_tokens = max(
            1,
            min(environment.reserved_output_tokens or 1, target_tokens - protected.estimated_total_tokens),
        )
        try:
            raw_result = await compressor.compress(selected, environment, max_output_tokens)
        except asyncio.CancelledError:
            await self._fail(session, CompressionFailureReason.CANCELLED, initial.estimated_total_tokens, protected.estimated_total_tokens)
            raise
        except Exception as exc:
            await self._fail(session, CompressionFailureReason.COMPRESSOR_ERROR, initial.estimated_total_tokens, protected.estimated_total_tokens)
            raise ContextBudgetError("上下文压缩模型调用失败") from exc
        result = raw_result if isinstance(raw_result, CompressionResult) else CompressionResult(raw_result)
        if not result.text.strip() or result.finish_reason == "length":
            reason = (
                CompressionFailureReason.OUTPUT_TRUNCATED
                if result.finish_reason == "length"
                else CompressionFailureReason.EMPTY_SUMMARY
            )
            await self._fail(session, reason, initial.estimated_total_tokens, protected.estimated_total_tokens)
            raise ContextBudgetError("上下文压缩结果为空或被长度截断")

        summary = ContextSummary(
            covers_through_message_id=boundary.message_id,
            resume_from_message_id=self._resume_from(
                working.messages,
                boundary.message_id,
                environment.current_user_message_id,
            ),
            summary=result.text.strip(),
        )
        candidate = self._context_after_boundary(
            working, environment, tools, boundary.message_id, candidate_summary=summary
        )
        candidate = self._measure(candidate, environment, tools)
        if self._at_or_above(candidate.estimated_total_tokens, environment.context_window, 80):
            await self._fail(session, CompressionFailureReason.INSUFFICIENT, candidate.estimated_total_tokens, protected.estimated_total_tokens)
            raise ContextBudgetError(
                "上下文压缩后仍达到预算上限",
                measured_token_count=candidate.estimated_total_tokens,
                protected_token_count=protected.estimated_total_tokens,
            )

        try:
            await session.commit_context_summary(summary)
        except Exception as exc:
            await self._fail(session, CompressionFailureReason.SUMMARY_COMMIT_FAILED, candidate.estimated_total_tokens, protected.estimated_total_tokens)
            raise ContextBudgetError("ContextSummary 提交失败") from exc

        diagnostics = ()
        if candidate.estimated_total_tokens > target_tokens:
            diagnostics = (ContextDiagnostic.COMPRESSION_TARGET_UNREACHABLE,)
        completed = replace(candidate, compression_applied=True, diagnostics=diagnostics)
        await session.publish_live(CompressionCompleted(
            summary_id=summary.summary_id,
            covers_through_message_id=summary.covers_through_message_id,
            resume_from_message_id=summary.resume_from_message_id,
            source_token_count=initial.estimated_input_tokens,
            summary_token_count=self._summary_token_count(result.text, environment),
            target_unreachable=bool(diagnostics),
        ))
        return completed

    def record_actual_prompt_tokens(
        self,
        run_id: UUID,
        estimated_input_tokens: int,
        actual_prompt_tokens: int,
    ) -> None:
        if actual_prompt_tokens < 0:
            raise ContextContractError("actual_prompt_tokens 不能为负数")
        self._usage_adjustments[run_id] = actual_prompt_tokens - estimated_input_tokens

    def _assemble(
        self,
        working: WorkingContext,
        environment: AgentRunEnvironment,
        tools: ToolView,
    ) -> ModelContext:
        system = self._dynamic_system(environment.system_context, working.summaries, tools)
        raw = self._messages_after_last_summary(working, environment.current_user_message_id)
        messages = (Message.text(Role.SYSTEM, system), *self._project_messages(raw))
        return ModelContext(messages=messages, tool_schemas=tools.function_schemas())

    def _context_after_boundary(
        self,
        working: WorkingContext,
        environment: AgentRunEnvironment,
        tools: ToolView,
        boundary_id: UUID,
        *,
        candidate_summary: ContextSummary | None,
    ) -> ModelContext:
        summaries = working.summaries + (() if candidate_summary is None else (candidate_summary,))
        boundary_index = self._index_of(working.messages, boundary_id)
        current_index = self._index_of(working.messages, environment.current_user_message_id)
        retained = list(working.messages[boundary_index + 1 :])
        if current_index <= boundary_index:
            retained.insert(0, working.messages[current_index])
        system = self._dynamic_system(environment.system_context, summaries, tools)
        return ModelContext(
            messages=(Message.text(Role.SYSTEM, system), *self._project_messages(tuple(retained))),
            tool_schemas=tools.function_schemas(),
        )

    def _select_groups(
        self,
        working: WorkingContext,
        groups: tuple[MessageGroup, ...],
        environment: AgentRunEnvironment,
        tools: ToolView,
    ) -> tuple[MessageGroup, ...]:
        selected: list[MessageGroup] = []
        for group in groups:
            selected.append(group)
            boundary = group.messages[-1].message_id
            retained = self._context_after_boundary(
                working, environment, tools, boundary, candidate_summary=None
            )
            measured = self._measure(retained, environment, tools)
            if measured.estimated_total_tokens <= environment.context_window // 2:
                break
        return tuple(selected)

    def _measure(
        self,
        context: ModelContext,
        environment: AgentRunEnvironment,
        tools: ToolView,
    ) -> ModelContext:
        estimated = self._token_counter.count_input(
            context,
            tools,
            environment.model_name,
            environment.tokenizer_encoding,
        )
        estimated = max(0, estimated + self._usage_adjustments.get(environment.run_id, 0))
        return replace(
            context,
            estimated_input_tokens=estimated,
            estimated_total_tokens=estimated + environment.reserved_output_tokens,
        )

    def _summary_token_count(self, text: str, environment: AgentRunEnvironment) -> int:
        context = ModelContext((Message.text(Role.SYSTEM, text),))
        return self._token_counter.count_input(
            context,
            ToolView(),
            environment.model_name,
            environment.tokenizer_encoding,
        )

    @staticmethod
    def _dynamic_system(
        base: SystemContext,
        summaries: tuple[ContextSummary, ...],
        tools: ToolView,
    ) -> str:
        sections: list[tuple[str, str]] = [("System Context", base.text)]
        sections.extend(("Context Summary", summary.summary) for summary in summaries)
        if tools.definitions:
            index = "\n".join(
                f"{definition.name}: {definition.description}" for definition in tools.definitions
            )
            sections.append(("Available Tools", index))
            prompts = "\n".join(
                f"{definition.name}: {definition.prompt}"
                for definition in tools.definitions
                if definition.prompt
            )
            if prompts:
                sections.append(("Tool Prompt", prompts))
        return ContextManager._join_sections(sections)

    @staticmethod
    def _join_sections(sections: Sequence[tuple[str, str]]) -> str:
        return "\n\n".join(f"[{title}]\n{content}" for title, content in sections if content)

    @staticmethod
    def _format_time(inputs: PromptInputs) -> str:
        if inputs.current_time is None:
            return ""
        suffix = f" ({inputs.timezone_name})" if inputs.timezone_name else ""
        return inputs.current_time.isoformat() + suffix

    @staticmethod
    def _project_messages(messages: tuple[Message, ...]) -> tuple[Message, ...]:
        projected: list[Message] = []
        for message in messages:
            parts = tuple(part for part in message.parts if not isinstance(part, ReasoningPart))
            if not parts:
                continue
            projected.append(Message(
                role=message.role,
                parts=parts,
                message_id=message.message_id,
                continuation_of_message_id=message.continuation_of_message_id,
                retry_of_message_id=message.retry_of_message_id,
            ))
        return tuple(projected)

    @staticmethod
    def _validate_working(working: WorkingContext, current_user_id: UUID) -> None:
        positions = {message.message_id: index for index, message in enumerate(working.messages)}
        if current_user_id not in positions:
            raise ContextContractError("当前用户消息不在 WorkingContext 中")
        if working.messages[positions[current_user_id]].role is not Role.USER:
            raise ContextContractError("current_user_message_id 必须引用 user 消息")
        previous = -1
        for summary in working.summaries:
            boundary = positions.get(summary.covers_through_message_id)
            if boundary is None or boundary <= previous:
                raise ContextContractError("ContextSummary 覆盖边界必须存在且严格递增")
            previous = boundary

    @staticmethod
    def _messages_after_last_summary(
        working: WorkingContext,
        current_user_id: UUID,
    ) -> tuple[Message, ...]:
        if not working.summaries:
            return working.messages
        summary = working.summaries[-1]
        boundary = ContextManager._index_of(working.messages, summary.covers_through_message_id)
        current = ContextManager._index_of(working.messages, current_user_id)
        if summary.resume_from_message_id is None:
            retained = list(working.messages[boundary + 1 :])
        else:
            resume = ContextManager._index_of(working.messages, summary.resume_from_message_id)
            if resume <= boundary:
                retained = [working.messages[resume], *working.messages[boundary + 1 :]]
            else:
                retained = list(working.messages[resume:])
        if current <= boundary:
            if all(message.message_id != current_user_id for message in retained):
                retained.insert(0, working.messages[current])
        return tuple(retained)

    @staticmethod
    def _compressible_groups(
        working: WorkingContext,
        current_user_id: UUID,
    ) -> tuple[MessageGroup, ...]:
        groups: list[MessageGroup] = []
        current_group: list[Message] = []
        visible = ContextManager._messages_after_last_summary(working, current_user_id)
        for message in visible:
            if message.message_id == current_user_id:
                if current_group:
                    groups.append(MessageGroup(tuple(current_group)))
                    current_group = []
                continue
            if message.role is Role.USER and current_group:
                groups.append(MessageGroup(tuple(current_group)))
                current_group = []
            current_group.append(message)
        if current_group:
            groups.append(MessageGroup(tuple(current_group)))
        return tuple(groups)

    @staticmethod
    def _message_after(messages: tuple[Message, ...], boundary_id: UUID) -> UUID | None:
        index = ContextManager._index_of(messages, boundary_id)
        return messages[index + 1].message_id if index + 1 < len(messages) else None

    @staticmethod
    def _resume_from(
        messages: tuple[Message, ...],
        boundary_id: UUID,
        current_user_id: UUID,
    ) -> UUID | None:
        boundary = ContextManager._index_of(messages, boundary_id)
        current = ContextManager._index_of(messages, current_user_id)
        if current <= boundary:
            return current_user_id
        return ContextManager._message_after(messages, boundary_id)

    @staticmethod
    def _index_of(messages: tuple[Message, ...], message_id: UUID) -> int:
        for index, message in enumerate(messages):
            if message.message_id == message_id:
                return index
        raise ContextContractError("消息边界不在 WorkingContext 中")

    @staticmethod
    def _at_or_above(tokens: int, window: int, percent: int) -> bool:
        return tokens * 100 >= window * percent

    @staticmethod
    async def _fail(
        session: ContextCommitPort,
        reason: str,
        measured: int,
        protected: int,
    ) -> None:
        await session.publish_live(CompressionFailed(reason, measured, protected))


# 兼容现有 composition root；新代码只使用 ContextManager 契约。
ContextBuilder = ContextManager


class ModelContextCompressor:
    """通过同一 Provider 发起独立调用；具体压缩提示词暂时只保留占位。"""

    def __init__(self, provider: object) -> None:
        self._provider = provider

    async def compress(
        self,
        source_groups: tuple[MessageGroup, ...],
        environment: AgentRunEnvironment,
        max_output_tokens: int,
    ) -> CompressionResult:
        # 延迟导入避免上下文领域模型与 Provider 事件模块形成初始化环。
        from .ports import Cancellation, GenerationOptions
        from .provider.events import ReasoningDelta, ResponseCompleted, ResponseFailed, TextDelta

        source = tuple(message for group in source_groups for message in group.messages)
        messages = (
            Message.text(Role.SYSTEM, CONTEXT_COMPRESSION_PROMPT_PLACEHOLDER),
            *ContextManager._project_messages(source),
        )
        cancellation = environment.cancellation
        if not isinstance(cancellation, Cancellation):
            cancellation = Cancellation()
        chunks: list[str] = []
        terminal: ResponseCompleted | ResponseFailed | None = None
        span = None
        if environment.trace_recorder is not None and environment.trace_context is not None:
            span = await environment.trace_recorder.start_span(
                "model.call",
                environment.trace_context.child(),
                provider=getattr(self._provider, "provider_name", type(self._provider).__name__),
                model=environment.model_name,
                operation="context_compression",
                max_tokens=max_output_tokens,
            )
        stream = getattr(self._provider, "stream", None)
        if stream is None:
            raise ContextContractError("压缩 Provider 不支持 stream")
        try:
            async for event in stream(
                ModelContext(messages),
                (),
                GenerationOptions(max_tokens=max_output_tokens),
                cancellation,
            ):
                if terminal is not None:
                    raise ContextContractError("压缩模型终态之后出现额外事件")
                if isinstance(event, TextDelta):
                    chunks.append(event.content)
                elif isinstance(event, ReasoningDelta):
                    continue
                elif isinstance(event, (ResponseCompleted, ResponseFailed)):
                    terminal = event
                else:
                    raise ContextContractError("压缩模型不得调用工具")
        except BaseException:
            if span is not None:
                from .trace import TraceStatus
                await span.finish(TraceStatus.ERROR, operation="context_compression")
            raise
        if terminal is None:
            if span is not None:
                from .trace import TraceStatus
                await span.finish(
                    TraceStatus.ERROR,
                    operation="context_compression",
                    error_category="missing_terminal",
                )
            raise ContextContractError("压缩模型流缺少终态")
        if isinstance(terminal, ResponseFailed):
            if span is not None:
                from .trace import TraceStatus
                await span.finish(
                    TraceStatus.ERROR,
                    operation="context_compression",
                    error_category=terminal.category,
                    request_id=terminal.request_id,
                )
            raise ContextBudgetError(f"压缩 Provider 失败: {terminal.category}")
        if span is not None:
            from .trace import TraceStatus
            await span.finish(
                TraceStatus.OK,
                operation="context_compression",
                finish_reason=terminal.finish_reason,
                usage=None if terminal.usage is None else {
                    "prompt_tokens": terminal.usage.prompt_tokens,
                    "completion_tokens": terminal.usage.completion_tokens,
                    "total_tokens": terminal.usage.total_tokens,
                },
                request_id=terminal.request_id,
            )
        return CompressionResult("".join(chunks), terminal.finish_reason)
