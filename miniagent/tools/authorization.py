from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from miniagent.ports import Cancellation

from .models import ToolTarget


class PermissionDecision(StrEnum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    targets: tuple[ToolTarget, ...]


PermissionRequester = Callable[[PermissionRequest, Cancellation], Awaitable[PermissionDecision]]


class TargetAuthorizer:
    """统一裁决工具目标；permission 状态只保存在当前进程内。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        enabled_external_reads: Iterable[str] = (),
        requester: PermissionRequester | None = None,
        controlled_read_targets: Callable[[], frozenset[ToolTarget]] | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve(strict=True)
        self._enabled_external_reads = frozenset(enabled_external_reads)
        self._requester = requester
        self._controlled_read_targets = controlled_read_targets or (lambda: frozenset())
        self._session_grants: set[ToolTarget] = set()
        self._run_denials: dict[str, set[ToolTarget]] = {}

    async def authorize(
        self,
        tool_name: str,
        targets: tuple[ToolTarget, ...],
        run_id: str,
        cancellation: Cancellation,
    ) -> bool:
        pending = tuple(target for target in targets if not self._auto_allowed(target))
        if not pending:
            return True
        denied = self._run_denials.setdefault(run_id, set())
        if any(target in denied for target in pending):
            return False
        if all(target in self._session_grants for target in pending):
            return True
        if self._requester is None:
            denied.update(pending)
            return False

        cancellation.raise_if_cancelled()
        decision_task = asyncio.create_task(self._requester(PermissionRequest(tool_name, targets), cancellation))
        cancel_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({decision_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                decision_task.cancel()
                await asyncio.gather(decision_task, return_exceptions=True)
                raise asyncio.CancelledError
            decision = decision_task.result()
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

        if decision is PermissionDecision.ALLOW_SESSION:
            self._session_grants.update(pending)
            return True
        if decision is PermissionDecision.ALLOW_ONCE:
            return True
        denied.update(pending)
        return False

    def _auto_allowed(self, target: ToolTarget) -> bool:
        if target.kind == "session_state" and target.scope == "exact":
            return True
        if target in self._controlled_read_targets():
            return True
        if (
            target.kind == "external_service"
            and target.capability == "read"
            and target.scope == "exact"
            and target.value in self._enabled_external_reads
        ):
            return True
        if target.kind not in {"file", "directory"}:
            return False
        path = Path(target.value)
        if not path.is_absolute():
            path = self._workspace_root / path
        try:
            relative = path.resolve(strict=False).relative_to(self._workspace_root)
        except (OSError, ValueError):
            return False
        # `.mini` 是受保护子树，只允许已登记的精确受控引用自动读取。
        return not relative.parts or relative.parts[0].casefold() != ".mini"
