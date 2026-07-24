from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from miniagent.domain import ToolExecutionBatch, ToolUsePart
from miniagent.ports import Cancellation
from miniagent.tools import build_default_registry
from miniagent.tools.artifacts import MemoryTraceSink
from miniagent.tools.executor import ToolExecutor


def arguments(**overrides):
    values = {
        "pattern": "needle",
        "path": ".",
        "include": None,
        "case_sensitive": True,
        "max_matches": 100,
        "correction_of_tool_use_id": None,
    }
    values.update(overrides)
    return json.dumps(values)


async def execute(tmp_path, raw, call_id="call-1"):
    registry = build_default_registry()
    executor = ToolExecutor(registry.enabled_view(), tmp_path, "session", trace_sink=MemoryTraceSink())
    batch = ToolExecutionBatch(uuid4(), uuid4(), (ToolUsePart("grep", raw, call_id),))
    return (await executor.submit_batch(batch, Cancellation()))[0]


@pytest.mark.parametrize("path", ["../outside", "missing", "C:\\Windows"])
async def test_invalid_paths_are_structured_failures(tmp_path, path):
    result = await execute(tmp_path, arguments(path=path))
    assert result.is_error and result.failure.code in {"invalid_arguments", "target_denied"}
    assert result.attempts == 0


async def test_symlink_escape_is_rejected_when_supported(tmp_path):
    outside = tmp_path.parent / f"outside-{uuid4().hex}.txt"
    outside.write_text("needle", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("当前 Windows 权限不允许创建符号链接")
    result = await execute(tmp_path, arguments(path="link.txt"))
    assert result.is_error and result.failure.code == "target_denied"


async def test_result_over_20_kib_is_externalized_with_preview(tmp_path):
    lines = [f"needle-{index}-" + "x" * 480 for index in range(60)]
    (tmp_path / "large.txt").write_text("\n".join(lines), encoding="utf-8")
    result = await execute(tmp_path, arguments(max_matches=100))
    assert result.artifact is not None and result.artifact.byte_count > 20 * 1024
    assert "完整内容已外置" in result.content and result.artifact.sha256 in result.content
    full = (tmp_path / result.artifact.path).read_text(encoding="utf-8")
    assert "large.txt:60:needle-59" in full
