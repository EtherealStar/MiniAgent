from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from ..domain import Message, Role
from ..journal import JournalRecord, JournalRecordType, UserMessagePayload
from ..ports import Cancellation
from ..repository import OpenSession, SessionRepository
from ..session import SessionEngine
from .projection import SessionSnapshot


@dataclass(frozen=True, slots=True)
class AcceptedInput:
    message_id: UUID
    run_id: UUID


UpdateCallback = Callable[[object], Awaitable[None]]


class SessionHandle(Protocol):
    session_id: UUID

    async def submit(self, text: str) -> AcceptedInput: ...
    async def withdraw(self, message_id: UUID) -> bool: ...
    async def snapshot(self) -> SessionSnapshot: ...
    async def stop(self, reason: str) -> None: ...
    def subscribe(self, callback: UpdateCallback) -> Callable[[], None]: ...


class RuntimeSession:
    """把 SessionEngine 的队列和 AgentLoop 组合为一个唯一后台 worker。"""

    def __init__(
        self,
        opened: OpenSession,
        *,
        loop_factory: Callable[[], object],
        system_prompt: str = "你是 MiniAgent。",
        max_turns: int = 20,
    ) -> None:
        self.session_id = opened.session_id
        self._callbacks: set[UpdateCallback] = set()
        self._engine = SessionEngine(opened, ui_sink=self._publish)
        self._loop_factory = loop_factory
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        self._active_cancellation: Cancellation | None = None
        self._initial: tuple[UUID, Message] | None = None
        self._stopping = False
        self._worker: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        repository: SessionRepository,
        first_text: str,
        *,
        loop_factory: Callable[[], object],
        system_prompt: str = "你是 MiniAgent。",
        max_turns: int = 20,
    ) -> tuple[RuntimeSession, AcceptedInput]:
        if not first_text.strip():
            raise ValueError("输入不能为空")
        session_id, run_id = uuid4(), uuid4()
        message = Message.text(Role.USER, first_text)
        first = JournalRecord(
            1,
            JournalRecordType.USER_MESSAGE,
            session_id,
            run_id,
            datetime.now(timezone.utc),
            UserMessagePayload(message),
        )
        opened = await repository.create_session(session_id, first)
        runtime = cls(opened, loop_factory=loop_factory, system_prompt=system_prompt, max_turns=max_turns)
        runtime._initial = (run_id, message)
        runtime._start_worker()
        return runtime, AcceptedInput(message.message_id, run_id)

    @classmethod
    async def open(
        cls,
        opened: OpenSession,
        *,
        loop_factory: Callable[[], object],
        system_prompt: str = "你是 MiniAgent。",
        max_turns: int = 20,
    ) -> RuntimeSession:
        runtime = cls(opened, loop_factory=loop_factory, system_prompt=system_prompt, max_turns=max_turns)
        await runtime._engine.recover_interrupted()
        runtime._start_worker()
        return runtime

    async def submit(self, text: str) -> AcceptedInput:
        if self._stopping:
            raise RuntimeError("Session 正在停止")
        queued = await self._engine.submit(text)
        return AcceptedInput(queued.message.message_id, queued.run_id)

    async def withdraw(self, message_id: UUID) -> bool:
        return await self._engine.withdraw(message_id)

    async def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(self._engine.messages)

    def subscribe(self, callback: UpdateCallback) -> Callable[[], None]:
        self._callbacks.add(callback)

        def unsubscribe() -> None:
            self._callbacks.discard(callback)

        return unsubscribe

    @property
    def active(self) -> bool:
        return self._active_cancellation is not None

    def cancel_active(self) -> bool:
        if self._active_cancellation is None:
            return False
        self._active_cancellation.cancel()
        return True

    async def stop(self, reason: str) -> None:
        del reason  # 停止原因属于应用生命周期，不写入 Transcript。
        if self._stopping:
            return
        self._stopping = True
        if self._active_cancellation is not None:
            self._active_cancellation.cancel()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await self._engine.close()

    async def _publish(self, update: object) -> None:
        for callback in tuple(self._callbacks):
            try:
                await callback(update)
            except Exception:
                # UI 通知可丢失；一个订阅者失败不能回滚已持久化事实。
                continue

    def _start_worker(self) -> None:
        self._worker = asyncio.create_task(self._run_worker(), name=f"session-{self.session_id}")

    async def _run_worker(self) -> None:
        if self._initial is not None:
            run_id, message = self._initial
            self._initial = None
            await self._run_committed(run_id, message)
        while True:
            self._active_cancellation = Cancellation()
            try:
                await self._engine.run_next(
                    self._loop_factory(),
                    self._system_prompt,
                    self._max_turns,
                    self._active_cancellation,
                )
            finally:
                self._active_cancellation = None

    async def _run_committed(self, run_id: UUID, message: Message) -> None:
        self._active_cancellation = Cancellation()
        try:
            await self._loop_factory().run(
                self._engine.messages,
                message,
                self._system_prompt,
                self._max_turns,
                self._engine,
                self._active_cancellation,
                run_id,
            )
        finally:
            self._active_cancellation = None
