import asyncio
import json

import httpx
import pytest

from miniagent.domain import Message, Role, ToolSpec, ToolUsePart
from miniagent.ports import Cancellation, GenerationOptions, ModelContext
from miniagent.provider.config import ProviderConfiguration
from miniagent.provider.events import ReasoningDelta, ResponseCompleted, ResponseFailed, TextDelta, ToolUseDelta
from miniagent.provider.openai import OpenAICompatibleModelAdapter


def sse(*payloads: object) -> bytes:
    lines = [f"data: {json.dumps(payload)}\n\n" for payload in payloads]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


async def collect(adapter, context=ModelContext(()), tools=(), options=GenerationOptions()):
    return [event async for event in adapter.stream(context, tools, options, Cancellation())]


async def test_request_and_stream_conversion_preserve_raw_deltas():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        content = sse(
            {"choices": [{"delta": {"content": "<think>x", "reasoning_content": "r", "tool_calls": [{"index": 0, "id": "call_", "type": "function", "function": {"name": "re", "arguments": '{"q":'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "ad", "arguments": "1}"}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ProviderConfiguration("model", "https://provider.test/v1", "secret")
    adapter = OpenAICompatibleModelAdapter(config, client)
    tool = ToolSpec("read", {"description": "read", "parameters": {"type": "object"}})
    events = await collect(adapter, tools=(tool,), options=GenerationOptions(temperature=0.2, max_tokens=10))

    assert isinstance(events[0], TextDelta) and events[0].content == "<think>x"
    assert isinstance(events[1], ReasoningDelta)
    assert [event.arguments_fragment for event in events if isinstance(event, ToolUseDelta)] == ['{"q":', "1}"]
    assert isinstance(events[-1], ResponseCompleted) and events[-1].usage.total_tokens == 5
    request_body = json.loads(requests[0].content)
    assert request_body["stream_options"] == {"include_usage": True}
    assert request_body["tool_choice"] == "auto"
    assert request_body["temperature"] == 0.2
    assert requests[0].headers["authorization"] == "Bearer secret"
    await adapter.close()
    assert not client.is_closed
    await client.aclose()


async def test_message_tool_ids_are_converted():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))

    assistant = Message(role=Role.ASSISTANT, parts=(ToolUsePart("read", "{}", "call-1"),))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "k"), client)
    await collect(adapter, ModelContext((assistant,)))
    assert seen["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert "tools" not in seen and "tool_choice" not in seen
    await client.aclose()


@pytest.mark.parametrize(("status", "category"), [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (400, "client_error"), (503, "server_error")])
async def test_http_error_mapping_and_secret_redaction(status, category):
    def handler(request):
        return httpx.Response(status, json={"error": {"code": "bad", "message": "secret leaked"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "secret"), client)
    events = await collect(adapter)
    assert len(events) == 1 and isinstance(events[0], ResponseFailed)
    assert events[0].category == category and "secret" not in events[0].message
    await client.aclose()


@pytest.mark.parametrize("content", [b"data: nope\n\ndata: [DONE]\n\n", b"data: {}\n\n"])
async def test_protocol_errors_have_one_terminal(content):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)))
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "k"), client)
    events = await collect(adapter)
    assert len([event for event in events if isinstance(event, (ResponseCompleted, ResponseFailed))]) == 1
    assert isinstance(events[-1], ResponseFailed) and events[-1].category == "protocol_error"
    await client.aclose()


async def test_connection_error_mapping():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "k"), client)
    assert (await collect(adapter))[0].category == "connection_error"
    await client.aclose()


async def test_timeout_mapping():
    def handler(request):
        raise httpx.ReadTimeout("late", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "k"), client)
    assert (await collect(adapter))[0].category == "timeout"
    await client.aclose()


async def test_cancel_before_request_raises_cancelled_error():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "k"), client)
    cancellation = Cancellation()
    cancellation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await anext(adapter.stream(ModelContext(()), (), GenerationOptions(), cancellation))
    assert calls == 0
    await client.aclose()


async def test_owned_client_is_closed():
    adapter = OpenAICompatibleModelAdapter(ProviderConfiguration("m", "https://p.test", "k"))
    client = adapter._client
    await adapter.close()
    assert client.is_closed
