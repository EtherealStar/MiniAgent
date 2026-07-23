from .models import (
    ArtifactRef,
    ExecutionContext,
    ExecutionTraits,
    FieldError,
    ResultPolicy,
    RetryPolicy,
    ToolExecutionError,
    ToolFailure,
    ToolProtocolError,
    ToolSpec,
    ToolTarget,
)
from .registry import ToolRegistry, ToolRegistryView
from .validation import FastValidationResult, fast_validate_tool_use


def build_default_registry() -> ToolRegistry:
    from .grep import grep_spec

    registry = ToolRegistry([grep_spec])
    registry.freeze()
    return registry

__all__ = [
    "ArtifactRef", "ExecutionContext", "ExecutionTraits", "FieldError", "ResultPolicy",
    "RetryPolicy", "ToolExecutionError", "ToolFailure", "ToolProtocolError", "ToolRegistry",
    "ToolRegistryView", "ToolSpec", "ToolTarget", "build_default_registry", "FastValidationResult", "fast_validate_tool_use",
]
