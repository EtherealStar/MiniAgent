from __future__ import annotations

import re

def compile_pattern(pattern: str) -> re.Pattern[str]:
    """编译受控 glob：通配符只能作用于单个路径段，** 必须独占一段。"""
    if not pattern or "\\" in pattern or any(part in {"", ".", ".."} for part in pattern.replace("\\", "/").split("/")):
        raise ValueError("invalid glob pattern")
    parts = pattern.replace("\\", "/").split("/")
    out = ["^"]
    for index, part in enumerate(parts):
        if part == "**":
            out.append("(?:[^/]+/)*" if index < len(parts) - 1 else ".*")
            continue
        if "**" in part or any(token in part for token in ("{", "}", "(", ")", "!")):
            raise ValueError("unsupported glob syntax")
        segment = ""
        i = 0
        while i < len(part):
            char = part[i]
            if char == "*": segment += "[^/]*"
            elif char == "?": segment += "[^/]"
            elif char == "[":
                end = part.find("]", i + 1)
                if end < 0: raise ValueError("invalid character class")
                segment += part[i:end + 1]; i = end
            else: segment += re.escape(char)
            i += 1
        out.append(segment)
        if index < len(parts) - 1: out.append("/")
    out.append("$")
    return re.compile("".join(out))
