from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList


class ModelPickerModal(ModalScreen[str | None]):
    def __init__(self, models: Iterable[str], *, error: str | None = None) -> None:
        super().__init__()
        self.models = tuple(models)
        self.error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker"):
            yield Label(self.error or "选择模型")
            yield OptionList(*self.models, id="models")
            yield Button("取消", id="cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

