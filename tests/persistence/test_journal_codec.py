from datetime import datetime, timezone
from uuid import UUID

import pytest

from miniagent.domain import (
    AgentRunResult,
    Message,
    Role,
    StopReason,
    TextPart,
    ToolResultPart,
    ToolUsePart,
)
from miniagent.journal import (
    AssistantMessagePayload,
    JournalCodec,
    JournalCorruptionError,
    JournalRecord,
    JournalRecordType,
    RunTerminatedPayload,
    ToolResultPayload,
    UserMessagePayload,
    replay_records,
)


SESSION_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = UUID("00000000-0000-0000-0000-000000000002")
OCCURRED_AT = datetime(2026, 7, 23, tzinfo=timezone.utc)


def record(record_type, payload):
    return JournalRecord(1, record_type, SESSION_ID, RUN_ID, OCCURRED_AT, payload)


def test_codec_is_deterministic_and_rejects_unknown_fields():
    user = Message(
        role=Role.USER,
        parts=(TextPart("你好", part_id=UUID("00000000-0000-0000-0000-000000000011")),),
        message_id=UUID("00000000-0000-0000-0000-000000000010"),
    )
    encoded = JournalCodec.encode(record(JournalRecordType.USER_MESSAGE, UserMessagePayload(user)))

    assert encoded == JournalCodec.encode(JournalCodec.decode(encoded, line_number=1))
    assert encoded.endswith(b"\n")
    assert b'"event_id"' not in encoded and b'"journal_sequence"' not in encoded

    malformed = encoded[:-2] + b',"unexpected":true}\n'
    with pytest.raises(JournalCorruptionError, match="第 3 行.*未知字段"):
        JournalCodec.decode(malformed, line_number=3)


def test_replay_restores_complete_transcript_and_terminal():
    user = Message.text(Role.USER, "run")
    tool_use = ToolUsePart("read", '{"path":"a"}', "call-1")
    first_assistant = Message(role=Role.ASSISTANT, parts=(tool_use,))
    tool_result = Message(
        role=Role.TOOL,
        parts=(ToolResultPart("call-1", first_assistant.message_id, "ok"),),
    )
    final = Message.text(Role.ASSISTANT, "done")
    records = (
        record(JournalRecordType.USER_MESSAGE, UserMessagePayload(user)),
        record(JournalRecordType.ASSISTANT_MESSAGE, AssistantMessagePayload(first_assistant, "tool_calls")),
        record(JournalRecordType.TOOL_RESULT, ToolResultPayload(tool_result)),
        record(JournalRecordType.ASSISTANT_MESSAGE, AssistantMessagePayload(final, "stop")),
        record(
            JournalRecordType.RUN_TERMINATED,
            RunTerminatedPayload.from_result(
                AgentRunResult(StopReason.COMPLETED, 2, final.message_id)
            ),
        ),
    )

    recovered = replay_records(records, SESSION_ID)

    assert recovered.messages == (user, first_assistant, tool_result, final)
    assert recovered.interrupted_run is None
    assert recovered.run_results == (
        AgentRunResult(StopReason.COMPLETED, 2, final.message_id),
    )


def test_replay_rejects_unknown_tool_result_and_overlapping_runs():
    user = Message.text(Role.USER, "run")
    unknown_result = Message(
        role=Role.TOOL,
        parts=(ToolResultPart("missing", UUID(int=99), "bad"),),
    )
    with pytest.raises(JournalCorruptionError, match="未知 ToolUse"):
        replay_records(
            (
                record(JournalRecordType.USER_MESSAGE, UserMessagePayload(user)),
                record(JournalRecordType.TOOL_RESULT, ToolResultPayload(unknown_result)),
            ),
            SESSION_ID,
        )

    other_run = UUID("00000000-0000-0000-0000-000000000003")
    second = JournalRecord(
        1,
        JournalRecordType.USER_MESSAGE,
        SESSION_ID,
        other_run,
        OCCURRED_AT,
        UserMessagePayload(Message.text(Role.USER, "overlap")),
    )
    with pytest.raises(JournalCorruptionError, match="不可交叠"):
        replay_records(
            (record(JournalRecordType.USER_MESSAGE, UserMessagePayload(user)), second),
            SESSION_ID,
        )
