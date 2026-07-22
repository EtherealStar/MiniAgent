from __future__ import annotations

import inspect
import re
from copy import deepcopy
from dataclasses import dataclass
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
    def __init__(self, specs: Collection[ToolSpec] = ()) -> None:
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
            frozen.append(spec.with_schema(build_function_schema(spec)))
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
