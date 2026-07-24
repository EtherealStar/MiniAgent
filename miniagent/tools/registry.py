from __future__ import annotations

import inspect
import re
import importlib
from types import MappingProxyType
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Collection

from pydantic import BaseModel

from .models import ToolRegistryError, ToolSpec
from .schema import build_function_schema


@dataclass(frozen=True, slots=True)
class ToolRegistryView:
    _specs: tuple[ToolSpec, ...]

    def get(self, name: str) -> ToolSpec | None:
        spec = next((spec for spec in self._specs if spec.name == name), None)
        return self._copy(spec) if spec is not None else None

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._copy(spec) for spec in self._specs)

    def function_schemas(self) -> tuple[dict[str, object], ...]:
        return tuple(deepcopy(dict(spec.function_schema or {})) for spec in self._specs)

    @staticmethod
    def _copy(spec: ToolSpec) -> ToolSpec:
        return spec.with_schema(deepcopy(dict(spec.function_schema or {})))


class ToolRegistry:
    def __init__(self, specs: Collection[ToolSpec] = (), available_names: Collection[str] | None = None) -> None:
        # 只按 composition root 明确列出的同名包加载，绝不扫描目录。
        if available_names is not None:
            loaded = []
            for name in available_names:
                module = importlib.import_module(f"miniagent.tools.{name}")
                spec = getattr(module, "SPEC", None) or getattr(module, f"{name}_spec", None)
                if spec is None:
                    tool_module = importlib.import_module(f"miniagent.tools.{name}.tool")
                    spec = getattr(tool_module, "SPEC", None) or getattr(tool_module, f"{name}_spec")
                loaded.append(spec)
            specs = tuple(loaded)
        self._pending = list(specs)
        self._view: ToolRegistryView | None = None

    @property
    def frozen(self) -> bool:
        return self._view is not None

    def register(self, spec: ToolSpec) -> None:
        if self.frozen:
            raise ToolRegistryError("注册表冻结后不能继续注册工具")
        if any(existing.name == spec.name for existing in self._pending):
            raise ToolRegistryError(f"工具名称重复: {spec.name}")
        self._pending.append(spec)

    def freeze(self) -> None:
        if self.frozen:
            return
        names: set[str] = set()
        frozen: list[ToolSpec] = []
        for spec in self._pending:
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec.name) is None:
                raise ToolRegistryError("工具名称必须是 1 到 64 位字母、数字、下划线或连字符")
            if spec.name in names:
                raise ToolRegistryError(f"工具名称重复: {spec.name}")
            names.add(spec.name)
            if not isinstance(spec.input_model, type) or not issubclass(spec.input_model, BaseModel):
                raise ToolRegistryError(f"工具 {spec.name} 的 input_model 必须是 Pydantic 模型")
            if spec.input_model.model_config.get("extra") != "forbid":
                raise ToolRegistryError(f"工具 {spec.name} 的 input_model 必须设置 extra='forbid'")
            if not inspect.iscoroutinefunction(spec.handler):
                raise ToolRegistryError(f"工具 {spec.name} 的 handler 必须是 async 函数")
            if not isinstance(spec.output_model, type) or not issubclass(spec.output_model, BaseModel):
                raise ToolRegistryError(f"工具 {spec.name} 的 output_model 必须是 Pydantic 模型")
            if spec.output_model.model_config.get("extra") != "forbid":
                raise ToolRegistryError(f"工具 {spec.name} 的 output_model 必须设置 extra='forbid'")
            if spec.prompt_ref:
                module_name, sep, symbol = spec.prompt_ref.partition(":")
                if not sep:
                    raise ToolRegistryError(f"工具 {spec.name} 的 PromptRef 无效")
                try:
                    prompt = getattr(importlib.import_module(module_name), symbol)
                except (ImportError, AttributeError) as exc:
                    raise ToolRegistryError(f"工具 {spec.name} 的 PromptRef 无法解析") from exc
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ToolRegistryError(f"工具 {spec.name} 的 Prompt 不能为空")
            schema = build_function_schema(spec)
            output_schema = spec.output_model.model_json_schema(by_alias=True)
            frozen.append(spec.with_schema(schema))
            frozen[-1] = replace(frozen[-1], output_schema=MappingProxyType(output_schema))
        self._view = ToolRegistryView(tuple(frozen))

    def _require_view(self) -> ToolRegistryView:
        if self._view is None:
            raise ToolRegistryError("注册表必须先冻结")
        return self._view

    def get(self, name: str) -> ToolSpec | None:
        return self._require_view().get(name)

    def function_schemas(self) -> tuple[dict[str, object], ...]:
        return self._require_view().function_schemas()

    def enabled_view(self, names: Collection[str] | None = None) -> ToolRegistryView:
        view = self._require_view()
        if names is None:
            return view
        requested = set(names)
        missing = requested - {spec.name for spec in view.specs}
        if missing:
            raise ToolRegistryError(f"未知工具: {', '.join(sorted(missing))}")
        return ToolRegistryView(tuple(spec for spec in view.specs if spec.name in requested))
