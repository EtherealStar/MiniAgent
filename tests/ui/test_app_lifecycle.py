from pathlib import Path

from textual.widgets import Footer

from miniagent.ui.app import MiniAgentApp
from miniagent.ui.composer import Composer


async def test_app_starts_without_scanning_or_creating_session_directory(tmp_path):
    root = tmp_path / "sessions"
    app = MiniAgentApp(repository=__import__("miniagent.repository", fromlist=["SessionRepository"]).SessionRepository(root))
    async with app.run_test() as pilot:
        assert not root.exists()
        await pilot.press("ctrl+c")
        assert isinstance(app.query_one(Footer), Footer)


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

