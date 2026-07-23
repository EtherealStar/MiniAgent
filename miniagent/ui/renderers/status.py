from __future__ import annotations

from rich.text import Text


def render_status(cwd: str, session_title: str | None, model: str | None) -> Text:
    return Text(" · ".join((cwd, session_title or "新 Session", model or "未配置模型")), style="dim")

