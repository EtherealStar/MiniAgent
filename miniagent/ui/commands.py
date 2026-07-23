from __future__ import annotations

from enum import StrEnum


class CommandName(StrEnum):
    MODEL = "/model"
    SESSION = "/session"
    CLEAR = "/clear"
    QUIT = "/quit"


def parse_command(text: str) -> CommandName | None:
    """只把首 token 的完整匹配识别为命令。"""
    tokens = text.strip().split(maxsplit=1)
    if not tokens:
        return None
    try:
        return CommandName(tokens[0])
    except ValueError:
        return None


def complete_command(text: str) -> str | None:
    value = text.strip()
    matches = [command.value for command in CommandName if command.value.startswith(value)]
    return matches[0] if value.startswith("/") and len(matches) == 1 else None

