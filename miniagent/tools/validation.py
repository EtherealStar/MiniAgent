from __future__ import annotations

import json
from dataclasses import dataclass

from .models import FieldError, ToolSpec
from ..domain import ToolUsePart


@dataclass(frozen=True, slots=True)
class FastValidationResult:
    valid: bool
    code: str = ""
    message: str = ""
    field_errors: tuple[FieldError, ...] = ()


def fast_validate_tool_use(use: ToolUsePart, spec: ToolSpec | None) -> FastValidationResult:
    if spec is None:
        return FastValidationResult(True)
    try:
        raw = json.loads(use.arguments)
    except (json.JSONDecodeError, TypeError):
        return FastValidationResult(False, "malformed_arguments", "arguments 不是合法 JSON object")
    if not isinstance(raw, dict):
        return FastValidationResult(False, "malformed_arguments", "arguments 顶层必须是 object")
    marker = raw.pop("correction_of_tool_use_id", ...)
    parameters = spec.function_schema or {}
    properties = set(parameters.get("function", {}).get("parameters", {}).get("properties", {}))
    required = set(parameters.get("function", {}).get("parameters", {}).get("required", []))
    business = required - {"correction_of_tool_use_id"}
    missing = business - set(raw)
    extra = set(raw) - business
    if marker is ...:
        missing.add("correction_of_tool_use_id")
    if missing or extra:
        errors = tuple(
            [FieldError(name, "缺少必填字段") for name in sorted(missing)]
            + [FieldError(name, "不允许的额外字段") for name in sorted(extra)]
        )
        return FastValidationResult(False, "invalid_arguments", "参数字段不符合 strict schema", errors)
    if marker is not None and (not isinstance(marker, str) or not marker):
        return FastValidationResult(False, "correction_not_allowed", "修正引用必须是非空字符串")
    return FastValidationResult(True)
