from pathlib import Path

from textual.widgets import Static

from miniagent.ui.app import MiniAgentApp
from miniagent.ui.composer import Composer
from miniagent.ui.status_bar import StatusBar


async def test_app_starts_without_scanning_or_creating_session_directory(tmp_path):
    root = tmp_path / "sessions"
    app = MiniAgentApp(repository=__import__("miniagent.repository", fromlist=["SessionRepository"]).SessionRepository(root))
    async with app.run_test() as pilot:
        assert not root.exists()
        await pilot.press("ctrl+c")
        # 主题化外壳已挂载：状态栏可见（Footer 已按视觉计划移除）。
        assert isinstance(app.query_one(StatusBar), StatusBar)
        assert app.theme == "miniagent"


async def test_composer_hint_hides_while_typing(tmp_path):
    from miniagent.repository import SessionRepository

    app = MiniAgentApp(repository=SessionRepository(tmp_path / "sessions"))
    async with app.run_test() as pilot:
        hint = app.query_one("#composer-hint", Static)
        assert hint.display
        app.query_one(Composer).insert("你好")
        await pilot.pause()
        assert not hint.display


async def test_status_bar_shows_run_state_and_stops_spinner(tmp_path):
    from miniagent.repository import SessionRepository
    from miniagent.ui.renderers.status import RunState

    app = MiniAgentApp(repository=SessionRepository(tmp_path / "sessions"))
    async with app.run_test() as pilot:
        status_bar = app.query_one(StatusBar)
        status_bar.update_run_state(RunState("running"))
        await pilot.pause()
        # 运行中启动 braille 帧轮转，并把"运行中"渲染到右段。
        assert status_bar._timer is not None
        assert "运行中" in str(app.query_one("#status-right", Static).renderable)
        status_bar.update_run_state(None)
        assert status_bar._timer is None


async def test_unknown_slash_text_is_submitted_as_a_normal_message(tmp_path):
    from miniagent.repository import SessionRepository

    app = MiniAgentApp(repository=SessionRepository(tmp_path / "sessions"))
    async with app.run_test() as pilot:
        composer = app.query_one(Composer)
        composer.text = "/unknown text"
        await pilot.press("enter")
        await pilot.pause()
        assert app.projection.messages[0].parts[0].content == "/unknown text"
        await pilot.press("ctrl+c")
        await pilot.press("ctrl+c")

