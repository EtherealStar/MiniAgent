from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from .domain import Message, ToolExecutionBatch, ToolResult, ToolSpec
from .provider.events import ModelEvent


class Cancellation:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    tool_choice: str | dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens 必须为正整数")


@dataclass(frozen=True, slots=True)
class ModelContext:
    messages: tuple[Message, ...]


class ModelAdapter(Protocol):
    async def stream(
        self,
        context: ModelContext,
        tools: tuple[ToolSpec, ...],
        options: GenerationOptions,
        cancellation: Cancellation,
    ) -> AsyncIterator[ModelEvent]: ...


class ToolExecutor(Protocol):
    async def submit_batch(
        self, batch: ToolExecutionBatch, cancellation: Cancellation
    ) -> tuple[ToolResult, ...]: ...


class EventSink(Protocol):
    async def emit(self, payload: object) -> object: ...
