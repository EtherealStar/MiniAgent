import json
from collections import deque

from miniagent.context import ContextBuilder
from miniagent.domain import Message, Role
from miniagent.loop import AgentLoop
from miniagent.provider.events import ResponseCompleted, TextDelta, ToolUseDelta
from miniagent.session import SessionEngine
from miniagent.tools import build_default_registry
from miniagent.tools.executor import ToolExecutor


class Model:
    def __init__(self, calls):
        self.calls = deque(calls)
        self.tools = []

    async def stream(self, context, tools, options, cancellation):
        self.tools.append(tools)
        for event in self.calls.popleft():
            yield event


async def test_default_registry_executes_through_agent_loop(tmp_path):
    (tmp_path / "file.txt").write_text("needle\n", encoding="utf-8")
    arguments = json.dumps({
        "pattern": "needle", "path": ".", "include": None,
        "case_sensitive": True, "max_matches": 10,
        "correction_of_tool_use_id": None,
    })
    model = Model([
        [ToolUseDelta(0, "call-1", name_fragment="grep", arguments_fragment=arguments), ResponseCompleted("tool_calls")],
        [TextDelta("done"), ResponseCompleted("stop")],
    ])
    registry = build_default_registry()
    executor = ToolExecutor(registry.enabled_view(), tmp_path, "session")
    session = SessionEngine()
    run_id, cancellation = session.begin_run()
    result = await AgentLoop(model, ContextBuilder(), executor, tools=registry.enabled_view().specs).run(
        (), Message.text(Role.USER, "search"), "system", 3, session, cancellation, run_id
    )
    assert result.turn_count == 2
    assert model.tools[0][0].function_schema["function"]["strict"] is True
    tool_messages = [message for message in session.messages if message.role is Role.TOOL]
    assert tool_messages[0].parts[0].tool_use_id == "call-1"
    assert "file.txt:1:needle" in tool_messages[0].parts[0].content
