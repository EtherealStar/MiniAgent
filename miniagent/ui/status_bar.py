from __future__ import annotations

from textual.widgets import Static

from .renderers.status import render_status


class StatusBar(Static):
    def update_status(self, cwd: str, session_title: str | None, model: str | None) -> None:
        self.update(render_status(cwd, session_title, model))

