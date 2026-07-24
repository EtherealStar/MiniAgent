from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static, TextArea

from ..provider.config import Configured, ProviderConfigLoader
from ..provider.errors import ProviderNotConfiguredError
from ..provider.openai import OpenAICompatibleModelAdapter
from ..context import ContextManager, PromptInputs
from ..hooks import FastToolValidationHook, HookDispatcher, HookRegistry
from ..tools import build_default_registry
from ..documents import DocumentCache
from ..tools.authorization import PermissionDecision, PermissionRequest
from ..tools.config import ExternalToolConfigLoader
from ..tools.executor import ToolExecutor
from ..tools.read_docs.client import MinerUClient
from ..tools.todo_write.tool import TodoStore
from ..repository import SessionRepository
from ..ui.projection import MessageLifecycle, UiProjection
from .commands import CommandName, parse_command
from .composer import Composer
from .modals.model_picker import ModelPickerModal
from .modals.session_picker import SessionPickerModal
from .modals.permission import PermissionModal
from .renderers.status import RunState
from .session_facade import RuntimeSession
from .status_bar import StatusBar
from .theme import apply_theme
from .viewport import MessageViewport, NewContentButton
from .runtime import RuntimeConfigStore


class _UnavailableLoop:
    async def run(self, initial_messages, user_message, system_prompt, max_turns, committer, cancellation, run_id):
        from ..domain import AgentRunResult, ErrorInfo, StopReason

        await committer.finish_run(run_id, AgentRunResult(StopReason.MODEL_UNAVAILABLE, 0, error=ErrorInfo("model_unavailable", "尚未配置模型")))


class _ConfiguredLoop:
    def __init__(self, configuration, tool_configuration, permission_requester, todo_store) -> None:
        self.configuration = configuration
        self.tool_configuration = tool_configuration
        self.permission_requester = permission_requester
        self.todo_store = todo_store

    async def run(self, initial_messages, user_message, system_prompt, max_turns, committer, cancellation, run_id):
        from ..loop import AgentLoop

        external_names = []
        capabilities = {"todo_store": self.todo_store}
        enabled_reads = set()
        mineru_client = None
        tavily_client = None
        if self.tool_configuration.tavily_api_key:
            from tavily import AsyncTavilyClient
            tavily_client = AsyncTavilyClient(api_key=self.tool_configuration.tavily_api_key)
            capabilities["tavily_client"] = tavily_client
            external_names.append("web_search")
            enabled_reads.add("api.tavily.com")
        if self.tool_configuration.mineru_api_token:
            mineru_client = MinerUClient(self.tool_configuration.mineru_api_token)
            capabilities["mineru_client"] = mineru_client
            external_names.append("read_docs")
        registry = build_default_registry(external_tools=tuple(external_names))
        hook_registry = HookRegistry()
        hook_registry.register_pre_tool_use(FastToolValidationHook())
        dispatcher = HookDispatcher(hook_registry.freeze())
        model = OpenAICompatibleModelAdapter(self.configuration)
        workspace = Path.cwd()
        document_registry = committer.ensure_document_registry(workspace)
        capabilities["document_cache"] = DocumentCache(workspace, document_registry)
        authorizer = committer.ensure_target_authorizer(
            workspace,
            enabled_external_reads=enabled_reads,
            requester=self.permission_requester,
        )
        executor = ToolExecutor(
            registry.enabled_view(), workspace, str(committer.session_id),
            runtime_capabilities=capabilities, target_authorizer=authorizer,
        )
        agents_path = workspace / "AGENTS.md"
        try:
            agents_md = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else ""
        except OSError:
            agents_md = ""
        frozen_now = datetime.now().astimezone()
        # 工作空间事实只在 AgentRun 开始时读取一次，后续 ModelCall 复用同一快照。
        prompt_inputs = PromptInputs(
            identity=system_prompt,
            workspace_state=str(workspace),
            agents_md=agents_md,
            current_time=frozen_now,
            timezone_name=str(frozen_now.tzinfo or ""),
        )
        try:
            return await AgentLoop(
                model,
                ContextManager(),
                executor,
                registry.enabled_view().specs,
                dispatcher=dispatcher,
            ).run(initial_messages, user_message, prompt_inputs, max_turns, committer, cancellation, run_id)
        finally:
            closers = [model.close()]
            if mineru_client is not None:
                closers.append(mineru_client.close())
            if tavily_client is not None:
                closers.append(tavily_client.close())
            await asyncio.gather(*closers, return_exceptions=True)


