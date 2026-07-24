import asyncio
import json
import uuid

from pydantic import BaseModel, ConfigDict

from miniagent.ports import Cancellation
from miniagent.tools.authorization import PermissionDecision, TargetAuthorizer
from miniagent.tools.config import ExternalToolConfigLoader
from miniagent.domain import ToolExecutionBatch, ToolUsePart
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.models import ExecutionTraits, ToolOutput, ToolSpec, ToolTarget
from miniagent.tools.registry import ToolRegistry
from miniagent.tools import build_default_registry


def test_external_config_environment_precedence_and_secret_repr(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TAVILY_API_KEY=file-key\nMINERU_API_TOKEN=file-token\n", encoding="utf-8")
    config = ExternalToolConfigLoader().load({"TAVILY_API_KEY": " env-key "}, dotenv)
    assert config.tavily_api_key == "env-key" and config.mineru_api_token == "file-token"
    assert "env-key" not in repr(config) and "file-token" not in repr(config)


def test_external_tools_are_registered_only_from_explicit_capabilities():
    assert build_default_registry().get("web_search") is None
    assert build_default_registry(external_tools=("web_search",)).get("web_search") is not None
    assert build_default_registry(external_tools=("read_docs",)).get("read_docs") is not None
    both = build_default_registry(external_tools=("web_search", "read_docs"))
    assert both.get("web_search") is not None and both.get("read_docs") is not None


async def test_authorizer_auto_allows_fixed_read_and_caches_session_write(tmp_path):
    requests = []

    async def request(value, cancellation):
        requests.append(value)
        return PermissionDecision.ALLOW_SESSION

    authorizer = TargetAuthorizer(tmp_path, enabled_external_reads={"api.tavily.com"}, requester=request)
    read = (ToolTarget("external_service", "read", "api.tavily.com"),)
    write = (ToolTarget("external_service", "write", "mineru.net"),)
    assert await authorizer.authorize("web_search", read, "run-1", Cancellation())
    assert not requests
    assert await authorizer.authorize("read_docs", write, "run-1", Cancellation())
    assert await authorizer.authorize("read_docs", write, "run-2", Cancellation())
    assert len(requests) == 1


async def test_authorizer_cancellation_closes_pending_request(tmp_path):
    started = asyncio.Event()

    async def request(value, cancellation):
        started.set()
        await asyncio.Future()

    cancellation = Cancellation()
    subject = TargetAuthorizer(tmp_path, requester=request)
    task = asyncio.create_task(subject.authorize(
        "read_docs", (ToolTarget("external_service", "write", "mineru.net"),), "run", cancellation
    ))
    await started.wait()
    cancellation.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()


async def test_permission_wait_does_not_consume_handler_timeout(tmp_path):
    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        value: str

    async def handler(args, context):
        return ToolOutput(content=args.value)

    async def request(value, cancellation):
        await asyncio.sleep(0.03)
        return PermissionDecision.ALLOW_ONCE

    spec = ToolSpec(
        "upload", Input, handler,
        resolve_targets=lambda args, root: (ToolTarget("external_service", "write", "example.test"),),
        classify=lambda args, targets: ExecutionTraits(False),
        timeout_seconds=0.01,
    )
    registry = ToolRegistry([spec]); registry.freeze()
    executor = ToolExecutor(
        registry.enabled_view(), tmp_path, "session",
        target_authorizer=TargetAuthorizer(tmp_path, requester=request),
    )
    use = ToolUsePart("upload", json.dumps({"value": "ok", "correction_of_tool_use_id": None}), "call")
    result = (await executor.submit_batch(
        ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()
    ))[0]
    assert result.content == "ok"
