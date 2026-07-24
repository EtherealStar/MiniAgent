from .models import (
    ArtifactRef,
    ExecutionContext,
    ExecutionErrorCode,
    ExecutionTraits,
    FieldError,
    PreToolUseOutcome,
    ResultPolicy,
    RetryPolicy,
    ToolExecutionError,
    ToolFailure,
    ToolProtocolError,
    ToolSpec,
    ToolTarget,
    ToolOutput,
)
from .registry import ToolRegistry, ToolRegistryView
from .validation import FastValidationResult, fast_validate_tool_use


def build_default_registry(*, external_tools: tuple[str, ...] = ()) -> ToolRegistry:
    names = ("grep", "glob", "calculator", "read_file", "write_file", "todo_write") + external_tools
    registry = ToolRegistry(available_names=names)
    registry.freeze()
    return registry

__all__ = [
    "ArtifactRef", "ExecutionContext", "ExecutionErrorCode", "ExecutionTraits", "FieldError", "ResultPolicy",
    "PreToolUseOutcome", "RetryPolicy", "ToolExecutionError", "ToolFailure", "ToolProtocolError", "ToolRegistry",
    "ToolRegistryView", "ToolSpec", "ToolTarget", "ToolOutput", "build_default_registry", "FastValidationResult", "fast_validate_tool_use",
]
