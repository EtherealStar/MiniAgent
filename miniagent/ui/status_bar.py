from __future__ import annotations

from textual.containers import Horizontal
from textual.widgets import Static

from .renderers.status import RunState, render_run_state, render_status_left


class StatusBar(Horizontal):
    """左段显示 cwd · 会话 · 模型；右段显示运行态，运行中时轮转 braille 帧。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._run_state: RunState | None = None
        self._frame = 0
        self._timer = None

    def compose(self):
        yield Static(id="status-left")
        yield Static(id="status-right")

    def update_status(self, cwd: str, session_title: str | None, model: str | None) -> None:
        self.query_one("#status-left", Static).update(render_status_left(cwd, session_title, model))

    def update_run_state(self, state: RunState | None) -> None:
        if state == self._run_state:
            return
        self._run_state = state
        self._render_state()
        running = state is not None and state.kind == "running"
        if running and self._timer is None:
            self._timer = self.set_interval(1 / 8, self._tick)
        elif not running and self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame += 1
        self._render_state()

    def _render_state(self) -> None:
        self.query_one("#status-right", Static).update(render_run_state(self._run_state, self._frame))
