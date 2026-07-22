from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from .domain import Message
from .events import (
    AssistantMessageCompleted,
    AssistantMessageDiscarded,
    AssistantMessageStarted,
    EventPayload,
    SessionEvent,
    ToolResultRecorded,
    UserMessageRecorded,
)
from .ports import Cancellation
from .storage import TranscriptStore


class EventCommitError(RuntimeError):
    pass


UiSink = Callable[[SessionEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class QueuedInput:
    run_id: UUID
    message: Message


class SessionEngine:
    """会话事件的唯一写入边界，并维护可重放的消息投影。"""

    def __init__(self, session_id: UUID | None = None, ui_sink: UiSink | None = None, transcript_store: TranscriptStore | None = None) -> None:
        self.session_id = session_id or uuid4()
        self._ui_sink = ui_sink
        self._transcript_store = transcript_store
        self._events: list[SessionEvent] = []
        self._event_ids: dict[UUID, SessionEvent] = {}
        self._messages: list[Message] = []
        self._drafts: set[UUID] = set()
        self._queue: asyncio.Queue[QueuedInput] = asyncio.Queue()
        self._cancellations: dict[UUID, Cancellation] = {}
        self._active_run_id: UUID | None = None
        self._recovered_runs: set[UUID] = set()
        self.ui_delivery_errors: list[Exception] = []
        self._lock = asyncio.Lock()

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def begin_run(self, run_id: UUID | None = None) -> tuple[UUID, Cancellation]:
        if self._active_run_id is not None:
            raise RuntimeError("同一 Session 同时只能运行一个 AgentRun")
        actual = run_id or uuid4()
        cancellation = Cancellation()
        self._active_run_id = actual
        self._cancellations[actual] = cancellation
        return actual, cancellation

    def finish_run(self, run_id: UUID) -> None:
        if self._active_run_id == run_id:
            self._active_run_id = None

    async def enqueue_input(self, message: Message) -> UUID:
        run_id = uuid4()
        await self._queue.put(QueuedInput(run_id=run_id, message=message))
        return run_id

    async def next_input(self) -> QueuedInput:
        return await self._queue.get()

    def cancel(self, run_id: UUID) -> bool:
        signal = self._cancellations.get(run_id)
        if signal is None:
            return False
        signal.cancel()
        return True

    async def emit(self, payload: EventPayload) -> SessionEvent:
        if self._active_run_id is None:
            raise EventCommitError("没有活动的 AgentRun")
        async with self._lock:
            existing = self._event_ids.get(payload.event_id)
            if existing is not None:
                return existing
            event = SessionEvent.create(
                session_id=self.session_id,
                run_id=self._active_run_id,
                sequence=len(self._events) + 1,
                payload=payload,
            )
            self._validate_projection(payload)
            if self._transcript_store is not None:
                try:
                    await self._transcript_store.append(event)
                except Exception as exc:
                    raise EventCommitError("transcript 持久化失败") from exc
            # 持久化确认后才更新内存投影，失败内容不会进入 Working Context。
            self._apply_projection(payload)
            self._events.append(event)
            self._event_ids[event.event_id] = event
        if self._ui_sink is not None:
            try:
                await self._ui_sink(event)
            except Exception as exc:  # UI 断线不改变 AgentRun 控制流。
                self.ui_delivery_errors.append(exc)
        return event

    def _apply_projection(self, payload: EventPayload) -> None:
        if isinstance(payload, UserMessageRecorded):
            self._messages.append(payload.message)
        elif isinstance(payload, AssistantMessageStarted):
            self._drafts.add(payload.message_id)
        elif isinstance(payload, AssistantMessageCompleted):
            self._drafts.remove(payload.message.message_id)
            self._messages.append(payload.message)
        elif isinstance(payload, AssistantMessageDiscarded):
            self._drafts.discard(payload.message_id)
        elif isinstance(payload, ToolResultRecorded):
            self._messages.append(payload.message)

    def _validate_projection(self, payload: EventPayload) -> None:
        if isinstance(payload, UserMessageRecorded):
            if any(message.message_id == payload.message.message_id for message in self._messages):
                raise EventCommitError("message_id 重复")
        elif isinstance(payload, AssistantMessageStarted):
            if payload.message_id in self._drafts or any(m.message_id == payload.message_id for m in self._messages):
                raise EventCommitError("message_id 重复")
        elif isinstance(payload, AssistantMessageCompleted):
            if payload.message.message_id not in self._drafts:
                raise EventCommitError("完成了未知的 Assistant 草稿")
        elif isinstance(payload, ToolResultRecorded):
            known_assistant_ids = {m.message_id for m in self._messages}
            for part in payload.message.parts:
                if part.assistant_message_id not in known_assistant_ids:  # type: ignore[attr-defined]
                    raise EventCommitError("工具结果来源 AssistantMessage 不存在")

    async def recover_interrupted(self, run_id: UUID) -> tuple[UUID, ...]:
        """只作废未完成草稿；未知工具绝不在恢复时执行。"""
        if run_id in self._recovered_runs:
            return ()
        previous = self._active_run_id
        self._active_run_id = run_id
        discarded: list[UUID] = []
        try:
            for message_id in tuple(self._drafts):
                await self.emit(AssistantMessageDiscarded(message_id=message_id, reason="process_interrupted"))
                discarded.append(message_id)
            from .events import RunTerminated
            await self.emit(RunTerminated(reason="PROCESS_INTERRUPTED", turn_count=0))
            self._recovered_runs.add(run_id)
        finally:
            self._active_run_id = previous
        return tuple(discarded)

    def replay_after(self, sequence: int) -> tuple[SessionEvent, ...]:
        return tuple(event for event in self._events if event.sequence > sequence)
