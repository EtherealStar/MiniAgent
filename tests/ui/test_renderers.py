from miniagent.domain import ToolUsePart
from miniagent.ui.renderers.reasoning import reasoning_preview
from miniagent.ui.renderers.tool import ToolPresentationRegistry


def test_reasoning_preview_uses_first_non_empty_line_and_truncates():
    assert reasoning_preview("\n  first line\nsecond") == "first line"


def test_unknown_tool_redacts_secrets_and_does_not_show_tool_id():
    use = ToolUsePart("unknown", '{"api_key":"secret", "path":"C:/private"}', "tool-secret-id")
    presentation = ToolPresentationRegistry().present(use)
    assert "secret" not in presentation.body
    assert "C:/private" not in presentation.body
    assert "tool-secret-id" not in presentation.summary + presentation.body

