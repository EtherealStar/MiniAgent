from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from .context import (
    AgentRunEnvironment,
    ContextBudgetError,
    ContextManager,
    PromptInputs,
    ToolView,
    WorkingContext,
)
from .hooks import HookDispatcher, HookExecutionError, HookRegistry
from .hooks.models import (
    AbortRun,
    AssistantMessageCompletedContext,
    ContinueModelCall,
    ContinueToolUse,
    PostToolUseContext,
    PreModelCallContext,
    PreToolUseContext,
    RejectToolUse,
    RequestCompression,
)
from .domain import (
    AgentRunResult,
    ErrorInfo,
    Message,
    ReasoningPart,
    Role,
    StopReason,
    TextPart,
    ToolExecutionBatch,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
)
from dataclasses import asdict
from .updates import (
    AssistantMessageDiscarded,
    AssistantMessageStarted,
    AssistantPartDelta,
    ToolUseDetected,
)
from .ports import Cancellation, GenerationOptions, ModelAdapter, RunCommitter, ToolExecutor
from .provider.errors import ProviderNotConfiguredError
from .provider.events import ReasoningDelta, ResponseCompleted, ResponseFailed, TextDelta, ToolUseDelta
from .session import EventCommitError
from .text_processing import PassthroughTextProcessor
from .tools.models import FieldError, PreToolUseOutcome
from .trace import NullTraceSink, TraceContext, TraceEventType, TraceRecorder, TraceStatus, sanitize_error


@dataclass(slots=True)
class _ToolDraft:
    tool_use_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class _BoundContextCommitPort:
    committer: RunCommitter
    run_id: UUID

    async def commit_context_summary(self, summary) -> None:
        await self.committer.commit_context_summary(self.run_id, summary)

    async def publish_live(self, update: object) -> None:
        await self.committer.publish_live(update)


