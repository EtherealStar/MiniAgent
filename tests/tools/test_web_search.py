import json

from miniagent.domain import ToolExecutionBatch, ToolUsePart
from miniagent.ports import Cancellation
from miniagent.tools.authorization import TargetAuthorizer
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.registry import ToolRegistry
from miniagent.tools.web_search.tool import WebSearchOutput, _normalize


class FakeTavily:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FlakyTavily:
    def __init__(self):
        self.calls = 0

    async def search(self, **kwargs):
        import httpx
        self.calls += 1
        if self.calls == 1:
            raise httpx.ConnectError("private diagnostic")
        return {"results": []}


def test_normalize_deduplicates_and_truncates_without_query_or_score():
    output = _normalize({"query": "secret", "results": [
        {"title": "A", "url": "HTTPS://Example.com:443/a#x", "content": "x" * 1001, "score": 1},
        {"title": "B", "url": "https://example.com/a", "content": "duplicate"},
        {"title": "", "url": "http://example.org", "content": ""},
        {"title": "bad", "url": "file:///tmp/x", "content": "no"},
    ]})
    assert isinstance(output, WebSearchOutput)
    assert output.metadata.returned_count == 2
    assert output.metadata.deduplicated_count == 1
    assert output.metadata.dropped_invalid_count == 1
    assert output.data.results[0].snippet_truncated
    assert output.data.results[1].title == "Untitled result"
    assert "secret" not in output.model_dump_json() and "score" not in output.model_dump_json()


async def test_executor_calls_tavily_with_fixed_parameters(tmp_path):
    client = FakeTavily({"results": [{"title": "A", "url": "https://example.com", "content": "S"}]})
    registry = ToolRegistry(available_names=("web_search",)); registry.freeze()
    executor = ToolExecutor(
        registry.enabled_view(), tmp_path, "session",
        runtime_capabilities={"tavily_client": client},
        target_authorizer=TargetAuthorizer(tmp_path, enabled_external_reads={"api.tavily.com"}),
    )
    use = ToolUsePart("web_search", json.dumps({"query": "focused", "correction_of_tool_use_id": None}), "call")
    import uuid
    result = (await executor.submit_batch(ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()))[0]
    assert result.output["data"]["results"][0]["title"] == "A"
    assert client.calls == [{"query": "focused", "max_results": 5, "include_answer": False, "include_raw_content": False, "include_images": False}]


async def test_only_transient_tavily_failure_retries_without_leaking_diagnostic(tmp_path):
    client = FlakyTavily()
    registry = ToolRegistry(available_names=("web_search",)); registry.freeze()
    executor = ToolExecutor(
        registry.enabled_view(), tmp_path, "session",
        runtime_capabilities={"tavily_client": client},
        target_authorizer=TargetAuthorizer(tmp_path, enabled_external_reads={"api.tavily.com"}),
    )
    use = ToolUsePart("web_search", json.dumps({"query": "focused", "correction_of_tool_use_id": None}), "call")
    import uuid
    result = (await executor.submit_batch(ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()))[0]
    assert result.content == "No search results found." and client.calls == 2
    assert "private diagnostic" not in result.content
