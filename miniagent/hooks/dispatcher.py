from __future__ import annotations

import asyncio
from typing import Any

from .models import (
    AbortRun,
    AssistantMessageCompletedContext,
    ContinueModelCall,
    ContinueToolUse,
    PostToolUseContext,
    PreModelCallContext,
    PreToolUseContext,
    RejectToolUse,
    RequestCompression,
    TraceSink,
)
from .registry import HookRegistryView


class HookExecutionError(RuntimeError):
    def __init__(self, phase: str, index: int, hook: object, cause: BaseException | str) -> None:
        self.phase, self.index, self.hook_name = phase, index, type(hook).__name__
        self.cause = cause
        super().__init__(f"{phase} Hook[{index}] {self.hook_name} 执行失败: {cause}")


class HookDispatcher:
    def __init__(self, view: HookRegistryView, trace_sink: TraceSink | None = None) -> None:
        self._view = view
        self._trace_sink = trace_sink

    async def before_model_call(self, context: PreModelCallContext):
        for index, hook in enumerate(self._view.pre_model_call):
            try:
                result = await hook(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise HookExecutionError("pre_model_call", index, hook, exc) from exc
            if not isinstance(result, (ContinueModelCall, RequestCompression, AbortRun)):
                raise HookExecutionError("pre_model_call", index, hook, "非法返回值")
            if not isinstance(result, ContinueModelCall):
                return result
        return ContinueModelCall()

    async def before_tool_use(self, context: PreToolUseContext):
        for index, hook in enumerate(self._view.pre_tool_use):
            try:
                result = await hook(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise HookExecutionError("pre_tool_use", index, hook, exc) from exc
            if not isinstance(result, (ContinueToolUse, RejectToolUse)):
                raise HookExecutionError("pre_tool_use", index, hook, "非法返回值")
            if not isinstance(result, ContinueToolUse):
                return result
        return ContinueToolUse()

    async def assistant_message_completed(self, context: AssistantMessageCompletedContext) -> None:
        await self._notify("assistant_message_completed", self._view.assistant_message_completed, context)

    async def after_tool_use(self, context: PostToolUseContext) -> None:
        await self._notify("post_tool_use", self._view.post_tool_use, context)

    async def _notify(self, phase: str, hooks: tuple[Any, ...], context: Any) -> None:
        for index, hook in enumerate(hooks):
            try:
                result = await hook(context)
                if result is not None:
                    raise TypeError("通知 Hook 必须返回 None")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._trace_sink is not None:
                    await self._trace_sink.emit({
                        "event": "hook_notification_failed",
                        "phase": phase,
                        "hook_name": type(hook).__name__,
                        "hook_index": index,
                        "run_id": str(getattr(context, "run_id", "")),
                        "exception_type": type(exc).__name__,
                    })
