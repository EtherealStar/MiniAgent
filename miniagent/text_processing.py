from __future__ import annotations

from collections.abc import Iterable

from .provider.events import ModelEvent, ReasoningDelta, TextDelta


class PassthroughTextProcessor:
    def feed(self, event: ModelEvent) -> Iterable[ModelEvent]:
        yield event

    def finish(self) -> Iterable[ModelEvent]:
        return ()


class ThinkTagTextProcessor:
    """跨 chunk 识别 think 标签；未闭合内容在流结束时仍作为 reasoning 输出。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._reasoning = False

    def feed(self, event: ModelEvent) -> Iterable[ModelEvent]:
        if not isinstance(event, TextDelta):
            yield event
            return
        self._buffer += event.content
        while self._buffer:
            marker = "</think>" if self._reasoning else "<think>"
            index = self._buffer.find(marker)
            if index >= 0:
                content = self._buffer[:index]
                if content:
                    yield ReasoningDelta(content) if self._reasoning else TextDelta(content)
                self._buffer = self._buffer[index + len(marker):]
                self._reasoning = not self._reasoning
                continue
            # 保留可能属于跨 chunk 标签的最长后缀。
            keep = max((size for size in range(1, len(marker)) if self._buffer.endswith(marker[:size])), default=0)
            content = self._buffer[:-keep] if keep else self._buffer
            self._buffer = self._buffer[-keep:] if keep else ""
            if content:
                yield ReasoningDelta(content) if self._reasoning else TextDelta(content)
            break

    def finish(self) -> Iterable[ModelEvent]:
        if self._buffer:
            event = ReasoningDelta(self._buffer) if self._reasoning else TextDelta(self._buffer)
            self._buffer = ""
            return (event,)
        return ()