class AgentLoop:
    def __init__(
        self,
        model: ModelAdapter,
        context_builder: ContextManager,
        tool_executor: ToolExecutor | None = None,
        tools: tuple[ToolSpec, ...] = (),
        options: GenerationOptions | None = None,
        context_budget: int = 16_000,
        text_processor_factory=PassthroughTextProcessor,
        trace_sink=None,
        dispatcher: HookDispatcher | None = None,
    ) -> None:
        self._model = model
        self._context_manager = context_builder
        self._tool_executor = tool_executor
        self._tools = tools
        self._options = options or GenerationOptions()
        self._context_budget = context_budget
        self._text_processor_factory = text_processor_factory
        self._trace_sink = trace_sink or NullTraceSink()
        self._provider_name = getattr(model, "provider_name", type(model).__name__)
        self._model_id = getattr(model, "model_id", None)
        self._dispatcher = dispatcher or HookDispatcher(HookRegistry().freeze())

    async def run(
        self,
        initial_messages: tuple[Message, ...],
        user_message: Message,
        system_prompt: str | PromptInputs,
        max_turns: int,
        committer: RunCommitter,
        cancellation: Cancellation,
        run_id: UUID | None = None,
    ) -> AgentRunResult:
        if max_turns <= 0:
            raise ValueError("max_turns 必须为正整数")
        if user_message.role is not Role.USER:
            raise ValueError("user_message 必须是 user 角色")
        if not any(message.message_id == user_message.message_id for message in initial_messages):
            raise ValueError("AgentLoop 只能接收已经提交的 user_message")
        actual_run_id = run_id or uuid4()
        working = list(initial_messages)
        turn_count = 0
        final_message_id: UUID | None = None
        continuation_of: UUID | None = None
        retry_of: UUID | None = None
        recorder = TraceRecorder(self._trace_sink)
        run_context = TraceContext(uuid4(), uuid4(), None, committer.session_id, actual_run_id, user_message.message_id)
        run_span = await recorder.start_span(
            "agent.run", run_context, input_message_id=str(user_message.message_id)
        )
        if isinstance(system_prompt, PromptInputs):
            frozen_inputs = system_prompt
        else:
            frozen_now = datetime.now().astimezone()
            frozen_inputs = PromptInputs(
                identity=system_prompt,
                current_time=frozen_now,
                timezone_name=str(frozen_now.tzinfo or ""),
            )
        system_context = await self._context_manager.start_run(frozen_inputs)
        provider_window = getattr(self._model, "context_window", None)
        context_window = int(
            self._context_budget if provider_window is None else provider_window
        )
        reserved_output = self._options.max_tokens
        if reserved_output is None:
            reserved_output = min(4096, max(1, context_window // 5))
        environment = AgentRunEnvironment(
            model_name=str(self._model_id or self._provider_name),
            system_context=system_context,
            context_window=context_window,
            reserved_output_tokens=reserved_output,
            current_user_message_id=user_message.message_id,
            run_id=actual_run_id,
            provider=self._model,
            cancellation=cancellation,
            trace_recorder=recorder,
            trace_context=run_context,
        )
        context_port = _BoundContextCommitPort(committer, actual_run_id)

        async def finish(reason, turns, final_id, error_message=None, error_category=None):
            result = AgentRunResult(
                reason=reason,
                turn_count=turns,
                final_message_id=final_id,
                error=ErrorInfo(
                    category=error_category or reason.value.lower(),
                    message=error_message,
                ) if error_message else None,
            )
            await committer.finish_run(actual_run_id, result)
            await run_span.finish(
                TraceStatus.CANCELLED if reason is StopReason.CANCELLED else TraceStatus.OK,
                turn_count=turns,
                stop_reason=reason.value,
                final_message_id=str(final_id) if final_id else None,
            )
            return result

        try:
            while True:
                if cancellation.cancelled:
                    return await finish(StopReason.CANCELLED, turn_count, final_message_id)
                if turn_count >= max_turns:
                    return await finish(StopReason.MAX_TURNS, turn_count, final_message_id)

                def current_working() -> WorkingContext:
                    return WorkingContext(
                        messages=tuple(working),
                        summaries=tuple(getattr(committer, "context_summaries", ())),
                    )

                current = current_working()
                tool_view = ToolView.from_specs(self._tools)
                preparation_id = uuid4()
                try:
                    context = await self._context_manager.before_model_call(
                        current,
                        environment,
                        tool_view,
                        context_port,
                        trigger_model_call_id=preparation_id,
                    )
                except ContextBudgetError as exc:
                    return await finish(
                        StopReason.PROMPT_TOO_LONG,
                        turn_count,
                        final_message_id,
                        str(exc),
                    )
                except asyncio.CancelledError:
                    return await finish(StopReason.CANCELLED, turn_count, final_message_id)
                compression_requested = False
                while True:
                    try:
                        hook_result = await self._dispatcher.before_model_call(
                            PreModelCallContext(
                                run_id=actual_run_id,
                                turn_number=turn_count + 1,
                                model_context=context,
                                tool_view_id="|".join(
                                    definition.name for definition in tool_view.definitions
                                ),
                            )
                        )
                    except asyncio.CancelledError:
                        return await finish(StopReason.CANCELLED, turn_count, final_message_id)
                    except HookExecutionError as exc:
                        return await finish(
                            StopReason.HOOK_FAILED,
                            turn_count,
                            final_message_id,
                            str(exc),
                            "hook_execution",
                        )
                    if isinstance(hook_result, ContinueModelCall):
                        break
                    if isinstance(hook_result, AbortRun):
                        return await finish(
                            StopReason.HOOK_ABORTED,
                            turn_count,
                            final_message_id,
                            hook_result.message,
                            hook_result.code,
                        )
                    if not isinstance(hook_result, RequestCompression):
                        raise AssertionError("Dispatcher 必须返回强类型 PreModelCall 结果")
                    if compression_requested:
                        return await finish(
                            StopReason.HOOK_FAILED,
                            turn_count,
                            final_message_id,
                            "PreModelCall Hook 重复请求上下文压缩",
                            "hook_compression_loop",
                        )
                    compression_requested = True
                    try:
                        context = await self._context_manager.request_compression(
                            current_working(),
                            environment,
                            tool_view,
                            context_port,
                            trigger_model_call_id=preparation_id,
                        )
                    except asyncio.CancelledError:
                        return await finish(StopReason.CANCELLED, turn_count, final_message_id)
                    except ContextBudgetError as exc:
                        return await finish(
                            StopReason.HOOK_FAILED,
                            turn_count,
                            final_message_id,
                            str(exc),
                            "hook_compression_failed",
                        )

                message_id = uuid4()
                turn_context = run_context.child(message_id=message_id)
                context_bytes = self._context_size(context)
                turn_span = await recorder.start_span(
                    "agent.turn",
                    turn_context,
                    turn=turn_count + 1,
                    continuation_of_message_id=str(continuation_of) if continuation_of else None,
                    retry_of_message_id=str(retry_of) if retry_of else None,
                    context_message_count=len(context.messages),
                    context_bytes=context_bytes,
                    compressed=context.compression_applied,
                )
                model_context = turn_context.child(message_id=message_id)
                model_span = await recorder.start_span(
                    "model.call",
                    model_context,
                    provider=self._provider_name,
                    model=self._model_id,
                    input_message_count=len(context.messages),
                    input_bytes=context_bytes,
                    temperature=self._options.temperature,
                    max_tokens=self._options.max_tokens,
                    tool_choice=self._options.tool_choice,
                    retry_of_message_id=str(retry_of) if retry_of else None,
                )
                await committer.publish_live(AssistantMessageStarted(
                    message_id=message_id,
                    continuation_of_message_id=continuation_of,
                    retry_of_message_id=retry_of,
                ))
                turn_count += 1
                processor = self._text_processor_factory()
                part_order: list[tuple[str, int]] = []
                text_chunks: list[list[str]] = []
                reasoning_chunks: list[list[str]] = []
                tool_drafts: dict[int, _ToolDraft] = {}
                terminal: ResponseCompleted | ResponseFailed | None = None
                text_delta_count = reasoning_delta_count = tool_delta_count = 0
                text_bytes = reasoning_bytes = tool_bytes = 0
                stream_started_ns = time.monotonic_ns()
                first_delta_ns: int | None = None
                last_delta_ns: int | None = None
                intervals_ms: list[float] = []

                def observe_delta() -> None:
                    nonlocal first_delta_ns, last_delta_ns
                    now = time.monotonic_ns()
                    if first_delta_ns is None:
                        first_delta_ns = now
                    if last_delta_ns is not None:
                        intervals_ms.append((now - last_delta_ns) / 1_000_000)
                    last_delta_ns = now

                try:
                    async for raw_event in self._model.stream(context, self._tools, self._options, cancellation):
                        cancellation.raise_if_cancelled()
                        for event in processor.feed(raw_event):
                            if terminal is not None:
                                raise RuntimeError("模型终态之后出现额外事件")
                            if isinstance(event, TextDelta):
                                observe_delta()
                                text_delta_count += 1
                                text_bytes += len(event.content.encode("utf-8"))
                                if not part_order or part_order[-1][0] != "text":
                                    text_chunks.append([])
                                    part_order.append(("text", len(text_chunks) - 1))
                                text_chunks[-1].append(event.content)
                                await committer.publish_live(AssistantPartDelta(message_id=message_id, kind="text", content=event.content))
                            elif isinstance(event, ReasoningDelta):
                                observe_delta()
                                reasoning_delta_count += 1
                                reasoning_bytes += len(event.content.encode("utf-8"))
                                if not part_order or part_order[-1][0] != "reasoning":
                                    reasoning_chunks.append([])
                                    part_order.append(("reasoning", len(reasoning_chunks) - 1))
                                reasoning_chunks[-1].append(event.content)
                                await committer.publish_live(AssistantPartDelta(message_id=message_id, kind="reasoning", content=event.content))
                            elif isinstance(event, ToolUseDelta):
                                observe_delta()
                                tool_delta_count += 1
                                tool_bytes += len(event.arguments_fragment.encode("utf-8"))
                                draft = tool_drafts.setdefault(event.index, _ToolDraft())
                                if ("tool", event.index) not in part_order:
                                    part_order.append(("tool", event.index))
                                draft.tool_use_id += event.tool_use_id_fragment
                                draft.name += event.name_fragment
                                draft.arguments += event.arguments_fragment
                                await committer.publish_live(AssistantPartDelta(message_id=message_id, kind="tool", content=event.arguments_fragment))
                            elif isinstance(event, (ResponseCompleted, ResponseFailed)):
                                terminal = event
                            else:
                                raise TypeError(f"未知 ModelEvent: {type(event)!r}")
                    for event in processor.finish():
                        if isinstance(event, TextDelta):
                            observe_delta()
                            text_chunks.append([event.content])
                            part_order.append(("text", len(text_chunks) - 1))
                            await committer.publish_live(AssistantPartDelta(message_id=message_id, kind="text", content=event.content))
                        elif isinstance(event, ReasoningDelta):
                            observe_delta()
                            reasoning_chunks.append([event.content])
                            part_order.append(("reasoning", len(reasoning_chunks) - 1))
                            await committer.publish_live(AssistantPartDelta(message_id=message_id, kind="reasoning", content=event.content))
                except asyncio.CancelledError:
                    await model_span.finish(TraceStatus.CANCELLED)
                    await turn_span.finish(TraceStatus.CANCELLED)
                    await committer.publish_live(AssistantMessageDiscarded(message_id=message_id, reason="cancelled"))
                    return await finish(StopReason.CANCELLED, turn_count, final_message_id)
                except ProviderNotConfiguredError as exc:
                    # 配置错误发生在 HTTP 请求前，不应计入实际 ModelCall。
                    turn_count -= 1
                    await model_span.finish(TraceStatus.ERROR, error_category="model_unavailable")
                    await turn_span.finish(TraceStatus.ERROR)
                    await committer.publish_live(AssistantMessageDiscarded(message_id=message_id, reason="model_unavailable"))
                    return await finish(StopReason.MODEL_UNAVAILABLE, turn_count, final_message_id, str(exc))

                if terminal is None:
                    terminal_received = False
                    terminal = ResponseFailed(category="protocol_error", message="模型流缺少终态")
                else:
                    terminal_received = True

                await recorder.emit(TraceEventType.STREAM_SUMMARY, model_context, {
                    "text_delta_count": text_delta_count,
                    "text_bytes": text_bytes,
                    "reasoning_delta_count": reasoning_delta_count,
                    "reasoning_bytes": reasoning_bytes,
                    "tool_delta_count": tool_delta_count,
                    "tool_bytes": tool_bytes,
                    "terminal_received": terminal_received,
                    "cancelled": False,
                    "first_delta_ms": None if first_delta_ns is None else (first_delta_ns - stream_started_ns) / 1_000_000,
                    "last_delta_ms": None if last_delta_ns is None else (last_delta_ns - stream_started_ns) / 1_000_000,
                    "interval_count": len(intervals_ms),
                    "interval_min_ms": min(intervals_ms) if intervals_ms else None,
                    "interval_max_ms": max(intervals_ms) if intervals_ms else None,
                    "interval_avg_ms": sum(intervals_ms) / len(intervals_ms) if intervals_ms else None,
                    "duration_ms": (time.monotonic_ns() - stream_started_ns) / 1_000_000,
                })

                if isinstance(terminal, ResponseFailed):
                    safe_error = sanitize_error({
                        "category": terminal.category,
                        "type": terminal.provider_type or "ProviderError",
                        "message": terminal.message,
                        "provider_code": terminal.provider_code,
                        "status_code": terminal.status_code,
                        "request_id": terminal.request_id,
                    })
                    await model_span.finish(TraceStatus.ERROR, error=safe_error)
                    await turn_span.finish(TraceStatus.ERROR)
                    await committer.publish_live(AssistantMessageDiscarded(message_id=message_id, reason=terminal.category))
                    if self._is_prompt_too_long(terminal):
                        return await finish(StopReason.PROMPT_TOO_LONG, turn_count, final_message_id, terminal.message)
                    if terminal.category in {"authentication", "client_error"}:
                        return await finish(StopReason.MODEL_UNAVAILABLE, turn_count, final_message_id, terminal.message)
                    retry_of = message_id
                    continuation_of = None
                    continue

                await model_span.finish(
                    TraceStatus.OK,
                    finish_reason=terminal.finish_reason,
                    usage=None if terminal.usage is None else {
                        "prompt_tokens": terminal.usage.prompt_tokens,
                        "completion_tokens": terminal.usage.completion_tokens,
                        "total_tokens": terminal.usage.total_tokens,
                    },
                    request_id=terminal.request_id,
                )
                if terminal.usage is not None:
                    self._context_manager.record_actual_prompt_tokens(
                        actual_run_id,
                        context.estimated_input_tokens,
                        terminal.usage.prompt_tokens,
                    )

                parts = []
                for kind, index in part_order:
                    if kind == "text":
                        content = "".join(text_chunks[index])
                        if content:
                            parts.append(TextPart(content))
                    elif kind == "reasoning":
                        content = "".join(reasoning_chunks[index])
                        if content:
                            parts.append(ReasoningPart(content))
                    else:
                        draft = tool_drafts[index]
                        parts.append(ToolUsePart(name=draft.name, arguments=draft.arguments, tool_use_id=draft.tool_use_id))
                if not parts:
                    parts.append(TextPart(""))
                assistant = Message(
                    role=Role.ASSISTANT,
                    parts=tuple(parts),
                    message_id=message_id,
                    continuation_of_message_id=continuation_of,
                    retry_of_message_id=retry_of,
                )
                for tool in (part for part in parts if isinstance(part, ToolUsePart)):
                    await committer.publish_live(ToolUseDetected(message_id=message_id, tool_use_id=tool.tool_use_id, name=tool.name, arguments=tool.arguments))
                await committer.commit_assistant(actual_run_id, assistant, terminal.finish_reason)
                final_message_id = message_id
                await self._dispatcher.assistant_message_completed(
                    AssistantMessageCompletedContext(
                        run_id=actual_run_id, message=assistant, finish_reason=terminal.finish_reason
                    )
                )
                working.append(assistant)
                tool_uses = tuple(part for part in parts if isinstance(part, ToolUsePart))

                if tool_uses:
                    if self._tool_executor is None:
                        raise RuntimeError("模型产生工具调用，但未配置 ToolExecutor")
                    batch = ToolExecutionBatch(
                        run_id=actual_run_id,
                        assistant_message_id=message_id,
                        tool_uses=tool_uses,
                        trace_id=run_context.trace_id,
                        parent_span_id=turn_context.span_id,
                    )
                    self._tool_executor.validate_batch(batch)
                    pre_tool_use_outcomes: list[PreToolUseOutcome] = []
                    try:
                        for tool_use in tool_uses:
                            registered = next(
                                (spec for spec in self._tools if spec.name == tool_use.name),
                                None,
                            )
                            hook_spec = None if registered is None else ToolSpec(
                                name=registered.name,
                                function_schema=registered.function_schema or {},
                            )
                            hook_result = await self._dispatcher.before_tool_use(
                                PreToolUseContext(
                                    run_id=actual_run_id,
                                    assistant_message_id=message_id,
                                    tool_use=tool_use,
                                    tool_spec=hook_spec,
                                )
                            )
                            if isinstance(hook_result, ContinueToolUse):
                                pre_tool_use_outcomes.append(
                                    PreToolUseOutcome(tool_use.tool_use_id)
                                )
                            elif isinstance(hook_result, RejectToolUse):
                                pre_tool_use_outcomes.append(PreToolUseOutcome(
                                    tool_use_id=tool_use.tool_use_id,
                                    rejection_code=hook_result.code,
                                    message=hook_result.message,
                                    field_errors=tuple(
                                        FieldError(path, message)
                                        for path, message in hook_result.field_errors
                                    ),
                                ))
                            else:
                                raise AssertionError("Dispatcher 必须返回强类型 PreToolUse 结果")
                        results = await self._tool_executor.submit_batch(
                            batch,
                            cancellation,
                            tuple(pre_tool_use_outcomes),
                        )
                    except asyncio.CancelledError:
                        await turn_span.finish(TraceStatus.CANCELLED)
                        return await finish(StopReason.CANCELLED, turn_count, final_message_id)
                    except HookExecutionError as exc:
                        await turn_span.finish(TraceStatus.ERROR, error_category="hook_execution")
                        return await finish(
                            StopReason.HOOK_FAILED,
                            turn_count,
                            final_message_id,
                            str(exc),
                            "hook_execution",
                        )
                    ordered = self._order_results(tool_uses, results, message_id)
                    for result in ordered:
                        tool_message = Message(
                            role=Role.TOOL,
                            parts=(ToolResultPart(
                                tool_use_id=result.tool_use_id,
                                assistant_message_id=result.assistant_message_id,
                                content=result.content,
                                is_error=result.is_error,
                                outcome_unknown=result.outcome_unknown,
                                tool_name=result.tool_name,
                                output=result.output,
                                failure=asdict(result.failure) if result.failure is not None else None,
                                artifact=asdict(result.artifact) if result.artifact is not None else None,
                            ),),
                        )
                        await committer.commit_tool_result(actual_run_id, tool_message)
                        await self._dispatcher.after_tool_use(
                            PostToolUseContext(
                                run_id=actual_run_id,
                                assistant_message_id=message_id,
                                result=result,
                                tool_message=tool_message,
                            )
                        )
                        working.append(tool_message)
                    if cancellation.cancelled:
                        await turn_span.finish(TraceStatus.CANCELLED)
                        return await finish(StopReason.CANCELLED, turn_count, final_message_id)

                await turn_span.finish(
                    TraceStatus.OK,
                    assistant_message_id=str(message_id),
                    tool_count=len(tool_uses),
                    finish_reason=terminal.finish_reason,
                )

                if terminal.finish_reason == "length":
                    continuation_of = message_id
                    retry_of = None
                    continue
                if tool_uses:
                    continuation_of = None
                    retry_of = None
                    continue
                return await finish(StopReason.COMPLETED, turn_count, final_message_id)
        except asyncio.CancelledError:
            return await finish(StopReason.CANCELLED, turn_count, final_message_id)
        except EventCommitError as exc:
            await run_span.finish(TraceStatus.ERROR, error_category="event_commit")
            return AgentRunResult(
                reason=StopReason.EVENT_COMMIT_FAILED,
                turn_count=turn_count,
                final_message_id=final_message_id,
                error=ErrorInfo(category="event_commit", message=str(exc)),
            )

    @staticmethod
    def _is_prompt_too_long(event: ResponseFailed) -> bool:
        return event.category == "prompt_too_long" or event.provider_code in {"context_length_exceeded", "prompt_too_long"}

    @staticmethod
    def _context_size(context) -> int:
        return sum(
            len(getattr(part, "content", getattr(part, "arguments", "")))
            for message in context.messages
            for part in message.parts
        )

    @staticmethod
    def _order_results(tool_uses, results, assistant_message_id):
        by_id = {result.tool_use_id: result for result in results}
        if len(by_id) != len(results) or set(by_id) != {tool.tool_use_id for tool in tool_uses}:
            raise RuntimeError("ToolExecutor 返回的 tool_use_id 集合与批次不匹配")
        if any(result.assistant_message_id != assistant_message_id for result in results):
            raise RuntimeError("ToolResult 的 assistant_message_id 不匹配")
        return tuple(by_id[tool.tool_use_id] for tool in tool_uses)
