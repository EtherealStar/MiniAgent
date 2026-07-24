from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


@dataclass(frozen=True, slots=True)
class ExternalToolConfiguration:
    tavily_api_key: str | None = field(default=None, repr=False)
    mineru_api_token: str | None = field(default=None, repr=False)


class ExternalToolConfigLoader:
    def load(self, environment: Mapping[str, str], dotenv_path: Path | None = None) -> ExternalToolConfiguration:
        file_values: Mapping[str, str | None] = {}
        if dotenv_path is not None and dotenv_path.exists():
            file_values = dotenv_values(dotenv_path)

        def optional_secret(key: str) -> str | None:
            # 环境变量优先；空白值按未配置处理，避免注册不可用工具。
            value = (environment.get(key) or file_values.get(key) or "").strip()
            return value or None

        return ExternalToolConfiguration(
            tavily_api_key=optional_secret("TAVILY_API_KEY"),
            mineru_api_token=optional_secret("MINERU_API_TOKEN"),
        )
