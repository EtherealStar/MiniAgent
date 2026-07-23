from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen

from .composer import Composer
from .status_bar import StatusBar
from .viewport import MessageViewport


class ChatScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Container(MessageViewport(id="message-viewport"), id="chat")
        yield StatusBar(id="status-bar")
        yield Composer(id="composer")

