from __future__ import annotations

from .models import ContinueToolUse, PreToolUseContext, RejectToolUse
from ..tools.validation import fast_validate_tool_use


class FastToolValidationHook:
    async def __call__(self, context: PreToolUseContext):
        result = fast_validate_tool_use(context.tool_use, context.tool_spec)
        if result.valid:
            return ContinueToolUse()
        return RejectToolUse(
            code=result.code,
            message=result.message,
            field_errors=tuple((error.path, error.message) for error in result.field_errors),
        )
