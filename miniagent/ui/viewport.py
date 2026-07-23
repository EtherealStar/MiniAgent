from __future__ import annotations

from textual.css.query import NoMatches
from textual.containers import VerticalScroll
from textual.widgets import Static

from .projection import UiProjection
from .render_cache import MarkdownBlockCache
from .renderers.message import MarkdownCaches, render_message
from .layout_index import VirtualLayoutIndex


class NewContentButton(Static):
    """离开底部时出现在右下角的返回底部入口。"""

    def on_click(self) -> None:
        if self.parent is None:
            return
        try:
            self.parent.query_one(MessageViewport).jump_to_latest()
        except NoMatches:
            pass


class MessageViewport(VerticalScroll):
    """只挂载当前投影的可见文本；Projection 仍保留完整历史事实。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._md_caches: MarkdownCaches = {}
        self._message_count = 0

    def refresh_projection(self, projection: UiProjection) -> None:
        was_at_bottom = self.scroll_y >= max(0, self.virtual_size.height - self.size.height - 1)
        index = VirtualLayoutIndex((item.message_id for item in projection.messages), default_height=3)
        start, end = index.visible_range(self.scroll_y, max(1, self.size.height), overscan=8)
        # 首次布局 size 可能为 0，此时先显示一个有限 overscan，避免历史消息一次性创建控件。
        if self.size.height <= 0:
            start, end = 0, min(len(projection.messages), 20)
        self.remove_children()
        for message in projection.messages[start:end]:
            renderable = render_message(message, md_caches=self._md_caches)
            self.mount(Static(renderable, classes=f"message message-{message.lifecycle.value}"))
        self._message_count = len(projection.messages)
        self._prune_caches(projection)
        if was_at_bottom or not projection.messages:
            self.call_after_refresh(self.scroll_end, animate=False)
        self._sync_new_content_button()

    def jump_to_latest(self) -> None:
        self.scroll_end(animate=False)
        self._sync_new_content_button()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._sync_new_content_button()

    def on_resize(self) -> None:
        # 终端宽度变化会改变父容器与按钮的相对几何，需要重算绝对定位偏移。
        self._reposition_new_content_button()

    def _prune_caches(self, projection: UiProjection) -> None:
        live = {message.message_id for message in projection.messages}
        for key in [key for key in self._md_caches if key[0] not in live]:
            del self._md_caches[key]

    def _sync_new_content_button(self) -> None:
        if self.parent is None:
            return
        try:
            button = self.parent.query_one(NewContentButton)
        except NoMatches:
            return
        at_bottom = self.scroll_y >= max(0, self.virtual_size.height - self.size.height - 1)
        button.display = bool(self._message_count) and not at_bottom
        if button.display:
            # 刚显示的按钮还没有可靠尺寸，等一次刷新后再换算偏移。
            self.call_after_refresh(self._reposition_new_content_button)

    def _reposition_new_content_button(self) -> None:
        """把按钮换算到父容器右下角（距右 2、距底 1）。

        Textual 的绝对定位以父容器左上角为原点叠加 offset，CSS 无法表达
        "靠右/靠底"，因此在这里按实际尺寸计算。
        """
        if self.parent is None:
            return
        try:
            button = self.parent.query_one(NewContentButton)
        except NoMatches:
            return
        x = max(0, self.parent.size.width - button.outer_size.width - 2)
        y = max(0, self.parent.size.height - button.outer_size.height - 1)
        button.styles.offset = (x, y)
