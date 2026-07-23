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
from ..tools.executor import ToolExecutor
from ..repository import SessionRepository
from ..ui.projection import MessageLifecycle, UiProjection
from .commands import CommandName, parse_command
from .composer import Composer
from .modals.model_picker import ModelPickerModal
from .modals.session_picker import SessionPickerModal
from .renderers.status import RunState
from .session_facade import RuntimeSession
from .status_bar import StatusBar
from .theme import MINIAGENT_THEME, apply_theme
from .viewport import MessageViewport, NewContentButton
from .runtime import RuntimeConfigStore


class _UnavailableLoop:
    async def run(self, initial_messages, user_message, system_prompt, max_turns, committer, cancellation, run_id):
        from ..domain import AgentRunResult, ErrorInfo, StopReason

        await committer.finish_run(run_id, AgentRunResult(StopReason.MODEL_UNAVAILABLE, 0, error=ErrorInfo("model_unavailable", "尚未配置模型")))


class _ConfiguredLoop:
    def __init__(self, configuration) -> None:
        self.configuration = configuration

    async def run(self, initial_messages, user_message, system_prompt, max_turns, committer, cancellation, run_id):
        from ..loop import AgentLoop

        registry = build_default_registry()
        hook_registry = HookRegistry()
        hook_registry.register_pre_tool_use(FastToolValidationHook())
        dispatcher = HookDispatcher(hook_registry.freeze())
        model = OpenAICompatibleModelAdapter(self.configuration)
        executor = ToolExecutor(registry.enabled_view(), Path.cwd(), str(committer.session_id))
        workspace = Path.cwd()
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
            await model.close()


class MiniAgentApp(App[None]):
    TITLE = "MiniAgent"
    CSS = """
    Screen { background: $background; }

    #chat-wrap { height: 1fr; }
    #message-viewport { background: $background; }
    .message { padding: 0 1; margin-bottom: 1; }
    /* Textual 没有 right/bottom 属性：绝对定位以父容器左上角为原点，
       具体偏移由 MessageViewport 按尺寸换算（距右 2、距底 1）。 */
    #new-content {
        display: none;
        position: absolute;
        width: auto;
        height: 1;
        padding: 0 1;
        background: $surface-2;
        color: $primary;
    }

    #status-bar { height: 1; background: $surface; padding: 0 1; }
    #status-left { width: 1fr; }
    #status-right { width: auto; }

    #composer-wrap { height: auto; }
    #composer {
        height: 5;
        min-height: 3;
        padding: 0 1;
        border-top: solid $surface-2;
        background: $background;
    }
    #composer:focus { border-top: solid $primary; }
    #composer-hint {
        position: absolute;
        offset: 1 1;
        width: auto;
        height: 1;
        color: $muted;
        background: $background;
    }

    #model-picker, #session-picker {
        margin: 4 8;
        padding: 1 2;
        background: $surface;
        border: solid $surface-2;
    }
    #model-picker Label, #session-picker Label { text-style: bold; margin-bottom: 1; }
    """
    BINDINGS = [Binding("ctrl+c", "cancel_or_quit", "取消/退出", show=True)]

    def __init__(
        self,
        *,
        repository: SessionRepository | None = None,
        loop_factory: Callable[[], object] | None = None,
    ) -> None:
        super().__init__()
        # App.__init__ 会用默认主题快照一份 CSS 变量，而 $surface-2 等自定义变量只
        # 存在于 MiniAgent 主题；这里注册主题、预设当前主题（set_reactive 跳过尚未
        # 运行时的 watcher），并用新主题的变量重建样式表，使启动解析即可命中。
        self.register_theme(MINIAGENT_THEME)
        self.set_reactive(App.theme, MINIAGENT_THEME.name)
        self.stylesheet.set_variables(self.get_css_variables())
        self.repository = repository or SessionRepository(Path(".miniagent") / "sessions")
        self.projection = UiProjection()
        self.current: RuntimeSession | None = None
        self._loop_factory = loop_factory or self._default_loop_factory
        self.config = RuntimeConfigStore()
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
        return _ConfiguredLoop(configuration)
