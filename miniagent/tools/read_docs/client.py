from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from miniagent.ports import Cancellation

from ..models import ExecutionErrorCode, ToolExecutionError
from .archive import extract_full_markdown

CONTROL_ROOT = "https://mineru.net"
CREATE_URL = f"{CONTROL_ROOT}/api/v4/file-urls/batch"
POLL_URL = f"{CONTROL_ROOT}/api/v4/extract-results/batch"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
NON_TERMINAL = {"waiting-file", "pending", "running", "converting"}


class MinerUClient:
    def __init__(
        self,
        token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(follow_redirects=False)
        self._sleep = sleep
        self._monotonic = monotonic

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def convert(self, source_path: Path, *, model_version: str,
                      cancellation: Cancellation, deadline: float) -> Path:
        cancellation.raise_if_cancelled()
        create = await self._control_request(
            "POST", CREATE_URL, timeout=30,
            json={"files": [{"name": source_path.name}], "model_version": model_version},
        )
        data = self._success_data(create)
        batch_id = data.get("batch_id")
        urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not batch_id or not isinstance(urls, list) or len(urls) != 1:
            raise self._invalid()
        upload_url = self._safe_url(urls[0])
        payload = await asyncio.to_thread(source_path.read_bytes)
        cancellation.raise_if_cancelled()
        upload = await self._request_with_safe_redirects("PUT", upload_url, content=payload, timeout=60)
        if upload.status_code < 200 or upload.status_code >= 300:
            raise self._http_error(upload.status_code)

        delays = (1.0, 2.0, 4.0, 5.0)
        attempt = 0
        while True:
            cancellation.raise_if_cancelled()
            if self._monotonic() >= deadline:
                raise ToolExecutionError("MinerU conversion timed out.", code=ExecutionErrorCode.DEADLINE_EXCEEDED, outcome_unknown=True)
            poll = await self._control_request("GET", f"{POLL_URL}/{batch_id}", timeout=30)
            poll_data = self._success_data(poll)
            results = poll_data.get("extract_result")
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                raise self._invalid()
            item = results[0]
            state = item.get("state")
            if state == "failed":
                raise ToolExecutionError("MinerU could not parse the document.", code=ExecutionErrorCode.OPERATION_FAILED)
            if state == "done":
                download_url = self._safe_url(item.get("full_zip_url"))
                archive = await self._download(download_url, cancellation)
                try:
                    return await asyncio.to_thread(extract_full_markdown, archive, archive.parent)
                finally:
                    archive.unlink(missing_ok=True)
            if state not in NON_TERMINAL:
                raise self._invalid()
            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1
            await self._sleep(delay)

    async def _control_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(
                method, url, headers={"Authorization": f"Bearer {self._token}"}, **kwargs
            )
        except httpx.TransportError as exc:
            raise ToolExecutionError(
                "MinerU is temporarily unavailable.", code=ExecutionErrorCode.RESOURCE_UNAVAILABLE,
                transient=True,
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise self._http_error(response.status_code)
        return response

    def _success_data(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError as exc:
            raise self._invalid() from exc
        if not isinstance(body, dict):
            raise self._invalid()
        code = body.get("code")
        if code != 0:
            raise self._business_error(code)
        data = body.get("data")
        if not isinstance(data, dict):
            raise self._invalid()
        return data

    async def _download(self, url: str, cancellation: Cancellation) -> Path:
        response = await self._request_with_safe_redirects("GET", url, timeout=30)
        if response.status_code < 200 or response.status_code >= 300:
            raise self._http_error(response.status_code)
        fd, name = tempfile.mkstemp(prefix="mineru-", suffix=".zip")
        path = Path(name)
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in response.aiter_bytes():
                    cancellation.raise_if_cancelled()
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ToolExecutionError("MinerU archive exceeds the download budget.", code=ExecutionErrorCode.RESOURCE_EXHAUSTED)
                    handle.write(chunk)
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def _request_with_safe_redirects(self, method: str, url: str, **kwargs) -> httpx.Response:
        current = self._safe_url(url)
        for _ in range(6):
            try:
                response = await self._client.request(method, current, follow_redirects=False, **kwargs)
            except httpx.TransportError as exc:
                raise ToolExecutionError("MinerU transfer failed.", code=ExecutionErrorCode.RESOURCE_UNAVAILABLE) from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            current = self._safe_url(urljoin(current, location or ""))
        raise self._invalid()

    @staticmethod
    def _safe_url(value: object) -> str:
        if not isinstance(value, str):
            raise MinerUClient._invalid()
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MinerUClient._invalid()
        return value

    @staticmethod
    def _invalid() -> ToolExecutionError:
        return ToolExecutionError("MinerU returned an invalid response.", code=ExecutionErrorCode.INVALID_RESPONSE)

    @staticmethod
    def _http_error(status: int) -> ToolExecutionError:
        if status in {401, 403}:
            return ToolExecutionError("MinerU authentication failed.", code=ExecutionErrorCode.AUTHENTICATION_FAILED)
        if status == 429:
            return ToolExecutionError("MinerU rate limit was reached.", code=ExecutionErrorCode.RATE_LIMITED)
        if status >= 500:
            return ToolExecutionError("MinerU is temporarily unavailable.", code=ExecutionErrorCode.RESOURCE_UNAVAILABLE)
        return ToolExecutionError("MinerU rejected the request.", code=ExecutionErrorCode.OPERATION_FAILED)

    @staticmethod
    def _business_error(code: object) -> ToolExecutionError:
        if code in {"A0202", "A0211"}:
            return ToolExecutionError("MinerU authentication failed.", code=ExecutionErrorCode.AUTHENTICATION_FAILED)
        if code == -60018:
            return ToolExecutionError("MinerU quota is exhausted.", code=ExecutionErrorCode.QUOTA_EXCEEDED)
        if code in {-10001, -60001, -60007, -60009, -60017}:
            return ToolExecutionError("MinerU is temporarily unavailable.", code=ExecutionErrorCode.RESOURCE_UNAVAILABLE)
        if code in {-60002, -60004, -60006, -60011, -60015, -60016}:
            return ToolExecutionError("MinerU does not support this document.", code=ExecutionErrorCode.UNSUPPORTED_OPERATION)
        if code == -60005:
            return ToolExecutionError("The document exceeds MinerU limits.", code=ExecutionErrorCode.RESOURCE_EXHAUSTED)
        return ToolExecutionError("MinerU could not parse the document.", code=ExecutionErrorCode.OPERATION_FAILED)
