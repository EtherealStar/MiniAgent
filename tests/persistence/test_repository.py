from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from miniagent.domain import Message, Role
from miniagent.journal import (
    JournalCodec,
    JournalRecord,
    JournalRecordType,
    UserMessagePayload,
)
from miniagent.repository import SessionLockedError, SessionRepository


def user_record(session_id, run_id, text, *, seconds=0):
    return JournalRecord(
        1,
        JournalRecordType.USER_MESSAGE,
        session_id,
        run_id,
        datetime(2026, 7, 23, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        UserMessagePayload(Message.text(Role.USER, text)),
    )


async def test_create_close_and_open_restore_the_first_message(tmp_path):
    repository = SessionRepository(tmp_path)
    session_id = uuid4()
    first = user_record(session_id, uuid4(), "  first   session  ")

    created = await repository.create_session(session_id, first)
    assert created.recovered.messages == (first.payload.message,)
    await created.close()

    opened = await repository.open_session(session_id)
    assert opened.recovered.messages == (first.payload.message,)
    await opened.close()


async def test_second_writer_is_rejected_until_the_first_closes(tmp_path):
    repository = SessionRepository(tmp_path)
    session_id = uuid4()
    first = await repository.create_session(session_id, user_record(session_id, uuid4(), "locked"))

    with pytest.raises(SessionLockedError, match="正在使用"):
        await SessionRepository(tmp_path).open_session(session_id)

    await first.close()
    reopened = await SessionRepository(tmp_path).open_session(session_id)
    await reopened.close()


async def test_open_truncates_only_the_uncommitted_tail(tmp_path):
    repository = SessionRepository(tmp_path)
    session_id = uuid4()
    opened = await repository.create_session(session_id, user_record(session_id, uuid4(), "tail"))
    await opened.close()
    journal = tmp_path / str(session_id) / "message.jsonl"
    committed = journal.read_bytes()
    with journal.open("ab") as stream:
        stream.write(b'{"half":')

    recovered = await repository.open_session(session_id)
    await recovered.close()

    assert journal.read_bytes() == committed


async def test_list_sessions_is_sorted_and_isolates_corruption(tmp_path):
    repository = SessionRepository(tmp_path)
    older_id, newer_id = uuid4(), uuid4()
    older = await repository.create_session(older_id, user_record(older_id, uuid4(), "older", seconds=1))
    await older.close()
    newer = await repository.create_session(newer_id, user_record(newer_id, uuid4(), "newer", seconds=2))
    await newer.close()
    bad_id = uuid4()
    bad_dir = tmp_path / str(bad_id)
    bad_dir.mkdir()
    (bad_dir / "message.jsonl").write_bytes(b"{}\n")

    summaries = await repository.list_sessions()

    assert [item.session_id for item in summaries] == [str(newer_id), str(older_id), str(bad_id)]
    assert summaries[0].name == "newer"
    assert summaries[-1].openable is False
    assert summaries[-1].error_category == "corrupt_journal"


async def test_list_sessions_uses_session_id_as_stable_timestamp_tie_break(tmp_path):
    repository = SessionRepository(tmp_path)
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    first = await repository.create_session(first_id, user_record(first_id, uuid4(), "first"))
    await first.close()
    second = await repository.create_session(second_id, user_record(second_id, uuid4(), "second"))
    await second.close()

    summaries = await repository.list_sessions()

    assert [summary.session_id for summary in summaries] == [str(first_id), str(second_id)]
