from __future__ import annotations

from rich.text import Text

from ..projection import UiMessage
from .reasoning import reasoning_preview
from .tool import redact_sensitive


def render_message(message: UiMessage, *, reasoning_expanded: bool = False) -> Text:
    output = Text()
    output.append("You\n" if message.role.value == "user" else "MiniAgent\n", style="bold")
    for part in message.parts:
        if part.kind == "reasoning":
            content = part.content if reasoning_expanded else "▸ " + reasoning_preview(part.content)
            output.append(content + "\n", style="dim italic")
        elif part.kind == "tool":
            output.append(f"▸ {part.name or '工具'}\n", style="cyan")
        elif part.kind == "tool_result":
            # ToolResult 没有可展示的关联 ID；结果正文仍必须经过统一脱敏。
            safe = redact_sensitive(part.content, key="tool_result")
            output.append("工具结果：" + str(safe) + "\n", style="dim")
        else:
            output.append(part.content + "\n")
    if message.lifecycle.value == "queued":
        output.append("queued\n", style="dim")
    elif message.lifecycle.value == "failed":
        output.append("未完成\n", style="red")
    return output
