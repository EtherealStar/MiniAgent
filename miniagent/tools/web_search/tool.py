from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import (
    ExecutionContext, ExecutionErrorCode, ExecutionTraits, RetryPolicy, ToolExecutionError,
    ToolOutput, ToolSpec, ToolTarget,
)


class TavilyAsyncClient(Protocol):
    async def search(self, **kwargs: Any) -> dict[str, Any]: ...


class WebSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    query: str

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 400:
            raise ValueError("query must contain 1-400 characters")
        return value


class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    url: str
    snippet: str
    published_at: str | None
    snippet_truncated: bool


class WebSearchMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    returned_count: int
    dropped_invalid_count: int
    deduplicated_count: int
    truncated_snippet_count: int


class WebSearchData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    results: list[WebSearchResult]


class WebSearchOutput(ToolOutput):
    metadata: WebSearchMetadata
    data: WebSearchData


def resolve_targets(args: WebSearchInput, workspace_root: Path) -> tuple[ToolTarget, ...]:
    return (ToolTarget("external_service", "read", "api.tavily.com"),)


def classify(args: WebSearchInput, targets: tuple[ToolTarget, ...]) -> ExecutionTraits:
    return ExecutionTraits(concurrency_safe=False)


def _normalize_url(raw: object) -> str | None:
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        port = parsed.port
        if port == (80 if parsed.scheme.lower() == "http" else 443):
            port = None
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        return None


def _normalize(response: object) -> WebSearchOutput:
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise ToolExecutionError("Tavily returned an invalid response.", code=ExecutionErrorCode.INVALID_RESPONSE)
    results: list[WebSearchResult] = []
    seen: set[str] = set()
    dropped = deduplicated = truncated_count = 0
    for candidate in response["results"]:
        if not isinstance(candidate, dict):
            dropped += 1
            continue
        url = _normalize_url(candidate.get("url"))
        title = candidate.get("title")
        snippet = candidate.get("content")
        if url is None or not isinstance(title, str) or not isinstance(snippet, str):
            dropped += 1
            continue
        if url in seen:
            deduplicated += 1
            continue
        seen.add(url)
        title = title.strip() or "Untitled result"
        if len(title) > 500:
            dropped += 1
            continue
        snippet = snippet.strip() or "No snippet available."
        was_truncated = len(snippet) > 1000
        if was_truncated:
            snippet = snippet[:997] + "..."
            truncated_count += 1
        published = candidate.get("published_at")
        if not isinstance(published, str) or not published.strip():
            published = None
        results.append(WebSearchResult(
            title=title, url=url, snippet=snippet, published_at=published, snippet_truncated=was_truncated
        ))
        if len(results) == 5:
            break
    blocks = []
    for index, item in enumerate(results, 1):
        lines = [f"[{index}] {item.title}", f"URL: {item.url}", f"Snippet: {item.snippet}"]
        if item.published_at is not None:
            lines.append(f"Published: {item.published_at}")
        blocks.append("\n".join(lines))
    content = "\n\n".join(blocks) if blocks else "No search results found."
    return WebSearchOutput(
        content=content,
        metadata=WebSearchMetadata(
            returned_count=len(results), dropped_invalid_count=dropped,
            deduplicated_count=deduplicated, truncated_snippet_count=truncated_count,
        ),
        data=WebSearchData(results=results),
    )


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is int:
        return status
    status = getattr(exc, "status_code", None)
    return status if type(status) is int else None


def _translate_error(exc: Exception) -> ToolExecutionError:
    status = _status_code(exc)
    if status == 401:
        return ToolExecutionError("Tavily authentication failed.", code=ExecutionErrorCode.AUTHENTICATION_FAILED)
    if status in {432, 433}:
        return ToolExecutionError("Tavily quota is exhausted.", code=ExecutionErrorCode.QUOTA_EXCEEDED)
    if status == 429:
        return ToolExecutionError("Tavily rate limit was reached.", code=ExecutionErrorCode.RATE_LIMITED)
    if status == 400:
        return ToolExecutionError("Tavily rejected the search request.", code=ExecutionErrorCode.OPERATION_FAILED)
    if isinstance(exc, (httpx.TransportError, TimeoutError, asyncio.TimeoutError)) or (status is not None and 500 <= status < 600):
        return ToolExecutionError(
            "Tavily is temporarily unavailable.", code=ExecutionErrorCode.RESOURCE_UNAVAILABLE, transient=True
        )
    return ToolExecutionError("Tavily could not complete the search.", code=ExecutionErrorCode.OPERATION_FAILED)


async def handler(args: WebSearchInput, context: ExecutionContext) -> WebSearchOutput:
    client: TavilyAsyncClient = context.capability("tavily_client")
    try:
        async with asyncio.timeout(25):
            response = await client.search(
                query=args.query, max_results=5, include_answer=False,
                include_raw_content=False, include_images=False,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None
    return _normalize(response)


SPEC = ToolSpec(
    name="web_search",
    description="Search the public web with Tavily and return concise source results.",
    input_model=WebSearchInput, output_model=WebSearchOutput, handler=handler,
    prompt_ref="miniagent.tools.web_search.prompt:PROMPT", resolve_targets=resolve_targets,
    classify=classify, retry_policy=RetryPolicy(max_attempts=2), timeout_seconds=30.0,
)
