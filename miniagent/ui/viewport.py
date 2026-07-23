from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Static

from .projection import UiProjection
from .renderers.message import render_message
from .layout_index import VirtualLayoutIndex


class MessageViewport(VerticalScroll):
    """只挂载当前投影的可见文本；Projection 仍保留完整历史事实。"""

    def refresh_projection(self, projection: UiProjection) -> None:
        was_at_bottom = self.scroll_y >= max(0, self.virtual_size.height - self.size.height - 1)
        index = VirtualLayoutIndex((item.message_id for item in projection.messages), default_height=3)
        start, end = index.visible_range(self.scroll_y, max(1, self.size.height), overscan=8)
        # 首次布局 size 可能为 0，此时先显示一个有限 overscan，避免历史消息一次性创建控件。
        if self.size.height <= 0:
            start, end = 0, min(len(projection.messages), 20)
        self.remove_children()
        for message in projection.messages[start:end]:
            self.mount(Static(render_message(message), classes=f"message-{message.lifecycle.value}"))
        if was_at_bottom or not projection.messages:
            self.call_after_refresh(self.scroll_end)
