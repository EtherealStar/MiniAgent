from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

from .errors import ProviderConfigurationError

REQUIRED_KEYS = ("OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY")


def normalize_chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("OPENAI_BASE_URL 必须是包含 host 的 http(s) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ProviderConfigurationError("OPENAI_BASE_URL 不得包含认证信息、query 或 fragment")
    path = parsed.path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2 and segments[-2:] == ["chat", "completions"]:
        raise ProviderConfigurationError("OPENAI_BASE_URL 应为 API 根地址，不能是完整请求地址")
    suffix = "/chat/completions" if segments and segments[-1] == "v1" else "/v1/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path + suffix, "", ""))


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    read_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        timeout = float(self.read_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProviderConfigurationError("OPENAI_TIMEOUT_SECONDS 必须是正有限数")
        object.__setattr__(self, "read_timeout_seconds", timeout)
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))
        # 构造配置时立即验证，避免请求阶段才暴露地址错误。
        normalize_chat_completions_url(self.base_url)

    @property
    def chat_completions_url(self) -> str:
        return normalize_chat_completions_url(self.base_url)


@dataclass(frozen=True, slots=True)
class Configured:
    configuration: ProviderConfiguration


@dataclass(frozen=True, slots=True)
class NotConfigured:
    missing: tuple[str, ...]


class ProviderConfigLoader:
    def load(self, environment: Mapping[str, str], dotenv_path: Path | None = None) -> Configured | NotConfigured:
        file_values: Mapping[str, str | None] = {}
        if dotenv_path is not None and dotenv_path.exists():
            file_values = dotenv_values(dotenv_path)

        def value(key: str) -> str:
            # 进程环境优先，空字符串按缺失处理。
            return (environment.get(key) or file_values.get(key) or "").strip()

        missing = tuple(key for key in REQUIRED_KEYS if not value(key))
        if missing:
            return NotConfigured(missing=missing)
        timeout_text = value("OPENAI_TIMEOUT_SECONDS") or "60"
        try:
            timeout = float(timeout_text)
        except ValueError as exc:
            raise ProviderConfigurationError("OPENAI_TIMEOUT_SECONDS 必须是数字") from exc
        return Configured(
            ProviderConfiguration(
                model=value("OPENAI_MODEL"),
                base_url=value("OPENAI_BASE_URL"),
                api_key=value("OPENAI_API_KEY"),
                read_timeout_seconds=timeout,
            )
        )
