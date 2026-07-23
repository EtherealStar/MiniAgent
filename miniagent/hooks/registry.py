from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from .models import (
    AssistantMessageCompletedHook,
    PostToolUseHook,
    PreModelCallHook,
    PreToolUseHook,
)


class HookRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HookRegistryView:
    pre_model_call: tuple[PreModelCallHook, ...]
    assistant_message_completed: tuple[AssistantMessageCompletedHook, ...]
    pre_tool_use: tuple[PreToolUseHook, ...]
    post_tool_use: tuple[PostToolUseHook, ...]


class HookRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, list[Any]] = {
            "pre_model_call": [],
            "assistant_message_completed": [],
            "pre_tool_use": [],
            "post_tool_use": [],
        }
        self._view: HookRegistryView | None = None

    @property
    def frozen(self) -> bool:
        return self._view is not None

    def _register(self, phase: str, hook: Any) -> None:
        if self.frozen:
            raise HookRegistryError("Hook 注册表冻结后不能继续注册")
        async_callable = inspect.iscoroutinefunction(hook) or inspect.iscoroutinefunction(getattr(hook, "__call__", None))
        if not callable(hook) or not async_callable:
            raise HookRegistryError("Hook 必须是可异步调用对象")
        self._pending[phase].append(hook)

    def register_pre_model_call(self, hook: PreModelCallHook) -> None:
        self._register("pre_model_call", hook)

    def register_assistant_message_completed(self, hook: AssistantMessageCompletedHook) -> None:
        self._register("assistant_message_completed", hook)

    def register_pre_tool_use(self, hook: PreToolUseHook) -> None:
        self._register("pre_tool_use", hook)

    def register_post_tool_use(self, hook: PostToolUseHook) -> None:
        self._register("post_tool_use", hook)

    def freeze(self) -> HookRegistryView:
        if self._view is None:
            # 冻结成 tuple 快照，调用方无法改变注册顺序或集合。
            self._view = HookRegistryView(
                *(tuple(self._pending[name]) for name in self._pending)
            )
        return self._view
