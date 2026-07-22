from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from .context import ContextBuilder, WorkingContext
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
from .events import (
    AssistantMessageCompleted,
    AssistantMessageDiscarded,
    AssistantMessageStarted,
    AssistantPartDelta,
    RunTerminated,
    ToolResultRecorded,
    ToolUseDetected,
    UserMessageRecorded,
)
from .ports import Cancellation, EventSink, GenerationOptions, ModelAdapter, ToolExecutor
from .provider.errors import ProviderNotConfiguredError
from .provider.events import ReasoningDelta, ResponseCompleted, ResponseFailed, TextDelta, ToolUseDelta
from .session import EventCommitError
from .text_processing import PassthroughTextProcessor


@dataclass(slots=True)
class _ToolDraft:
    tool_use_id: str = ""
    name: str = ""
    arguments: str = ""


class AgentLoop:
    def __init__(
        self,
        model: ModelAdapter,
        context_builder: ContextBuilder,
        tool_executor: ToolExecutor | None = None,
        tools: tuple[ToolSpec, ...] = (),
        options: GenerationOptions | None = None,
        context_budget: int = 16_000,
        text_processor_factory=PassthroughTextProcessor,
    ) -> None:
        self._model = model
        self._context_builder = context_builder
        self._tool_executor = tool_executor
        self._tools = tools
        self._options = options or GenerationOptions()
        self._context_budget = context_budget
        self._text_processor_factory = text_processor_factory

    async def run(
        self,
        initial_messages: tuple[Message, ...],
        user_message: Message,
        system_prompt: str,
        max_turns: int,
        event_sink: EventSink,
        cancellation: Cancellation,
        run_id: UUID | None = None,
    ) -> AgentRunResult:
        if max_turns <= 0:
            raise ValueError("max_turns 必须为正整数")
        if user_message.role is not Role.USER:
            raise ValueError("user_message 必须是 user 角色")
        actual_run_id = run_id or uuid4()
        working = list(initial_messages)
        turn_count = 0
        final_message_id: UUID | None = None
        continuation_of: UUID | None = None
        retry_of: UUID | None = None
        compression_used = False

        try:
            await event_sink.emit(UserMessageRecorded(message=user_message))
            working.append(user_message)
            while True:
                if cancellation.cancelled:
                    return await self._finish(event_sink, StopReason.CANCELLED, turn_count, final_message_id)
                if turn_count >= max_turns:
                    return await self._finish(event_sink, StopReason.MAX_TURNS, turn_count, final_message_id)

                current = WorkingContext(messages=tuple(working))
                context = (
                    self._context_builder.force_compress(current, system_prompt, self._context_budget)
                    if compression_used
                    else self._context_builder.build(current, system_prompt, self._context_budget)
                )
                message_id = uuid4()
                await event_sink.emit(AssistantMessageStarted(
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

                try:
                    async for raw_event in self._model.stream(context, self._tools, self._options, cancellation):
                        cancellation.raise_if_cancelled()
                        for event in processor.feed(raw_event):
                            if terminal is not None:
                                raise RuntimeError("模型终态之后出现额外事件")
                            if isinstance(event, TextDelta):
                                if not part_order or part_order[-1][0] != "text":
                                    text_chunks.append([])
                                    part_order.append(("text", len(text_chunks) - 1))
                                text_chunks[-1].append(event.content)
                                await event_sink.emit(AssistantPartDelta(message_id=message_id, kind="text", content=event.content))
                            elif isinstance(event, ReasoningDelta):
                                if not part_order or part_order[-1][0] != "reasoning":
                                    reasoning_chunks.append([])
                                    part_order.append(("reasoning", len(reasoning_chunks) - 1))
                                reasoning_chunks[-1].append(event.content)
                                await event_sink.emit(AssistantPartDelta(message_id=message_id, kind="reasoning", content=event.content))
                            elif isinstance(event, ToolUseDelta):
                                draft = tool_drafts.setdefault(event.index, _ToolDraft())
                                if ("tool", event.index) not in part_order:
                                    part_order.append(("tool", event.index))
                                draft.tool_use_id += event.tool_use_id_fragment
                                draft.name += event.name_fragment
                                draft.arguments += event.arguments_fragment
                                await event_sink.emit(AssistantPartDelta(message_id=message_id, kind="tool", content=event.arguments_fragment))
                            elif isinstance(event, (ResponseCompleted, ResponseFailed)):
                                terminal = event
                            else:
                                raise TypeError(f"未知 ModelEvent: {type(event)!r}")
                    for event in processor.finish():
                        if isinstance(event, TextDelta):
                            text_chunks.append([event.content])
                            part_order.append(("text", len(text_chunks) - 1))
                            await event_sink.emit(AssistantPartDelta(message_id=message_id, kind="text", content=event.content))
                        elif isinstance(event, ReasoningDelta):
                            reasoning_chunks.append([event.content])
                            part_order.append(("reasoning", len(reasoning_chunks) - 1))
                            await event_sink.emit(AssistantPartDelta(message_id=message_id, kind="reasoning", content=event.content))
                except asyncio.CancelledError:
                    await event_sink.emit(AssistantMessageDiscarded(message_id=message_id, reason="cancelled"))
                    return await self._finish(event_sink, StopReason.CANCELLED, turn_count, final_message_id)
                except ProviderNotConfiguredError as exc:
                    # 配置错误发生在 HTTP 请求前，不应计入实际 ModelCall。
                    turn_count -= 1
                    await event_sink.emit(AssistantMessageDiscarded(message_id=message_id, reason="model_unavailable"))
                    return await self._finish(event_sink, StopReason.MODEL_UNAVAILABLE, turn_count, final_message_id, str(exc))

                if terminal is None:
                    terminal = ResponseFailed(category="protocol_error", message="模型流缺少终态")

                if isinstance(terminal, ResponseFailed):
                    await event_sink.emit(AssistantMessageDiscarded(message_id=message_id, reason=terminal.category))
                    if self._is_prompt_too_long(terminal):
                        if compression_used:
                            return await self._finish(event_sink, StopReason.PROMPT_TOO_LONG, turn_count, final_message_id, terminal.message)
                        compressed = self._context_builder.force_compress(current, system_prompt, self._context_budget)
                        if self._context_size(compressed) >= self._context_size(context):
                            return await self._finish(event_sink, StopReason.PROMPT_TOO_LONG, turn_count, final_message_id, "强制压缩未减少输入")
                        compression_used = True
                        retry_of = message_id
                        continue
                    if terminal.category in {"authentication", "client_error"}:
                        return await self._finish(event_sink, StopReason.MODEL_UNAVAILABLE, turn_count, final_message_id, terminal.message)
                    retry_of = message_id
                    continuation_of = None
                    continue

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
                    await event_sink.emit(ToolUseDetected(message_id=message_id, tool_use_id=tool.tool_use_id, name=tool.name, arguments=tool.arguments))
                await event_sink.emit(AssistantMessageCompleted(message=assistant, finish_reason=terminal.finish_reason))
                working.append(assistant)
                final_message_id = message_id
                tool_uses = tuple(part for part in parts if isinstance(part, ToolUsePart))

                if tool_uses:
                    if self._tool_executor is None:
                        raise RuntimeError("模型产生工具调用，但未配置 ToolExecutor")
                    batch = ToolExecutionBatch(run_id=actual_run_id, assistant_message_id=message_id, tool_uses=tool_uses)
                    try:
                        results = await self._tool_executor.submit_batch(batch, cancellation)
                    except asyncio.CancelledError:
                        return await self._finish(event_sink, StopReason.CANCELLED, turn_count, final_message_id)
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
                            ),),
                        )
                        await event_sink.emit(ToolResultRecorded(message=tool_message))
                        working.append(tool_message)
                    if cancellation.cancelled:
                        return await self._finish(event_sink, StopReason.CANCELLED, turn_count, final_message_id)

                if terminal.finish_reason == "length":
                    continuation_of = message_id
                    retry_of = None
                    continue
                if tool_uses:
                    continuation_of = None
                    retry_of = None
                    continue
                return await self._finish(event_sink, StopReason.COMPLETED, turn_count, final_message_id)
        except EventCommitError as exc:
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

    @staticmethod
    async def _finish(event_sink, reason, turn_count, final_message_id, error_message=None):
        await event_sink.emit(RunTerminated(reason=reason.value, turn_count=turn_count))
        return AgentRunResult(
            reason=reason,
            turn_count=turn_count,
            final_message_id=final_message_id,
            error=ErrorInfo(category=reason.value.lower(), message=error_message) if error_message else None,
        )
