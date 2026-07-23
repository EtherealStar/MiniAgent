from uuid import uuid4

from rich.console import Group
from rich.text import Text

from miniagent.domain import Role, ToolUsePart
from miniagent.ui.projection import MessageLifecycle, UiMessage, UiPart
from miniagent.ui.renderers.message import render_message
from miniagent.ui.renderers.reasoning import reasoning_preview
from miniagent.ui.renderers.status import (
    SPINNER_FRAMES,
    RunState,
    render_run_state,
    render_status_left,
)
from miniagent.ui.renderers.tool import ToolPresentationRegistry


def test_reasoning_preview_uses_first_non_empty_line_and_truncates():
    assert reasoning_preview("\n  first line\nsecond") == "first line"


def test_unknown_tool_redacts_secrets_and_does_not_show_tool_id():
    use = ToolUsePart("unknown", '{"api_key":"secret", "path":"C:/private"}', "tool-secret-id")
    presentation = ToolPresentationRegistry().present(use)
    assert "secret" not in presentation.body
    assert "C:/private" not in presentation.body
    assert "tool-secret-id" not in presentation.summary + presentation.body


def _styles_of(text: Text) -> set[str]:
    """收集 Text 的基础样式与所有 span 样式名。"""
    styles = {str(text.style)} if text.style else set()
    return styles | {str(span.style) for span in text.spans}


def _assistant_message(*parts: UiPart, lifecycle=MessageLifecycle.COMPLETED) -> UiMessage:
    return UiMessage(uuid4(), Role.ASSISTANT, tuple(parts), lifecycle)


def test_assistant_label_uses_agent_style_name():
    rendered = render_message(_assistant_message(UiPart("text", "你好")))
    assert isinstance(rendered, Group)
    label = rendered.renderables[0]
    assert isinstance(label, Text)
    assert label.plain == "MiniAgent"
    assert str(label.style) == "ui.label.agent"


def test_failed_tool_part_shows_error_glyph_and_excerpt():
    part = UiPart(
        "tool",
        '{"command": "false"}',
        name="bash",
        tool_use_id="t-1",
        is_error=True,
        result="exit code 1",
    )
    rendered = render_message(_assistant_message(part))
    line = next(item for item in rendered.renderables if isinstance(item, Text) and "bash" in item.plain)
    assert "✗" in line.plain
    assert "exit code 1" in line.plain
    assert "ui.error" in _styles_of(line)


def test_pending_tool_part_shows_progress_glyph():
    part = UiPart("tool", "{}", name="read", tool_use_id="t-2")
    rendered = render_message(_assistant_message(part))
    line = next(item for item in rendered.renderables if isinstance(item, Text) and "read" in item.plain)
    assert "▸" in line.plain
    assert "ui.tool" in _styles_of(line)


def test_completed_tool_part_shows_success_glyph():
    part = UiPart("tool", "{}", name="read", tool_use_id="t-3", result="ok")
    rendered = render_message(_assistant_message(part))
    line = next(item for item in rendered.renderables if isinstance(item, Text) and "read" in item.plain)
    assert "✓" in line.plain
    assert "ui.success" in _styles_of(line)


def test_queued_user_message_shows_queued_tag():
    message = UiMessage(uuid4(), Role.USER, (UiPart("text", "稍后处理"),), MessageLifecycle.QUEUED)
    rendered = render_message(message)
    assert isinstance(rendered, Text)
    assert "排队中" in rendered.plain
    styles = _styles_of(rendered)
    assert "ui.queued" in styles
    assert "ui.queued.tag" in styles


def test_status_left_marks_model_with_agent_style():
    rendered = render_status_left("/repo", "周报", "gpt-test")
    assert "/repo · 周报 · " in rendered.plain
    assert "gpt-test" in rendered.plain
    assert "ui.label.agent" in _styles_of(rendered)


def test_status_left_has_blank_state_fallbacks():
    rendered = render_status_left("/repo", None, None)
    assert "新 Session" in rendered.plain
    assert "未配置模型" in rendered.plain


def test_run_state_none_renders_nothing():
    assert render_run_state(None).plain == ""


def test_run_state_running_shows_spinner_frame_and_label():
    rendered = render_run_state(RunState("running"), frame=0)
    assert SPINNER_FRAMES[0] in rendered.plain
    assert "运行中" in rendered.plain
    assert str(rendered.style) == "ui.tool"


def test_run_state_queued_shows_count():
    rendered = render_run_state(RunState("queued", 2))
    assert "排队 2" in rendered.plain
    assert str(rendered.style) == "ui.queued.tag"


def test_run_state_error_shows_glyph_and_label():
    rendered = render_run_state(RunState("error"))
    assert "✗ 出错" in rendered.plain
    assert str(rendered.style) == "ui.error"

