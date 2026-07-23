from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList
from textual.widgets._option_list import Option

from ...repository import SessionSummary


class SessionPickerModal(ModalScreen[str | None]):
    def __init__(self, sessions: Iterable[SessionSummary]) -> None:
        super().__init__()
        self.sessions = tuple(sessions)

    def compose(self) -> ComposeResult:
        with Vertical(id="session-picker"):
            yield Label("选择 Session")
            yield OptionList(*[
                Option(f"{item.name} ({item.session_id})", id=item.session_id, disabled=not item.openable)
                for item in self.sessions
            ], id="sessions")
            yield Button("新建 Session", id="new")
            yield Button("取消", id="cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("__new__" if event.button.id == "new" else None)
