from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, TypeAlias
from uuid import UUID

from .domain import (
    AgentRunResult,
    ContextSummary,
    ErrorInfo,
    Message,
    ReasoningPart,
    ReasoningSource,
    ReasoningVisibility,
    Role,
    StopReason,
    TextPart,
    ToolResultPart,
    ToolUsePart,
    message_to_dict,
)


class JournalRecordType(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    CONTEXT_SUMMARY = "context_summary"
    RUN_TERMINATED = "run_terminated"


@dataclass(frozen=True, slots=True)
class UserMessagePayload:
    message: Message


@dataclass(frozen=True, slots=True)
class AssistantMessagePayload:
    message: Message
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResultPayload:
    message: Message


@dataclass(frozen=True, slots=True)
class ContextSummaryPayload:
    summary: ContextSummary


@dataclass(frozen=True, slots=True)
class RunTerminatedPayload:
    reason: StopReason
    turn_count: int
    final_message_id: UUID | None = None
    error: ErrorInfo | None = None

    @classmethod
    def from_result(cls, result: AgentRunResult) -> RunTerminatedPayload:
        return cls(result.reason, result.turn_count, result.final_message_id, result.error)

    def to_result(self) -> AgentRunResult:
        return AgentRunResult(self.reason, self.turn_count, self.final_message_id, self.error)


JournalPayload: TypeAlias = (
    UserMessagePayload
    | AssistantMessagePayload
    | ToolResultPayload
    | ContextSummaryPayload
    | RunTerminatedPayload
)


@dataclass(frozen=True, slots=True)
class JournalRecord:
    schema_version: int
    record_type: JournalRecordType
    session_id: UUID
    run_id: UUID
    occurred_at: datetime
    payload: JournalPayload

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version 必须是整数 1")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at 必须包含时区")


class JournalCorruptionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveredSession:
    session_id: UUID
    messages: tuple[Message, ...]
    context_summaries: tuple[ContextSummary, ...]
    run_results: tuple[AgentRunResult, ...]
    interrupted_run: UUID | None


