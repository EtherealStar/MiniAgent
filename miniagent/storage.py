from __future__ import annotations

import asyncio
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from .events import SessionEvent


class TranscriptStore(Protocol):
    async def append(self, event: SessionEvent) -> None: ...


class InMemoryTranscriptStore:
    def __init__(self) -> None:
        self.events: list[SessionEvent] = []

    async def append(self, event: SessionEvent) -> None:
        self.events.append(event)


class JsonlTranscriptStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def append(self, event: SessionEvent) -> None:
        line = json.dumps(_json_value(event), ensure_ascii=False, separators=(",", ":")) + "\n"
        await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        # 单次打开追加，确保每个已确认事件对应一条完整 JSONL 记录。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(line)
            stream.flush()


def _json_value(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        result = {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
        result["type"] = type(value).__name__
        return result
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value
