from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from ...domain import ToolResultPart, ToolUsePart


_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization|credential)", re.I)
_PATH_KEY = re.compile(r"(^|[_-])(path|cwd|home|file)([_-]|$)", re.I)
_INLINE_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*([^,\s]+)")


def redact_sensitive(value: object, *, key: str = "") -> object:
    if _SECRET_KEY.search(key) or _PATH_KEY.search(key):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        value = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[已隐藏]", value)
        # 工具输出中的绝对路径不具备用户可读摘要价值，避免泄露本地目录。
        value = re.sub(r"(?i)([A-Z]:\\|/)(?:[^\s,;]+)", "[路径已隐藏]", value)
        return value[:2000] + ("…" if len(value) > 2000 else "")
    return value


@dataclass(frozen=True, slots=True)
class ToolPresentation:
    summary: str
    body: str
    is_error: bool = False


class ToolPresenter(Protocol):
    def __call__(self, use: ToolUsePart, result: ToolResultPart | None) -> ToolPresentation: ...


class ToolPresentationRegistry:
    def __init__(self) -> None:
        self._presenters: dict[str, ToolPresenter] = {}

    def register(self, name: str, presenter: ToolPresenter) -> None:
        self._presenters[name] = presenter

    def present(self, use: ToolUsePart, result: ToolResultPart | None = None) -> ToolPresentation:
        presenter = self._presenters.get(use.name)
        if presenter is not None:
            return presenter(use, result)
        try:
            arguments = json.loads(use.arguments)
            safe_arguments = redact_sensitive(arguments)
            body = json.dumps(safe_arguments, ensure_ascii=False, indent=2)[:2000]
        except (TypeError, ValueError):
            body = "参数不可显示"
        summary = f"正在使用工具：{use.name}"
        if result is not None:
            summary = f"工具已完成：{use.name}" if not result.is_error else f"工具失败：{use.name}"
            body = redact_sensitive(result.content, key="result")
        return ToolPresentation(summary, str(body), bool(result and result.is_error))