class MiniAgentApp(App[None]):
    TITLE = "MiniAgent"
    CSS_PATH = "miniagent.tcss"
    BINDINGS = [Binding("ctrl+c", "cancel_or_quit", "取消/退出", show=True)]

    def __init__(
        self,
        *,
        repository: SessionRepository | None = None,
        loop_factory: Callable[[], object] | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository or SessionRepository(Path(".miniagent") / "sessions")
        self.projection = UiProjection()
        self.current: RuntimeSession | None = None
        self._loop_factory = loop_factory or self._default_loop_factory
        self.config = RuntimeConfigStore()
        self._todo_store = TodoStore()
        self._transition_lock = asyncio.Lock()
        self._last_ctrl_c = 0.0

    def compose(self) -> ComposeResult:
        with Container(id="chat-wrap"):
            yield MessageViewport(id="message-viewport")
            yield NewContentButton("↓ 新内容", id="new-content")
        yield StatusBar(id="status-bar")
        with Container(id="composer-wrap"):
            yield Composer(id="composer")
            yield Static("输入消息，/ 打开命令", id="composer-hint")

    async def on_mount(self) -> None:
        apply_theme(self)
        self.query_one(Composer).focus()
        self._update_status()
        self._update_run_state()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "composer":
            self.query_one("#composer-hint", Static).display = not event.text_area.text

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        command = parse_command(event.text)
        if command is CommandName.QUIT:
            await self.action_quit()
            return
        if command is CommandName.CLEAR:
            await self.clear_session()
            return
        if command is CommandName.MODEL:
            selected = await self.push_screen_wait(ModelPickerModal(self._available_models()))
            if selected and selected != "未配置模型":
                self.config.set_model(selected)
                self._update_status()
            return
        if command is CommandName.SESSION:
            await self.open_session_picker()
            return
        try:
            if self.current is None:
                async with self._transition_lock:
                    if self.current is None:
                        self.current, accepted = await RuntimeSession.start(self.repository, event.text, loop_factory=self._loop_factory)
                        self.current.subscribe(self._on_update)
                        self.projection.replace(await self.current.snapshot())
            else:
                accepted = await self.current.submit(event.text)
            del accepted
            self._refresh_view()
        except Exception as exc:
            self.notify(f"无法提交消息：{exc}", severity="error")
            self.query_one(Composer).value = event.text

    async def on_composer_cancel_requested(self, event: Composer.CancelRequested) -> None:
        composer = self.query_one(Composer)
        if composer.text:
            composer.clear()
        elif self.current and self.current.cancel_active():
            self.notify("已取消当前运行")
        else:
            await self.action_cancel_or_quit()

    async def action_cancel_or_quit(self) -> None:
        import time

        now = time.monotonic()
        if now - self._last_ctrl_c <= 1.5:
            await self.action_quit()
        else:
            self._last_ctrl_c = now
            self.notify("再次按 Ctrl+C 退出")

    async def action_quit(self) -> None:
        async with self._transition_lock:
            if self.current is not None:
                await self.current.stop("APPLICATION_SHUTDOWN")
                self.current = None
        self.exit()

    async def clear_session(self) -> None:
        async with self._transition_lock:
            if self.current is not None:
                await self.current.stop("SESSION_CLEARED")
                self.current = None
            self.projection.clear()
            self._refresh_view()

    async def open_session_picker(self) -> None:
        try:
            sessions = await self.repository.list_sessions()
            selected = await self.push_screen_wait(SessionPickerModal(sessions))
            if selected == "__new__":
                await self.clear_session()
            elif selected:
                await self.switch_session(selected)
        except Exception as exc:
            self.notify(f"无法读取 Session：{exc}", severity="error")

    async def switch_session(self, session_id: str) -> None:
        async with self._transition_lock:
            opened = await self.repository.open_session(__import__("uuid").UUID(session_id))
            replacement = await RuntimeSession.open(opened, loop_factory=self._loop_factory)
            if self.current is not None:
                await self.current.stop("SESSION_SWITCHED")
            self.current = replacement
            self.current.subscribe(self._on_update)
            self.projection.replace(await replacement.snapshot())
            self._refresh_view()

    async def _on_update(self, update: object) -> None:
        self.projection.apply(update)
        self._refresh_view()

    def _refresh_view(self) -> None:
        try:
            self.query_one(MessageViewport).refresh_projection(self.projection)
            self._update_run_state()
        except Exception:
            pass

    def _derive_run_state(self) -> RunState | None:
        messages = self.projection.messages
        if any(message.lifecycle is MessageLifecycle.DRAFT for message in messages):
            return RunState("running")
        queued = sum(1 for message in messages if message.lifecycle is MessageLifecycle.QUEUED)
        if queued:
            return RunState("queued", queued)
        if messages and messages[-1].lifecycle is MessageLifecycle.FAILED:
            return RunState("error")
        return None

    def _update_run_state(self) -> None:
        try:
            self.query_one(StatusBar).update_run_state(self._derive_run_state())
        except Exception:
            pass

    def _update_status(self) -> None:
        try:
            self.query_one(StatusBar).update_status(os.getcwd(), None, self._model_name())
        except Exception:
            pass

    def _model_name(self) -> str | None:
        if self.config.get().model:
            return self.config.get().model
        loaded = ProviderConfigLoader().load(os.environ, Path(".env"))
        return loaded.configuration.model if isinstance(loaded, Configured) else None

    def _available_models(self) -> tuple[str, ...]:
        model = self._model_name()
        return (model,) if model else ("未配置模型",)

    def _default_loop_factory(self) -> object:
        loaded = ProviderConfigLoader().load(os.environ, Path(".env"))
        if not isinstance(loaded, Configured):
            return _UnavailableLoop()
        selected = self.config.get().model
        configuration = replace(loaded.configuration, model=selected) if selected else loaded.configuration
        tool_configuration = ExternalToolConfigLoader().load(os.environ, Path(".env"))
        return _ConfiguredLoop(
            configuration, tool_configuration, self._request_permission, self._todo_store
        )

    async def _request_permission(self, request: PermissionRequest, cancellation) -> PermissionDecision:
        if cancellation.cancelled:
            raise asyncio.CancelledError
        modal = PermissionModal(request)
        try:
            return await self.push_screen_wait(modal)
        except asyncio.CancelledError:
            if self.screen is modal:
                self.pop_screen()
            raise
