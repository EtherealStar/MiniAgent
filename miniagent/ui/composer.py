from __future__ import annotations

from textual.message import Message
from textual.widgets import TextArea
from textual.binding import Binding


class Composer(TextArea):
    BINDINGS = [Binding("enter", "submit_text", "提交", show=False)]

    def __init__(self, *args, **kwargs) -> None:
        # 聊天输入不需要代码编辑器的行号槽。
        kwargs.setdefault("show_line_numbers", False)
        super().__init__(*args, **kwargs)
    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CancelRequested(Message):
        pass

    def on_key(self, event) -> None:
        if event.key == "enter":
            self.action_submit_text()
            event.stop()
        elif event.key in {"ctrl+c", "escape"}:
            event.prevent_default()
            self.post_message(self.CancelRequested())

    def action_submit_text(self) -> None:
        text = self.text
        if text.strip():
            self.post_message(self.Submitted(text))
            self.clear()