class JournalCodec:
    _ENVELOPE_FIELDS = {
        "schema_version",
        "record_type",
        "session_id",
        "run_id",
        "occurred_at",
        "payload",
    }

    @classmethod
    def encode(cls, record: JournalRecord) -> bytes:
        payload = cls._payload_to_dict(record.payload)
        value = {
            "schema_version": record.schema_version,
            "record_type": record.record_type.value,
            "session_id": str(record.session_id),
            "run_id": str(record.run_id),
            "occurred_at": cls._format_datetime(record.occurred_at),
            "payload": payload,
        }
        return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    @classmethod
    def decode(cls, line: bytes | str, *, line_number: int | None = None) -> JournalRecord:
        prefix = f"第 {line_number} 行" if line_number is not None else "Journal record"
        try:
            text = line.decode("utf-8") if isinstance(line, bytes) else line
        except UnicodeDecodeError as exc:
            raise JournalCorruptionError(f"{prefix}: 不是合法 UTF-8") from exc
        if not text.endswith("\n"):
            raise JournalCorruptionError(f"{prefix}: 缺少行终止符")
        try:
            raw = json.loads(text[:-1])
        except json.JSONDecodeError as exc:
            raise JournalCorruptionError(f"{prefix}: 不是合法 JSON") from exc
        try:
            return cls._decode_value(raw)
        except JournalCorruptionError as exc:
            raise JournalCorruptionError(f"{prefix}: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalCorruptionError(f"{prefix}: 字段值无效") from exc

    @classmethod
    def _decode_value(cls, raw: object) -> JournalRecord:
        value = _object(raw, "envelope")
        _exact_fields(value, cls._ENVELOPE_FIELDS, "envelope")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise JournalCorruptionError("不支持的 schema_version")
        try:
            record_type = JournalRecordType(_string(value["record_type"], "record_type"))
        except ValueError as exc:
            raise JournalCorruptionError("未知 record_type") from exc
        session_id = _uuid(value["session_id"], "session_id")
        run_id = _uuid(value["run_id"], "run_id")
        occurred_at = _datetime(value["occurred_at"])
        payload = cls._payload_from_dict(record_type, value["payload"])
        return JournalRecord(1, record_type, session_id, run_id, occurred_at, payload)

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        normalized = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return normalized.replace("+00:00", "Z")

    @staticmethod
    def _payload_to_dict(payload: JournalPayload) -> dict[str, Any]:
        if isinstance(payload, UserMessagePayload):
            return {"message": message_to_dict(payload.message)}
        if isinstance(payload, AssistantMessagePayload):
            return {"message": message_to_dict(payload.message), "finish_reason": payload.finish_reason}
        if isinstance(payload, ToolResultPayload):
            return {"message": message_to_dict(payload.message)}
        if isinstance(payload, ContextSummaryPayload):
            summary = payload.summary
            return {
                "summary": {
                    "summary_id": str(summary.summary_id),
                    "covers_through_message_id": str(summary.covers_through_message_id),
                    "resume_from_message_id": str(summary.resume_from_message_id) if summary.resume_from_message_id else None,
                    "summary": summary.summary,
                }
            }
        return {
            "reason": payload.reason.value,
            "turn_count": payload.turn_count,
            "final_message_id": str(payload.final_message_id) if payload.final_message_id else None,
            "error": None if payload.error is None else {
                "category": payload.error.category,
                "message": payload.error.message,
            },
        }

    @staticmethod
    def _payload_from_dict(record_type: JournalRecordType, raw: object) -> JournalPayload:
        value = _object(raw, "payload")
        if record_type is JournalRecordType.USER_MESSAGE:
            _exact_fields(value, {"message"}, "user_message payload")
            return UserMessagePayload(_message(value["message"]))
        if record_type is JournalRecordType.ASSISTANT_MESSAGE:
            _exact_fields(value, {"message", "finish_reason"}, "assistant_message payload")
            finish_reason = value["finish_reason"]
            if finish_reason is not None:
                finish_reason = _string(finish_reason, "finish_reason")
            return AssistantMessagePayload(_message(value["message"]), finish_reason)
        if record_type is JournalRecordType.TOOL_RESULT:
            _exact_fields(value, {"message"}, "tool_result payload")
            return ToolResultPayload(_message(value["message"]))
        if record_type is JournalRecordType.CONTEXT_SUMMARY:
            _exact_fields(value, {"summary"}, "context_summary payload")
            summary = _object(value["summary"], "summary")
            _exact_fields(
                summary,
                {"summary_id", "covers_through_message_id", "resume_from_message_id", "summary"},
                "summary",
            )
            resume = summary["resume_from_message_id"]
            return ContextSummaryPayload(ContextSummary(
                covers_through_message_id=_uuid(summary["covers_through_message_id"], "covers_through_message_id"),
                resume_from_message_id=None if resume is None else _uuid(resume, "resume_from_message_id"),
                summary=_string(summary["summary"], "summary"),
                summary_id=_uuid(summary["summary_id"], "summary_id"),
            ))
        _exact_fields(value, {"reason", "turn_count", "final_message_id", "error"}, "run_terminated payload")
        reason = StopReason(_string(value["reason"], "reason"))
        turn_count = value["turn_count"]
        if type(turn_count) is not int or turn_count < 0:
            raise JournalCorruptionError("turn_count 必须是非负整数")
        final = value["final_message_id"]
        error_value = value["error"]
        error = None
        if error_value is not None:
            error_dict = _object(error_value, "error")
            _exact_fields(error_dict, {"category", "message"}, "error")
            error = ErrorInfo(_string(error_dict["category"], "error.category"), _string(error_dict["message"], "error.message"))
        return RunTerminatedPayload(reason, turn_count, None if final is None else _uuid(final, "final_message_id"), error)


def replay_records(records: tuple[JournalRecord, ...] | list[JournalRecord], expected_session_id: UUID) -> RecoveredSession:
    messages: list[Message] = []
    summaries: list[ContextSummary] = []
    results: list[AgentRunResult] = []
    message_ids: set[UUID] = set()
    assistant_ids: set[UUID] = set()
    assistant_ids_by_run: dict[UUID, set[UUID]] = {}
    summary_ids: set[UUID] = set()
    tool_uses: dict[str, UUID] = {}
    resolved_tool_uses: set[str] = set()
    seen_runs: set[UUID] = set()
    active_run: UUID | None = None
    last_summary_boundary = -1

    for index, record in enumerate(records, 1):
        if record.session_id != expected_session_id:
            raise JournalCorruptionError(f"第 {index} 条记录的 session_id 与目录不一致")
        payload = record.payload
        if record.record_type is JournalRecordType.USER_MESSAGE:
            if not isinstance(payload, UserMessagePayload) or payload.message.role is not Role.USER:
                raise JournalCorruptionError("user_message 的 role 或 payload 无效")
            if active_run is not None:
                raise JournalCorruptionError("同一 Session 的 AgentRun 不可交叠")
            if record.run_id in seen_runs:
                raise JournalCorruptionError("run_id 重复")
            _claim_message(payload.message, message_ids)
            active_run = record.run_id
            seen_runs.add(record.run_id)
            assistant_ids_by_run[record.run_id] = set()
            messages.append(payload.message)
            continue
        if active_run is None or record.run_id != active_run:
            raise JournalCorruptionError("记录不属于当前 AgentRun")
        if record.record_type is JournalRecordType.ASSISTANT_MESSAGE:
            if not isinstance(payload, AssistantMessagePayload) or payload.message.role is not Role.ASSISTANT:
                raise JournalCorruptionError("assistant_message 的 role 或 payload 无效")
            message = payload.message
            _claim_message(message, message_ids)
            for reference in (message.continuation_of_message_id, message.retry_of_message_id):
                if reference is not None and reference not in assistant_ids:
                    raise JournalCorruptionError("AssistantMessage 引用了未知的先前消息")
            for part in message.parts:
                if isinstance(part, ToolUsePart):
                    if part.tool_use_id in tool_uses:
                        raise JournalCorruptionError("tool_use_id 重复")
                    tool_uses[part.tool_use_id] = message.message_id
            assistant_ids.add(message.message_id)
            assistant_ids_by_run[active_run].add(message.message_id)
            messages.append(message)
        elif record.record_type is JournalRecordType.TOOL_RESULT:
            if not isinstance(payload, ToolResultPayload) or payload.message.role is not Role.TOOL:
                raise JournalCorruptionError("tool_result 的 role 或 payload 无效")
            _claim_message(payload.message, message_ids)
            for part in payload.message.parts:
                if not isinstance(part, ToolResultPart):
                    raise JournalCorruptionError("tool_result 只能包含 ToolResultPart")
                source = tool_uses.get(part.tool_use_id)
                if source is None:
                    raise JournalCorruptionError("ToolResult 引用了未知 ToolUse")
                if source != part.assistant_message_id:
                    raise JournalCorruptionError("ToolResult 的 AssistantMessage 关联不一致")
                if part.tool_use_id in resolved_tool_uses:
                    raise JournalCorruptionError("同一 ToolUse 只能有一个结果")
                resolved_tool_uses.add(part.tool_use_id)
            messages.append(payload.message)
        elif record.record_type is JournalRecordType.CONTEXT_SUMMARY:
            if not isinstance(payload, ContextSummaryPayload):
                raise JournalCorruptionError("context_summary payload 无效")
            summary = payload.summary
            if summary.summary_id in summary_ids:
                raise JournalCorruptionError("summary_id 重复")
            positions = {message.message_id: position for position, message in enumerate(messages)}
            boundary = positions.get(summary.covers_through_message_id)
            if boundary is None or (summary.resume_from_message_id is not None and summary.resume_from_message_id not in positions):
                raise JournalCorruptionError("ContextSummary 边界引用未知消息")
            if boundary <= last_summary_boundary:
                raise JournalCorruptionError("ContextSummary 覆盖边界倒退")
            last_summary_boundary = boundary
            summary_ids.add(summary.summary_id)
            summaries.append(summary)
        elif record.record_type is JournalRecordType.RUN_TERMINATED:
            if not isinstance(payload, RunTerminatedPayload):
                raise JournalCorruptionError("run_terminated payload 无效")
            if payload.final_message_id is not None and payload.final_message_id not in assistant_ids_by_run[active_run]:
                raise JournalCorruptionError("run_terminated final_message_id 不属于当前 Run")
            results.append(payload.to_result())
            active_run = None
        else:
            raise JournalCorruptionError("未知 record_type")

    # 只有物理文件末尾的 run 可以没有终态，它表示进程中断而非损坏。
    return RecoveredSession(
        session_id=expected_session_id,
        messages=tuple(messages),
        context_summaries=tuple(summaries),
        run_results=tuple(results),
        interrupted_run=active_run,
    )


def _claim_message(message: Message, known: set[UUID]) -> None:
    if message.message_id in known:
        raise JournalCorruptionError("message_id 重复")
    known.add(message.message_id)


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise JournalCorruptionError(f"{label} 必须是 JSON object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise JournalCorruptionError(f"{label} 包含未知字段: {', '.join(sorted(unknown))}")
    if missing:
        raise JournalCorruptionError(f"{label} 缺少字段: {', '.join(sorted(missing))}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise JournalCorruptionError(f"{label} 必须是字符串")
    return value


def _uuid(value: object, label: str) -> UUID:
    try:
        return UUID(_string(value, label))
    except ValueError as exc:
        raise JournalCorruptionError(f"{label} 不是合法 UUID") from exc


def _datetime(value: object) -> datetime:
    text = _string(value, "occurred_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JournalCorruptionError("occurred_at 不是合法 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JournalCorruptionError("occurred_at 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _message(raw: object) -> Message:
    value = _object(raw, "message")
    _exact_fields(
        value,
        {"message_id", "role", "parts", "continuation_of_message_id", "retry_of_message_id"},
        "message",
    )
    raw_parts = value["parts"]
    if not isinstance(raw_parts, list):
        raise JournalCorruptionError("message.parts 必须是数组")
    parts = tuple(_part(item) for item in raw_parts)
    continuation = value["continuation_of_message_id"]
    retry = value["retry_of_message_id"]
    try:
        return Message(
            message_id=_uuid(value["message_id"], "message_id"),
            role=Role(_string(value["role"], "role")),
            parts=parts,
            continuation_of_message_id=None if continuation is None else _uuid(continuation, "continuation_of_message_id"),
            retry_of_message_id=None if retry is None else _uuid(retry, "retry_of_message_id"),
        )
    except ValueError as exc:
        raise JournalCorruptionError(f"message 无效: {exc}") from exc


def _part(raw: object):
    value = _object(raw, "part")
    kind = _string(value.get("type"), "part.type")
    common = {"part_id": _uuid(value.get("part_id"), "part_id")}
    try:
        if kind == "TextPart":
            _exact_fields(value, {"type", "part_id", "content"}, "TextPart")
            return TextPart(_string(value["content"], "content"), **common)
        if kind == "ReasoningPart":
            _exact_fields(value, {"type", "part_id", "content", "source", "visibility"}, "ReasoningPart")
            return ReasoningPart(
                _string(value["content"], "content"),
                ReasoningSource(_string(value["source"], "source")),
                ReasoningVisibility(_string(value["visibility"], "visibility")),
                **common,
            )
        if kind == "ToolUsePart":
            _exact_fields(value, {"type", "part_id", "name", "arguments", "tool_use_id"}, "ToolUsePart")
            return ToolUsePart(
                _string(value["name"], "name"),
                _string(value["arguments"], "arguments"),
                _string(value["tool_use_id"], "tool_use_id"),
                **common,
            )
        if kind == "ToolResultPart":
            _exact_fields(
                value,
                {"type", "part_id", "tool_use_id", "assistant_message_id", "content", "is_error", "outcome_unknown"},
                "ToolResultPart",
            )
            is_error = value["is_error"]
            outcome_unknown = value["outcome_unknown"]
            if type(is_error) is not bool or type(outcome_unknown) is not bool:
                raise JournalCorruptionError("ToolResultPart 状态必须是布尔值")
            return ToolResultPart(
                _string(value["tool_use_id"], "tool_use_id"),
                _uuid(value["assistant_message_id"], "assistant_message_id"),
                _string(value["content"], "content"),
                is_error,
                outcome_unknown,
                **common,
            )
    except ValueError as exc:
        raise JournalCorruptionError(f"Part 无效: {exc}") from exc
    raise JournalCorruptionError(f"未知 Part 类型: {kind}")
