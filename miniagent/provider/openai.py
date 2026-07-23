from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from ..domain import Message, ReasoningPart, Role, TextPart, ToolResultPart, ToolSpec, ToolUsePart
from ..ports import Cancellation, GenerationOptions, ModelContext
from .config import NotConfigured, ProviderConfiguration
from .errors import ModelContractError, ProviderNotConfiguredError
from .events import (
    ModelEvent,
    ReasoningDelta,
    ResponseCompleted,
    ResponseFailed,
    TextDelta,
    ToolUseDelta,
    Usage,
)


class _ProtocolError(Exception):
    pass


class OpenAICompatibleModelAdapter:
    def __init__(self, configuration: ProviderConfiguration | NotConfigured, client: httpx.AsyncClient | None = None) -> None:
        if isinstance(configuration, NotConfigured):
            raise ProviderNotConfiguredError(configuration.missing)
        self._configuration = configuration
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._closed = False

    @property
    def provider_name(self) -> str:
        return self._configuration.chat_completions_url.split("/", 3)[2]

    @property
    def model_id(self) -> str:
        return self._configuration.model

    async def stream(
        self,
        context: ModelContext,
        tools: tuple[ToolSpec, ...],
        options: GenerationOptions,
        cancellation: Cancellation,
    ) -> AsyncIterator[ModelEvent]:
        if self._closed:
            raise RuntimeError("ModelAdapter 已关闭")
        cancellation.raise_if_cancelled()
        body = self._request_body(context, tools, options)
        headers = {
            "Authorization": f"Bearer {self._configuration.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(
            connect=10.0,
            read=self._configuration.read_timeout_seconds,
            write=self._configuration.read_timeout_seconds,
            pool=self._configuration.read_timeout_seconds,
        )
        try:
            async with self._client.stream(
                "POST",
                self._configuration.chat_completions_url,
                headers=headers,
                json=body,
                timeout=timeout,
            ) as response:
                cancellation.raise_if_cancelled()
                if response.status_code >= 400:
                    yield await self._http_failure(response)
                    return
                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                    yield ResponseFailed(category="protocol_error", message="供应商响应不是 text/event-stream")
                    return
                try:
                    async for event in self._read_sse(response, cancellation):
                        yield event
                except _ProtocolError as exc:
                    yield ResponseFailed(category="protocol_error", message=self._safe(str(exc)))
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            yield ResponseFailed(category="timeout", message="模型供应商请求超时")
        except httpx.NetworkError:
            yield ResponseFailed(category="connection_error", message="无法连接模型供应商")
        except httpx.HTTPError:
            yield ResponseFailed(category="connection_error", message="模型供应商连接中断")

    async def _read_sse(self, response: httpx.Response, cancellation: Cancellation) -> AsyncIterator[ModelEvent]:
        finish_reason: str | None = None
        usage: Usage | None = None
        saw_done = False
        async for line in self._lines_until_cancelled(response, cancellation):
            cancellation.raise_if_cancelled()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].lstrip()
            if data == "[DONE]":
                saw_done = True
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise _ProtocolError("SSE data 不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise _ProtocolError("SSE data 必须是 JSON object")
            raw_usage = payload.get("usage")
            if raw_usage is not None:
                usage = self._parse_usage(raw_usage)
            choices = payload.get("choices", [])
            if not isinstance(choices, list):
                raise _ProtocolError("choices 必须是数组")
            for choice in choices:
                if not isinstance(choice, dict):
                    raise _ProtocolError("choice 必须是 object")
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise _ProtocolError("delta 必须是 object")
                content = delta.get("content")
                if content:
                    if not isinstance(content, str):
                        raise _ProtocolError("delta.content 必须是字符串")
                    yield TextDelta(content)
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    if not isinstance(reasoning, str):
                        raise _ProtocolError("delta.reasoning_content 必须是字符串")
                    yield ReasoningDelta(reasoning)
                tool_calls = delta.get("tool_calls") or []
                if not isinstance(tool_calls, list):
                    raise _ProtocolError("delta.tool_calls 必须是数组")
                for call in tool_calls:
                    if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                        raise _ProtocolError("tool call 必须包含整数 index")
                    function = call.get("function") or {}
                    if not isinstance(function, dict):
                        raise _ProtocolError("tool call function 必须是 object")
                    yield ToolUseDelta(
                        index=call["index"],
                        tool_use_id_fragment=str(call.get("id") or ""),
                        type_fragment=str(call.get("type") or ""),
                        name_fragment=str(function.get("name") or ""),
                        arguments_fragment=str(function.get("arguments") or ""),
                    )
        if not saw_done:
            raise _ProtocolError("SSE 流在 [DONE] 前结束")
        yield ResponseCompleted(
            finish_reason=finish_reason,
            usage=usage,
            request_id=response.headers.get("x-request-id"),
        )

    @staticmethod
    async def _lines_until_cancelled(response: httpx.Response, cancellation: Cancellation) -> AsyncIterator[str]:
        iterator = response.aiter_lines().__aiter__()
        while True:
            next_line = asyncio.create_task(anext(iterator))
            cancelled = asyncio.create_task(cancellation.wait())
            done, _ = await asyncio.wait({next_line, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                next_line.cancel()
                await response.aclose()
                try:
                    await next_line
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                raise asyncio.CancelledError
            cancelled.cancel()
            try:
                await cancelled
            except asyncio.CancelledError:
                pass
            try:
                yield next_line.result()
            except StopAsyncIteration:
                return

    def _request_body(
        self,
        context: ModelContext,
        tools: tuple[ToolSpec, ...],
        options: GenerationOptions,
    ) -> dict[str, Any]:
        if options.tool_choice is not None and not tools:
            raise ModelContractError("没有工具时不能设置 tool_choice")
        if isinstance(options.tool_choice, str) and options.tool_choice not in {"auto", "none", "required"}:
            raise ModelContractError("tool_choice 字符串值无效")
        body: dict[str, Any] = {
            "model": self._configuration.model,
            "messages": [self._message_to_openai(message) for message in context.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if options.temperature is not None:
            body["temperature"] = options.temperature
        if options.max_tokens is not None:
            body["max_tokens"] = options.max_tokens
        if tools:
            body["tools"] = [self._tool_schema(tool) for tool in tools]
            body["tool_choice"] = options.tool_choice if options.tool_choice is not None else "auto"
        return body

    @staticmethod
    def _tool_schema(tool: ToolSpec) -> dict[str, Any]:
        schema = dict(tool.function_schema)
        if schema.get("type") == "function" and "function" in schema:
            return schema
        return {"type": "function", "function": {"name": tool.name, **schema}}

    @staticmethod
    def _message_to_openai(message: Message) -> dict[str, Any]:
        if message.role in {Role.SYSTEM, Role.USER}:
            return {"role": message.role.value, "content": "".join(part.content for part in message.parts if isinstance(part, TextPart))}
        if message.role is Role.TOOL:
            if len(message.parts) != 1 or not isinstance(message.parts[0], ToolResultPart):
                raise ModelContractError("tool 消息必须包含一个 ToolResultPart")
            result = message.parts[0]
            return {"role": "tool", "tool_call_id": result.tool_use_id, "content": result.content}
        result: dict[str, Any] = {"role": "assistant"}
        text = "".join(part.content for part in message.parts if isinstance(part, TextPart))
        reasoning = "".join(part.content for part in message.parts if isinstance(part, ReasoningPart))
        result["content"] = text or None
        if reasoning:
            result["reasoning_content"] = reasoning
        calls = [part for part in message.parts if isinstance(part, ToolUsePart)]
        if calls:
            result["tool_calls"] = [
                {"id": part.tool_use_id, "type": "function", "function": {"name": part.name, "arguments": part.arguments}}
                for part in calls
            ]
        return result

    async def _http_failure(self, response: httpx.Response) -> ResponseFailed:
        await response.aread()
        status = response.status_code
        category = "authentication" if status in {401, 403} else "rate_limit" if status == 429 else "client_error" if status < 500 else "server_error"
        code = provider_type = None
        message = f"模型供应商返回 HTTP {status}"
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(error, dict):
                code = str(error.get("code")) if error.get("code") is not None else None
                provider_type = str(error.get("type")) if error.get("type") is not None else None
                if error.get("message"):
                    message = self._safe(str(error["message"]))
        except (ValueError, json.JSONDecodeError):
            pass
        return ResponseFailed(
            category=category,
            message=message,
            status_code=status,
            provider_code=code,
            provider_type=provider_type,
            request_id=response.headers.get("x-request-id"),
        )

    @staticmethod
    def _parse_usage(value: object) -> Usage:
        if not isinstance(value, Mapping):
            raise _ProtocolError("usage 必须是 object")
        try:
            return Usage(
                prompt_tokens=int(value["prompt_tokens"]),
                completion_tokens=int(value["completion_tokens"]),
                total_tokens=int(value["total_tokens"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _ProtocolError("usage 字段无效") from exc

    def _safe(self, message: str) -> str:
        return message.replace(self._configuration.api_key, "[REDACTED]")[:300]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatibleModelAdapter:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
