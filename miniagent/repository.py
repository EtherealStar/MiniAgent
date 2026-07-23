from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable
from uuid import UUID

from .domain import Role, TextPart
from .journal import (
    JournalCodec,
    JournalCorruptionError,
    JournalRecord,
    JournalRecordType,
    RecoveredSession,
    UserMessagePayload,
    replay_records,
)


class SessionOpenError(RuntimeError):
    pass


class SessionLockedError(SessionOpenError):
    pass


class SessionCorruptError(SessionOpenError):
    pass


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    name: str
    created_at: datetime | None
    last_user_input_at: datetime | None
    openable: bool
    error_category: str | None = None


_OWNERS: set[str] = set()
_OWNERS_GUARD = threading.Lock()


class _WriterLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._key = os.path.normcase(str(path.resolve()))
        self._stream: BinaryIO | None = None
        self._closed = False

    def acquire(self) -> None:
        with _OWNERS_GUARD:
            if self._key in _OWNERS:
                raise SessionLockedError("Session 正在使用")
            _OWNERS.add(self._key)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a+b")
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SessionLockedError("Session 正在使用") from exc
        except Exception:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            with _OWNERS_GUARD:
                _OWNERS.discard(self._key)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stream = self._stream
        self._stream = None
        try:
            if stream is not None:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            with _OWNERS_GUARD:
                _OWNERS.discard(self._key)


class OpenSession:
    def __init__(
        self,
        session_id: UUID,
        journal_path: Path,
        lock: _WriterLock,
        stream: BinaryIO,
        records: list[JournalRecord],
        sync_file: Callable[[int], None],
    ) -> None:
        self.session_id = session_id
        self.journal_path = journal_path
        self._writer_lock = lock
        self._stream = stream
        self._records = records
        self._sync_file = sync_file
        self._append_lock = asyncio.Lock()
        self._closed = False
        self._poisoned = False
        self.recovered = replay_records(records, session_id)

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        return tuple(self._records)

    async def append(self, record: JournalRecord) -> None:
        async with self._append_lock:
            if self._closed:
                raise SessionOpenError("Session 已关闭")
            if self._poisoned:
                raise SessionOpenError("Journal writer 已失效，必须关闭并重新打开")
            # 先用完整重放规则预演，磁盘失败时内存投影仍保持原状。
            candidate = [*self._records, record]
            try:
                recovered = replay_records(candidate, self.session_id)
                encoded = JournalCodec.encode(record)
            except (JournalCorruptionError, ValueError) as exc:
                raise SessionCorruptError(str(exc)) from exc
            try:
                await asyncio.to_thread(self._append_bytes, encoded)
            except Exception:
                self._poisoned = True
                raise
            self._records = candidate
            self.recovered = recovered

    def _append_bytes(self, encoded: bytes) -> None:
        written = self._stream.write(encoded)
        if written != len(encoded):
            raise OSError("Journal 未完整写入")
        self._stream.flush()
        self._sync_file(self._stream.fileno())

    async def close(self) -> None:
        async with self._append_lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        try:
            self._stream.close()
        finally:
            self._writer_lock.close()


