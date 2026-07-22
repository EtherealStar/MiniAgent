from __future__ import annotations

from typing import Optional

import pytest
from pydantic import BaseModel, ConfigDict, Field

from miniagent.tools.models import ResultPolicy, ToolRegistryError, ToolSpec
from miniagent.tools.registry import ToolRegistry


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    query: str = Field(alias="pattern")
    include: str | None = None


async def handler(args, context):
    return "ok"


def spec(name="grep", model=Input, selected_handler=handler):
    return ToolSpec(name=name, input_model=model, handler=selected_handler, description="搜索文本")


def test_freeze_builds_strict_schema_and_defensive_copies():
    registry = ToolRegistry([spec()])
    with pytest.raises(ToolRegistryError, match="先冻结"):
        registry.function_schemas()
    registry.freeze()
    schema = registry.function_schemas()[0]
    function = schema["function"]
    parameters = function["parameters"]
    assert function["strict"] is True
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["pattern", "include", "correction_of_tool_use_id"]
    assert "query" not in parameters["properties"]
    assert {item["type"] for item in parameters["properties"]["include"]["anyOf"]} == {"string", "null"}
    schema["function"]["name"] = "mutated"
    assert registry.function_schemas()[0]["function"]["name"] == "grep"
    resolved = registry.get("grep")
    resolved.function_schema["function"]["parameters"]["required"].clear()
    assert registry.get("grep").function_schema["function"]["parameters"]["required"]
    registry.freeze()
    with pytest.raises(ToolRegistryError, match="冻结后"):
        registry.register(spec("other"))


def test_enabled_view_is_read_only_subset():
    registry = ToolRegistry([spec("first"), spec("second")])
    registry.freeze()
    view = registry.enabled_view(["second"])
    assert tuple(item.name for item in view.specs) == ("second",)
    assert registry.get("first") is not None
    with pytest.raises(ToolRegistryError, match="未知工具"):
        registry.enabled_view(["missing"])


def test_duplicate_names_are_rejected_even_from_constructor():
    registry = ToolRegistry([spec(), spec()])
    with pytest.raises(ToolRegistryError, match="重复"):
        registry.freeze()


def test_extra_allow_and_sync_handler_are_rejected():
    class Loose(BaseModel):
        value: str

    with pytest.raises(ToolRegistryError, match="extra='forbid'"):
        ToolRegistry([spec(model=Loose)]).freeze()

    def sync_handler(args, context):
        return "no"

    with pytest.raises(ToolRegistryError, match="async"):
        ToolRegistry([spec(selected_handler=sync_handler)]).freeze()


def test_recursive_schema_is_rejected_with_tool_and_path():
    class Node(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        name: str
        child: Optional["Node"] = None

    with pytest.raises(ToolRegistryError, match=r"grep.*\$parameters.*递归"):
        ToolRegistry([spec(model=Node)]).freeze()


def test_result_policy_cannot_raise_system_hard_limit():
    with pytest.raises(ValueError, match="系统结果硬上限"):
        ResultPolicy(threshold_bytes=60 * 1024, hard_limit_bytes=60 * 1024)


def test_invalid_openai_tool_name_is_rejected():
    with pytest.raises(ToolRegistryError, match="1 到 64"):
        ToolRegistry([spec("not/a/name")]).freeze()
