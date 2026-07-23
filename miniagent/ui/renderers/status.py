"""状态栏渲染：左段上下文（cwd · 会话 · 模型）与右段运行态。"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

SPINNER_FRAMES = "⠋⠙⠹⠸⠼ⴇ⦧⦏⦆⦃"


@dataclass(frozen=True, slots=True)
class RunState:
    """状态栏右段的运行态。kind: running / queued / error。"""

    kind: str
    count: int = 0


def render_status_left(cwd: str, session_title: str | None, model: str | None) -> Text:
    output = Text(style="ui.meta")
    output.append(cwd)
    output.append(" · ")
    output.append(session_title or "新 Session")
    output.append(" · ")
    output.append(model or "未配置模型", style="ui.label.agent")
    return output


def render_run_state(state: RunState | None, frame: int = 0) -> Text:
    if state is None:
        return Text("")
    if state.kind == "running":
        spinner = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
        return Text(f"{spinner} 运行中", style="ui.tool")
    if state.kind == "queued":
        return Text(f"排队 {state.count}", style="ui.queued.tag")
    return Text("✗ 出错", style="ui.error")
