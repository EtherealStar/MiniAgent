from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models import ExecutionContext, ExecutionTraits, ToolOutput, ToolSpec, ToolTarget


class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    content: str
    status: Literal["pending", "in_progress", "completed"]

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
            raise ValueError("id must be ASCII letters, digits, '_' or '-'")
        return value

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        if not value.strip() or len(value.strip()) > 500:
            raise ValueError("content must be 1-500 characters")
        return value


class TodoWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    todos: list[TodoItem] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_list(self):
        if len({item.id for item in self.todos}) != len(self.todos):
            raise ValueError("todo ids must be unique")
        if sum(item.status == "in_progress" for item in self.todos) > 1:
            raise ValueError("at most one todo may be in_progress")
        if len(json.dumps([item.model_dump() for item in self.todos], ensure_ascii=False, separators=(",", ":")).encode()) > 32 * 1024:
            raise ValueError("todo list exceeds 32 KiB")
        return self


class TodoWriteMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    total_count: int
    pending_count: int
    in_progress_count: int
    completed_count: int


class TodoWriteData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    todos: list[TodoItem]


class TodoWriteOutput(ToolOutput):
    metadata: TodoWriteMetadata
    data: TodoWriteData


@dataclass(slots=True)
class TodoStore:
    """进程内按 Session 隔离保存 TodoList；不参与 Journal 恢复。"""
    _values: dict[str, tuple[TodoItem, ...]]

    def __init__(self) -> None:
        self._values = {}

    def get(self, session_id: str) -> tuple[TodoItem, ...]:
        return self._values.get(session_id, ())

    def replace(self, session_id: str, todos: list[TodoItem] | tuple[TodoItem, ...]) -> tuple[TodoItem, ...]:
        value = tuple(todos)
        self._values[session_id] = value
        return value


def resolve_targets(args, workspace_root):
    return (ToolTarget("session_state", "write", "todos"),)


def classify(args, targets):
    return ExecutionTraits(concurrency_safe=False)


async def handler(args: TodoWriteInput, context: ExecutionContext) -> TodoWriteOutput:
    store = context.capability("todo_store")
    todos = await asyncio.to_thread(store.replace, context.session_id, tuple(args.todos))
    counts = {status: sum(item.status == status for item in todos) for status in ("pending", "in_progress", "completed")}
    return TodoWriteOutput(
        content=f"Todo list updated: {len(todos)} total, {counts['in_progress']} in progress, {counts['pending']} pending, {counts['completed']} completed.",
        metadata=TodoWriteMetadata(total_count=len(todos), pending_count=counts["pending"], in_progress_count=counts["in_progress"], completed_count=counts["completed"]),
        data=TodoWriteData(todos=list(todos)),
    )


SPEC = ToolSpec(
    name="todo_write", description="Replace the current session's in-memory todo list with a structured task list.",
    input_model=TodoWriteInput, output_model=TodoWriteOutput, handler=handler,
    prompt_ref="miniagent.tools.todo_write.prompt:PROMPT", resolve_targets=resolve_targets,
    classify=classify, timeout_seconds=5.0,
)
todo_write_spec = SPEC
