from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import ToolRegistryError, ToolSpec


UNSUPPORTED = {"oneOf", "not", "if", "then", "else", "patternProperties", "unevaluatedProperties"}


def build_function_schema(spec: ToolSpec) -> dict[str, object]:
    raw = spec.input_model.model_json_schema(by_alias=True)
    definitions = raw.pop("$defs", {})

    def convert(node: Any, path: str, resolving: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [convert(item, f"{path}[{index}]", resolving) for index, item in enumerate(node)]
        if not isinstance(node, dict):
            return node
        for keyword in UNSUPPORTED & node.keys():
            raise ToolRegistryError(f"工具 {spec.name} 的 schema 在 {path} 使用不支持的 {keyword}")
        if "$ref" in node:
            ref = node["$ref"]
            prefix = "#/$defs/"
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise ToolRegistryError(f"工具 {spec.name} 的 schema 在 {path} 包含非本地引用")
            key = ref[len(prefix):]
            if key in resolving:
                raise ToolRegistryError(f"工具 {spec.name} 的 schema 在 {path} 包含递归引用 {key}")
            if key not in definitions:
                raise ToolRegistryError(f"工具 {spec.name} 的 schema 在 {path} 引用了缺失定义 {key}")
            # 冻结时内联定义，使提交给模型的 schema 不依赖本地 $defs。
            merged = {**deepcopy(definitions[key]), **{k: v for k, v in node.items() if k != "$ref"}}
            return convert(merged, path, resolving + (key,))
        result = {key: convert(value, f"{path}.{key}", resolving) for key, value in node.items()}
        if result.get("type") == "object" or "properties" in result:
            properties = result.setdefault("properties", {})
            result["additionalProperties"] = False
            result["required"] = list(properties)
        return result

    parameters = convert(raw, "$parameters")
    properties = parameters.setdefault("properties", {})
    properties["correction_of_tool_use_id"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "被本次调用修正的原始工具调用 ID；普通调用传 null。",
    }
    parameters["required"] = list(properties)
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "strict": True,
            "parameters": parameters,
        },
    }