class SessionRepository:
    def __init__(self, sessions_root: Path, *, sync_file: Callable[[int], None] = os.fsync) -> None:
        self.sessions_root = Path(sessions_root)
        self._sync_file = sync_file

    async def create_session(self, session_id: UUID, first_user_record: JournalRecord) -> OpenSession:
        return await asyncio.to_thread(self._create_session, session_id, first_user_record)

    def _create_session(self, session_id: UUID, first: JournalRecord) -> OpenSession:
        if (
            first.session_id != session_id
            or first.record_type is not JournalRecordType.USER_MESSAGE
            or not isinstance(first.payload, UserMessagePayload)
            or first.payload.message.role is not Role.USER
        ):
            raise SessionCorruptError("首条记录必须是当前 Session 的 user_message")
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        directory = self.sessions_root / str(session_id)
        try:
            directory.mkdir()
        except FileExistsError as exc:
            raise SessionOpenError("Session 已存在") from exc
        lock = _WriterLock(directory / "writer.lock")
        stream: BinaryIO | None = None
        try:
            lock.acquire()
            journal = directory / "message.jsonl"
            stream = journal.open("x+b")
            opened = OpenSession(session_id, journal, lock, stream, [], self._sync_file)
            encoded = JournalCodec.encode(first)
            opened._append_bytes(encoded)
            opened._records = [first]
            opened.recovered = replay_records([first], session_id)
            return opened
        except Exception:
            if stream is not None:
                stream.close()
            lock.close()
            self._remove_failed_creation(directory)
            raise

    @staticmethod
    def _remove_failed_creation(directory: Path) -> None:
        # 目录由本次 create 独占创建；首条 fsync 未确认时整个 Session 都不可见。
        try:
            for name in ("message.jsonl", "writer.lock"):
                path = directory / name
                if path.exists():
                    path.unlink()
            directory.rmdir()
        except OSError:
            pass

    async def open_session(self, session_id: UUID) -> OpenSession:
        return await asyncio.to_thread(self._open_session, session_id)

    def _open_session(self, session_id: UUID) -> OpenSession:
        directory = self.sessions_root / str(session_id)
        if not directory.is_dir():
            raise SessionOpenError("Session 不存在")
        lock = _WriterLock(directory / "writer.lock")
        stream: BinaryIO | None = None
        try:
            lock.acquire()
            journal = directory / "message.jsonl"
            records = self._read_records(journal, repair_tail=True)
            if not records or records[0].record_type is not JournalRecordType.USER_MESSAGE:
                raise SessionCorruptError("Session 缺少首条 user_message")
            recovered = replay_records(records, session_id)
            stream = journal.open("ab", buffering=0)
            opened = OpenSession(session_id, journal, lock, stream, records, self._sync_file)
            opened.recovered = recovered
            return opened
        except SessionLockedError:
            raise
        except (JournalCorruptionError, UnicodeError, OSError) as exc:
            if stream is not None:
                stream.close()
            lock.close()
            if isinstance(exc, SessionCorruptError):
                raise
            raise SessionCorruptError(str(exc)) from exc
        except Exception:
            if stream is not None:
                stream.close()
            lock.close()
            raise

    def _read_records(self, path: Path, *, repair_tail: bool) -> list[JournalRecord]:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SessionCorruptError("无法读取 message.jsonl") from exc
        complete_length = len(data) if data.endswith(b"\n") else data.rfind(b"\n") + 1
        complete = data[:complete_length]
        if repair_tail and complete_length != len(data):
            with path.open("r+b") as stream:
                stream.truncate(complete_length)
                stream.flush()
                self._sync_file(stream.fileno())
        records: list[JournalRecord] = []
        for line_number, line in enumerate(complete.splitlines(keepends=True), 1):
            if line in {b"\n", b"\r\n"}:
                raise JournalCorruptionError(f"第 {line_number} 行为空")
            records.append(JournalCodec.decode(line, line_number=line_number))
        return records

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        return await asyncio.to_thread(self._list_sessions)

    def _list_sessions(self) -> tuple[SessionSummary, ...]:
        if not self.sessions_root.is_dir():
            return ()
        summaries: list[SessionSummary] = []
        for directory in self.sessions_root.iterdir():
            if not directory.is_dir():
                continue
            try:
                session_id = UUID(directory.name)
                records = self._read_records(directory / "message.jsonl", repair_tail=False)
                if not records:
                    continue
                recovered = replay_records(records, session_id)
                user_records = [
                    item for item in records
                    if item.record_type is JournalRecordType.USER_MESSAGE and isinstance(item.payload, UserMessagePayload)
                ]
                if not user_records:
                    raise JournalCorruptionError("缺少 user_message")
                first = user_records[0]
                name = self._session_name(first.payload.message)
                summaries.append(SessionSummary(
                    str(session_id),
                    name,
                    first.occurred_at,
                    user_records[-1].occurred_at,
                    True,
                ))
            except (ValueError, OSError, JournalCorruptionError, SessionCorruptError):
                summaries.append(SessionSummary(directory.name, directory.name, None, None, False, "corrupt_journal"))
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        # 先固定 session_id 次序，再用稳定排序按时间倒序，消除文件系统枚举差异。
        summaries.sort(key=lambda item: item.session_id)
        summaries.sort(key=lambda item: item.last_user_input_at or minimum, reverse=True)
        return tuple(summaries)

    @staticmethod
    def _session_name(message) -> str:
        text = " ".join(
            part.content for part in message.parts if isinstance(part, TextPart)
        )
        normalized = " ".join(text.split())
        return normalized[:48] or "Untitled session"
