from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .domain import AgentRunResult, ContextSummary, ErrorInfo, Message, Role, StopReason
from .journal import (
    AssistantMessagePayload,
    ContextSummaryPayload,
    JournalRecord,
    JournalRecordType,
    RunTerminatedPayload,
    ToolResultPayload,
    UserMessagePayload,
)
from .repository import OpenSession
from .ports import Cancellation
from .updates import (
    AssistantMessageCompleted,
    RunTerminated,
    ToolResultCompleted,
    UserMessageCommitted,
    InputQueued,
    InputWithdrawn,
)
from .trace import sanitize_error


class EventCommitError(RuntimeError):
    pass


UiSink = Callable[[object], Awaitable[None]]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class QueuedInput:
    run_id: UUID
    message: Message


class SessionEngine:
    """Transcript 的唯一提交者；运行时通知不参与 Journal 恢复。"""

    def __init__(
        self,
        opened: OpenSession,
        *,
        ui_sink: UiSink | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._opened = opened
        self.session_id = opened.session_id
        self._ui_sink = ui_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._messages = list(opened.recovered.messages)
        self._context_summaries = list(opened.recovered.context_summaries)
        self._run_results = list(opened.recovered.run_results)
        self._failed = False
        self._queue: asyncio.Queue[QueuedInput] = asyncio.Queue()
        self._queued_ids: set[UUID] = set()
        self._withdrawn_ids: set[UUID] = set()
        self._run_lock = asyncio.Lock()
        self.ui_delivery_errors: list[Exception] = []

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    @property
    def context_summaries(self) -> tuple[ContextSummary, ...]:
        return tuple(self._context_summaries)

    @property
    def run_results(self) -> tuple[AgentRunResult, ...]:
        return tuple(self._run_results)

    @property
    def failed(self) -> bool:
        return self._failed

    async def submit(self, text: str) -> QueuedInput:
        if self._failed:
            raise EventCommitError("Session 已失效，不能继续排队输入")
        if not text.strip():
            raise ValueError("输入不能为空")
        queued = QueuedInput(uuid4(), Message.text(Role.USER, text))
        self._queued_ids.add(queued.message.message_id)
        await self._queue.put(queued)
        await self.publish_live(InputQueued(queued.run_id, queued.message))
        return queued

    async def withdraw(self, message_id: UUID) -> bool:
        if message_id not in self._queued_ids or message_id in self._withdrawn_ids:
            return False
        self._withdrawn_ids.add(message_id)
        await self.publish_live(InputWithdrawn(message_id))
        return True

    async def run_next(
        self,
        agent_loop,
        system_prompt: str,
        max_turns: int,
        cancellation: Cancellation | None = None,
    ) -> AgentRunResult:
        async with self._run_lock:
            while True:
                queued = await self._queue.get()
                self._queued_ids.discard(queued.message.message_id)
                if queued.message.message_id in self._withdrawn_ids:
                    self._withdrawn_ids.discard(queued.message.message_id)
                    self._queue.task_done()
                    continue
                break
            try:
                # 这是后续输入进入 AgentLoop 的唯一公开路径；提交失败时不会调用模型。
                await self.commit_user(queued.run_id, queued.message)
                return await agent_loop.run(
                    self.messages,
                    queued.message,
                    system_prompt,
                    max_turns,
                    self,
                    cancellation or Cancellation(),
                    queued.run_id,
                )
            finally:
                self._queue.task_done()

    async def commit_user(self, run_id: UUID, message: Message) -> None:
        if message.role is not Role.USER:
            raise EventCommitError("commit_user 只接受 user 消息")
        await self._commit(
            JournalRecordType.USER_MESSAGE,
            run_id,
            UserMessagePayload(message),
            lambda: self._messages.append(message),
            UserMessageCommitted(message=message),
        )

    async def commit_assistant(
        self,
        run_id: UUID,
        message: Message,
        finish_reason: str | None,
    ) -> None:
        if message.role is not Role.ASSISTANT:
            raise EventCommitError("commit_assistant 只接受 assistant 消息")
        await self._commit(
            JournalRecordType.ASSISTANT_MESSAGE,
            run_id,
            AssistantMessagePayload(message, finish_reason),
            lambda: self._messages.append(message),
            AssistantMessageCompleted(message=message, finish_reason=finish_reason),
        )

    async def commit_tool_result(self, run_id: UUID, message: Message) -> None:
        if message.role is not Role.TOOL:
            raise EventCommitError("commit_tool_result 只接受 tool 消息")
        await self._commit(
            JournalRecordType.TOOL_RESULT,
            run_id,
            ToolResultPayload(message),
            lambda: self._messages.append(message),
            ToolResultCompleted(message=message),
        )

    async def commit_context_summary(self, run_id: UUID, summary: ContextSummary) -> None:
        await self._commit(
            JournalRecordType.CONTEXT_SUMMARY,
            run_id,
            ContextSummaryPayload(summary),
            lambda: self._context_summaries.append(summary),
            summary,
        )

    async def finish_run(self, run_id: UUID, result: AgentRunResult) -> None:
        if result.error is not None:
            safe = sanitize_error({
                "category": result.error.category,
                "type": "AgentRunError",
                "message": result.error.message,
            })
            result = replace(
                result,
                error=ErrorInfo(str(safe["category"]), str(safe["message"])),
            )
        await self._commit(
            JournalRecordType.RUN_TERMINATED,
            run_id,
            RunTerminatedPayload.from_result(result),
            lambda: self._run_results.append(result),
            RunTerminated(reason=result.reason.value, turn_count=result.turn_count),
        )

    async def _commit(self, record_type, run_id, payload, apply, update) -> None:
        if self._failed:
            raise EventCommitError("Session 已因 Journal 失败而必须重新打开")
        record = JournalRecord(1, record_type, self.session_id, run_id, self._clock(), payload)
        try:
            await self._opened.append(record)
        except Exception as exc:
            self._failed = True
            raise EventCommitError("Journal 提交失败，Session 必须重新打开") from exc
        # 只有 fsync 返回后，内存 Transcript 和 UI 才能观察到新事实。
        apply()
        await self.publish_live(update)

    async def publish_live(self, update: object) -> None:
        if self._ui_sink is None:
            return
        try:
            await self._ui_sink(update)
        except Exception as exc:
            self.ui_delivery_errors.append(exc)

    async def recover_interrupted(self) -> AgentRunResult | None:
        run_id = self._opened.recovered.interrupted_run
        if run_id is None:
            return None
        assistants = [
            record.payload.message
            for record in self._opened.records
            if record.run_id == run_id
            and record.record_type is JournalRecordType.ASSISTANT_MESSAGE
            and isinstance(record.payload, AssistantMessagePayload)
        ]
        result = AgentRunResult(
            StopReason.PROCESS_INTERRUPTED,
            len(assistants),
            assistants[-1].message_id if assistants else None,
        )
        await self.finish_run(run_id, result)
        return result

    async def close(self) -> None:
        await self._opened.close()
