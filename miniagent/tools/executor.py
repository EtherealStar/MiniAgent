from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError

from miniagent.domain import ToolExecutionBatch, ToolResult, ToolUsePart
from miniagent.ports import Cancellation
from miniagent.trace import (
    BestEffortTraceSink,
    TraceContext,
    TraceEventType,
    TraceRecorder,
    TraceSpan,
    TraceStatus,
)

from .artifacts import FileArtifactStore, MemoryTraceSink
from .models import (
    ExecutionContext,
    ExecutionTraits,
    FieldError,
    PreToolUseOutcome,
    ToolExecutionError,
    ToolFailure,
    ToolProtocolError,
    ToolSpec,
)
from .policy import TargetPolicyError
from .registry import ToolRegistryView
from .validation import fast_validate_tool_use


@dataclass(slots=True)
class _PreparedCall:
    use: ToolUsePart
    spec: ToolSpec | None = None
    args: Any = None
    targets: tuple = ()
    traits: ExecutionTraits = ExecutionTraits()
    result: ToolResult | None = None
    trace_context: TraceContext | None = None
    trace_span: TraceSpan | None = None
    batch_position: int = 0


class _AttemptCancellation:
    def __init__(self, parent: Cancellation) -> None:
        self._parent = parent
        self._local = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._parent.cancelled or self._local.is_set()

    def cancel(self) -> None:
        self._local.set()

    async def wait(self) -> None:
        if self.cancelled:
            return
        parent_task = asyncio.create_task(self._parent.wait())
        local_task = asyncio.create_task(self._local.wait())
        done, pending = await asyncio.wait({parent_task, local_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistryView,
        workspace_root: Path,
        session_id: str,
        *,
        artifact_store=None,
        trace_sink=None,
    ) -> None:
        self.registry = registry
        self.workspace_root = workspace_root.resolve(strict=True)
        self.session_id = session_id
        self.artifact_store = artifact_store or FileArtifactStore(self.workspace_root)
        raw_trace_sink = trace_sink or MemoryTraceSink()
        self.trace_sink = (
            raw_trace_sink
            if isinstance(raw_trace_sink, BestEffortTraceSink)
            else BestEffortTraceSink(raw_trace_sink)
        )
        self._trace_recorder = TraceRecorder(self.trace_sink)
        self._seen_ids: set[str] = set()
        self._correctable: dict[str, tuple[str, bool]] = {}
        self._correction_calls: set[str] = set()

    def validate_batch(self, batch: ToolExecutionBatch) -> None:
        ids = [use.tool_use_id for use in batch.tool_uses]
        if any(not item for item in ids) or len(ids) != len(set(ids)) or self._seen_ids.intersection(ids):
            raise ToolProtocolError("tool_use_id 缺失或重复")

    async def submit_batch(
        self,
        batch: ToolExecutionBatch,
        cancellation: Cancellation,
        pre_tool_use_outcomes: tuple[PreToolUseOutcome, ...] | None = None,
    ) -> tuple[ToolResult, ...]:
        self.validate_batch(batch)
        ids = [use.tool_use_id for use in batch.tool_uses]
        outcomes = pre_tool_use_outcomes or tuple(
            PreToolUseOutcome(tool_use_id=item) for item in ids
        )
        if len(outcomes) != len(ids) or [item.tool_use_id for item in outcomes] != ids:
            raise ToolProtocolError("PreToolUse outcome 与工具批次不匹配")
        self._seen_ids.update(ids)
        correction_eligible = frozenset(self._correctable)
        prepared = [
            await self._prepare(use, batch, correction_eligible, position, outcomes[position])
            for position, use in enumerate(batch.tool_uses)
        ]
        for item in prepared:
            if item.result is not None and item.result.is_error:
                await self._trace("call_started", item, attempt=0)
                await self._trace("call_error", item, attempt=0, status=item.result.status)
                await self._trace("call_finished", item, attempt=0, status=item.result.status)
        index = 0
        while index < len(prepared):
            current = prepared[index]
            if current.result is not None:
                index += 1
                continue
            if cancellation.cancelled:
                for pending in prepared[index:]:
                    if pending.result is None:
                        pending.result = self._cancelled_result(pending, batch)
                        await self._trace("call_started", pending, attempt=0)
                        await self._trace(
                            "call_error",
                            pending,
                            attempt=0,
                            status=pending.result.status,
                            outcome_unknown=pending.result.outcome_unknown,
                        )
                        await self._trace(
                            "call_finished",
                            pending,
                            attempt=0,
                            status=pending.result.status,
                            outcome_unknown=pending.result.outcome_unknown,
                        )
                break
            if current.traits.concurrency_safe:
                end = index
                while end < len(prepared) and prepared[end].result is None and prepared[end].traits.concurrency_safe:
                    end += 1
                results = await asyncio.gather(*(self._execute(item, batch, cancellation) for item in prepared[index:end]))
                for item, result in zip(prepared[index:end], results, strict=True):
                    item.result = result
                index = end
            else:
                current.result = await self._execute(current, batch, cancellation)
                index += 1
        return tuple(item.result for item in prepared if item.result is not None)

    async def _prepare(
        self,
        use: ToolUsePart,
        batch: ToolExecutionBatch,
        correction_eligible: frozenset[str],
        position: int,
        preflight: PreToolUseOutcome,
    ) -> _PreparedCall:
        trace_id = batch.trace_id or uuid4()
        session_id = self._as_uuid(self.session_id)
        item = _PreparedCall(
            use,
            trace_context=TraceContext(
                trace_id,
                uuid4(),
                batch.parent_span_id,
                session_id,
                batch.run_id,
                batch.assistant_message_id,
            ),
            batch_position=position,
        )
        spec = self.registry.get(use.name)
        if spec is None:
            item.result = self._failure_result(use, batch, ToolFailure("unknown_tool", "resolve_tool", f"未知工具: {use.name}"))
            return item
        item.spec = spec
        try:
            raw = json.loads(use.arguments)
        except (json.JSONDecodeError, TypeError):
            return self._parameter_failure(item, batch, "malformed_arguments", "arguments 不是合法 JSON object")
        if not isinstance(raw, dict):
            return self._parameter_failure(item, batch, "malformed_arguments", "arguments 顶层必须是 object")
        marker = raw.pop("correction_of_tool_use_id", ...)
        correction_failure = self._check_correction(use, marker, correction_eligible)
        if correction_failure:
            item.result = self._failure_result(use, batch, correction_failure)
            return item
        if not preflight.accepted:
            return self._parameter_failure(
                item,
                batch,
                preflight.rejection_code,
                preflight.message,
                preflight.field_errors,
            )
        # Executor 仍是最终真相边界；这里与默认 PreToolUse Hook 共用同一快筛逻辑。
        fast = fast_validate_tool_use(use, spec)
        if not fast.valid:
            return self._parameter_failure(item, batch, fast.code, fast.message, fast.field_errors)
        parameters = spec.function_schema["function"]["parameters"]
        business_properties = set(parameters["properties"]) - {"correction_of_tool_use_id"}
        missing = business_properties - set(raw)
        extra = set(raw) - business_properties
        if marker is ...:
            missing.add("correction_of_tool_use_id")
        if missing or extra:
            errors = tuple(
                [FieldError(name, "缺少必填字段") for name in sorted(missing)]
                + [FieldError(name, "不允许的额外字段") for name in sorted(extra)]
            )
            return self._parameter_failure(item, batch, "invalid_arguments", "参数字段不符合 strict schema", errors)
        try:
            item.args = spec.input_model.model_validate(raw, strict=True, by_alias=True, by_name=False)
        except ValidationError as exc:
            errors = tuple(FieldError(".".join(str(part) for part in error["loc"]), error["msg"]) for error in exc.errors())
            return self._parameter_failure(item, batch, "invalid_arguments", "参数校验失败", errors, "pydantic_validation")
        try:
            item.targets = spec.resolve_targets(item.args, self.workspace_root)
        except (TargetPolicyError, ToolExecutionError, OSError, ValueError) as exc:
            item.result = self._failure_result(use, batch, ToolFailure("target_denied", "target_policy", str(exc)))
            return item
        try:
            item.traits = spec.classify(item.args, item.targets)
        except Exception:
            # 分类器异常时采用最保守的串行策略，但不把启动错误泄露给模型。
            item.traits = ExecutionTraits(concurrency_safe=False)
        return item

    def _check_correction(self, use: ToolUsePart, marker: object, eligible: frozenset[str]) -> ToolFailure | None:
        if marker is ... or marker is None:
            return None
        if not isinstance(marker, str) or not marker:
            return ToolFailure("correction_not_allowed", "correction", "修正引用必须是非空字符串")
        original = self._correctable.get(marker) if marker in eligible else None
        if original is None or original[0] != use.name or original[1]:
            return ToolFailure("correction_not_allowed", "correction", "修正引用无效、跨工具或已使用")
        if marker in self._correction_calls:
            return ToolFailure("correction_not_allowed", "correction", "修正调用不能形成链")
        self._correctable[marker] = (original[0], True)
        self._correction_calls.add(use.tool_use_id)
        return None

    def _parameter_failure(self, item, batch, code, message, errors=(), stage="fast_validation"):
        failure = ToolFailure(code, stage, message, errors, correctable=True)
        if item.use.tool_use_id not in self._correction_calls:
            self._correctable[item.use.tool_use_id] = (item.use.name, False)
        else:
            failure = ToolFailure(code, stage, message, errors, correctable=False)
        item.result = self._failure_result(item.use, batch, failure)
        return item

    async def _execute(self, item: _PreparedCall, batch: ToolExecutionBatch, cancellation: Cancellation) -> ToolResult:
        assert item.spec is not None
        await self._trace("call_started", item, attempt=0)
        attempts = 0
        while attempts < item.spec.retry_policy.max_attempts:
            if cancellation.cancelled:
                code = "cancelled" if item.traits.concurrency_safe else "outcome_unknown"
                return await self._execution_failure(item, batch, attempts, code, "工具执行已取消", code == "outcome_unknown")
            attempts += 1
            await self._trace("attempt_started", item, attempt=attempts)
            attempt_cancellation = _AttemptCancellation(cancellation)
            context = ExecutionContext(
                session_id=self.session_id,
                run_id=str(batch.run_id),
                tool_use_id=item.use.tool_use_id,
                workspace_root=self.workspace_root,
                cancellation=attempt_cancellation,
                trace_sink=self.trace_sink,
                artifact_store=self.artifact_store,
                targets=item.targets,
            )
            try:
                content = await self._run_handler(item, context, attempt_cancellation)
                artifact = None
                visible = content
                encoded = content.encode("utf-8")
                if len(encoded) > item.spec.result_policy.threshold_bytes:
                    artifact = self.artifact_store.persist(self.session_id, item.use.tool_use_id, content)
                    preview = encoded[: item.spec.result_policy.preview_bytes].decode("utf-8", errors="ignore")
                    visible = (
                        f"结果超过 {item.spec.result_policy.threshold_bytes} 字节，完整内容已外置。\n"
                        f"artifact: {artifact.path}\nbytes: {artifact.byte_count}\nsha256: {artifact.sha256}\n"
                        f"preview:\n{preview}"
                    )
                result = ToolResult(item.use.tool_use_id, batch.assistant_message_id, visible, tool_name=item.use.name, attempts=attempts, artifact=artifact)
                await self._trace("call_finished", item, attempt=attempts, status="success", result_bytes=len(encoded))
                return result
            except asyncio.TimeoutError:
                code = "timeout" if item.traits.concurrency_safe else "outcome_unknown"
                return await self._execution_failure(item, batch, attempts, code, "工具执行超时", code == "outcome_unknown")
            except asyncio.CancelledError:
                code = "cancelled" if item.traits.concurrency_safe else "outcome_unknown"
                return await self._execution_failure(item, batch, attempts, code, "工具执行已取消", code == "outcome_unknown")
            except ToolExecutionError as exc:
                if exc.transient and not exc.outcome_unknown and attempts < item.spec.retry_policy.max_attempts:
                    await self._trace("retry_scheduled", item, attempt=attempts)
                    if item.spec.retry_policy.retry_delay_seconds:
                        try:
                            await asyncio.wait_for(cancellation.wait(), timeout=item.spec.retry_policy.retry_delay_seconds)
                        except asyncio.TimeoutError:
                            pass
                    continue
                code = "outcome_unknown" if exc.outcome_unknown else "execution_failed"
                return await self._execution_failure(item, batch, attempts, code, str(exc), exc.outcome_unknown, exc.transient)
            except Exception as exc:
                return await self._execution_failure(item, batch, attempts, "execution_failed", str(exc), False)
        raise AssertionError("重试循环必须返回终态")

    async def _run_handler(self, item, context, cancellation):
        handler_task = asyncio.create_task(item.spec.handler(item.args, context))
        cancel_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {handler_task, cancel_task},
                timeout=item.spec.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if handler_task in done:
                return handler_task.result()
            cancellation.cancel()
            handler_task.cancel()
            try:
                await handler_task
            except asyncio.CancelledError:
                pass
            if cancel_task in done:
                raise asyncio.CancelledError
            raise asyncio.TimeoutError
        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass

    async def _execution_failure(self, item, batch, attempts, code, message, unknown, retryable=False):
        failure = ToolFailure(code, "execution", message, retryable=retryable)
        result = self._failure_result(item.use, batch, failure, attempts, unknown)
        await self._trace("call_error", item, attempt=attempts, status=code, outcome_unknown=unknown)
        await self._trace("call_finished", item, attempt=attempts, status=code, outcome_unknown=unknown)
        return result

    def _cancelled_result(self, item, batch):
        code = "cancelled" if item.traits.concurrency_safe else "outcome_unknown"
        return self._failure_result(item.use, batch, ToolFailure(code, "execution", "批次已取消"), 0, code == "outcome_unknown")

    @staticmethod
    def _failure_result(use, batch, failure, attempts=0, outcome_unknown=False):
        content = json.dumps(
            {"code": failure.code, "stage": failure.stage, "message": failure.message,
             "field_errors": [{"path": error.path, "message": error.message} for error in failure.field_errors],
             "correctable": failure.correctable, "retryable": failure.retryable},
            ensure_ascii=False,
        )
        return ToolResult(use.tool_use_id, batch.assistant_message_id, content, True, outcome_unknown, use.name, failure.code, attempts, failure)

    async def _trace(self, event, item: _PreparedCall, **fields):
        assert item.trace_context is not None
        payload = {
            "tool_use_id": item.use.tool_use_id,
            "tool_name": item.use.name,
            "assistant_message_id": str(item.trace_context.message_id),
            "batch_position": item.batch_position,
            **fields,
        }
        if event == "call_started":
            item.trace_span = await self._trace_recorder.start_span(
                "tool.call", item.trace_context, **payload
            )
        elif event == "attempt_started":
            await self._trace_recorder.emit(TraceEventType.ATTEMPT_STARTED, item.trace_context, payload)
        elif event == "retry_scheduled":
            await self._trace_recorder.emit(TraceEventType.RETRY_SCHEDULED, item.trace_context, payload)
        elif event == "call_error":
            await self._trace_recorder.emit(TraceEventType.TOOL_FINISHED, item.trace_context, payload)
        elif event == "call_finished":
            if fields.get("status") == "success":
                await self._trace_recorder.emit(TraceEventType.TOOL_FINISHED, item.trace_context, payload)
            if item.trace_span is not None:
                status = TraceStatus.OK if fields.get("status") == "success" else TraceStatus.ERROR
                finish_payload = {key: value for key, value in payload.items() if key != "status"}
                finish_payload["outcome_status"] = fields.get("status")
                await item.trace_span.finish(status, **finish_payload)

    @staticmethod
    def _as_uuid(value: str) -> UUID:
        try:
            return UUID(value)
        except ValueError:
            return uuid5(NAMESPACE_URL, value)
