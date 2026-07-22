from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from miniagent.context import ContextBuilder
from miniagent.domain import Message, Role
from miniagent.loop import AgentLoop
from miniagent.provider.config import Configured, ProviderConfigLoader
from miniagent.provider.events import ResponseCompleted, TextDelta
from miniagent.session import SessionEngine
from miniagent.ports import Cancellation
from miniagent.tools import build_default_registry
from miniagent.tools.artifacts import MemoryTraceSink
from miniagent.tools.executor import ToolExecutor
from miniagent.domain import ToolExecutionBatch, ToolUsePart


class DemoModel:
    async def stream(self, context, tools, options, cancellation):
        yield TextDelta("MiniAgent 主循环已就绪。")
        yield ResponseCompleted(finish_reason="stop")


async def run_demo() -> None:
    loaded = ProviderConfigLoader().load(os.environ, Path(".env"))
    if isinstance(loaded, Configured):
        print(json.dumps({"provider": "configured", "model": loaded.configuration.model}, ensure_ascii=False))
    else:
        print(json.dumps({"provider": "not_configured", "missing": loaded.missing}, ensure_ascii=False))

    workspace = Path(__file__).parent.resolve()
    registry = build_default_registry()
    trace = MemoryTraceSink()
    executor = ToolExecutor(registry.enabled_view(), workspace, "demo-session", trace_sink=trace)
    grep_arguments = json.dumps(
        {
            "pattern": "DEMO_NEEDLE",
            "path": "tests/fixtures/demo_grep.txt",
            "include": None,
            "case_sensitive": True,
            "max_matches": 10,
            "correction_of_tool_use_id": None,
        }
    )
    demo_batch = ToolExecutionBatch(
        run_id=uuid4(),
        assistant_message_id=uuid4(),
        tool_uses=(ToolUsePart("grep", grep_arguments, "demo-grep-1"),),
    )
    grep_result = (await executor.submit_batch(demo_batch, Cancellation()))[0]
    schema = registry.function_schemas()[0]
    print(json.dumps({"registered_tools": [spec.name for spec in registry.enabled_view().specs], "strict": schema["function"]["strict"]}, ensure_ascii=False))
    print(json.dumps({"tool_use_id": grep_result.tool_use_id, "tool_name": grep_result.tool_name, "status": grep_result.status, "content": grep_result.content}, ensure_ascii=False))
    print(json.dumps({"trace_events": len(trace.events), "trace_tool_use_id": trace.events[0]["tool_use_id"]}, ensure_ascii=False))

    session = SessionEngine()
    run_id, cancellation = session.begin_run()
    loop = AgentLoop(
        model=DemoModel(),
        context_builder=ContextBuilder(),
        tool_executor=executor,
        tools=registry.enabled_view().specs,
    )
    result = await loop.run(
        initial_messages=session.messages,
        user_message=Message.text(Role.USER, "检查主循环"),
        system_prompt="你是 MiniAgent。",
        max_turns=3,
        event_sink=session,
        cancellation=cancellation,
        run_id=run_id,
    )
    session.finish_run(run_id)
    for event in session.events:
        print(f"event sequence={event.sequence} type={type(event.payload).__name__}")
    print(json.dumps({"reason": result.reason.value, "turn_count": result.turn_count, "final_message_id": str(result.final_message_id)}, ensure_ascii=False))


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
