import hashlib
import io
import json
import time
import uuid
import zipfile
from pathlib import Path

import httpx

from miniagent.documents import DocumentCache, DocumentRegistry
from miniagent.domain import ToolExecutionBatch, ToolUsePart
from miniagent.ports import Cancellation
from miniagent.tools.authorization import PermissionDecision, TargetAuthorizer
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.read_docs.client import MinerUClient
from miniagent.tools.registry import ToolRegistry


def _archive_bytes(text="# converted") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", text)
    return buffer.getvalue()


async def test_mineru_client_runs_create_upload_poll_download_without_content_type(tmp_path):
    states = iter(["waiting-file", "pending", "running", "converting", "done"])
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v4/file-urls/batch":
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch", "file_urls": ["https://upload.example/file"]}})
        if request.url.host == "upload.example":
            return httpx.Response(200)
        if request.url.path.endswith("/batch/batch"):
            state = next(states)
            item = {"state": state}
            if state == "done":
                item["full_zip_url"] = "https://download.example/result"
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [item]}})
        if request.url.host == "download.example":
            return httpx.Response(200, content=_archive_bytes())
        raise AssertionError(request.url)

    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-test")
    sleeps = []
    async def fake_sleep(value):
        sleeps.append(value)
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = MinerUClient("token", http_client=http, sleep=fake_sleep, monotonic=lambda: 0)
    markdown = await client.convert(source, model_version="vlm", cancellation=Cancellation(), deadline=100)
    assert markdown.read_text() == "# converted"
    upload = next(request for request in requests if request.url.host == "upload.example")
    assert "content-type" not in upload.headers
    assert sleeps == [1.0, 2.0, 4.0, 5.0]
    assert not any(request.method == "POST" and "submit" in request.url.path for request in requests)
    await http.aclose()


class FakeMinerU:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.calls = 0

    async def convert(self, source_path, **kwargs):
        self.calls += 1
        target = self.tmp_path / f"converted-{self.calls}.md"
        target.write_text("# converted", encoding="utf-8")
        return target


async def test_read_docs_denial_prevents_handler_and_success_commits_cache(tmp_path):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-test")
    registry = ToolRegistry(available_names=("read_docs",)); registry.freeze()
    document_registry = DocumentRegistry(tmp_path, "session")
    client = FakeMinerU(tmp_path)

    async def deny(request, cancellation):
        return PermissionDecision.DENY
    denied_executor = ToolExecutor(
        registry.enabled_view(), tmp_path, "session",
        runtime_capabilities={"mineru_client": client, "document_cache": DocumentCache(tmp_path, document_registry)},
        target_authorizer=TargetAuthorizer(tmp_path, requester=deny),
    )
    use = ToolUsePart("read_docs", json.dumps({"path": "sample.pdf", "correction_of_tool_use_id": None}), "denied")
    denied = (await denied_executor.submit_batch(ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()))[0]
    assert denied.failure.code == "permission_denied" and client.calls == 0

    async def allow(request, cancellation):
        return PermissionDecision.ALLOW_ONCE
    allowed_executor = ToolExecutor(
        registry.enabled_view(), tmp_path, "session",
        runtime_capabilities={"mineru_client": client, "document_cache": DocumentCache(tmp_path, document_registry)},
        target_authorizer=TargetAuthorizer(tmp_path, requester=allow),
    )
    use = ToolUsePart("read_docs", json.dumps({"path": "sample.pdf", "correction_of_tool_use_id": None}), "allowed")
    result = (await allowed_executor.submit_batch(ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()))[0]
    assert result.output["metadata"]["cache_hit"] is False and client.calls == 1
    ref = result.output["data"]["document"]
    assert (tmp_path / ref["path"]).read_text() == "# converted"
    assert "batch" not in json.dumps(result.output)
    assert document_registry.targets() == frozenset()

    second = ToolUsePart("read_docs", json.dumps({"path": "sample.pdf", "correction_of_tool_use_id": None}), "cached")
    cached = (await allowed_executor.submit_batch(ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (second,)), Cancellation()))[0]
    assert cached.output["metadata"]["cache_hit"] is True and client.calls == 1


async def test_read_docs_rejects_unsupported_suffix_before_permission(tmp_path):
    (tmp_path / "sample.txt").write_text("text", encoding="utf-8")
    registry = ToolRegistry(available_names=("read_docs",)); registry.freeze()
    executor = ToolExecutor(registry.enabled_view(), tmp_path, "session")
    use = ToolUsePart("read_docs", json.dumps({"path": "sample.txt", "correction_of_tool_use_id": None}), "bad")
    result = (await executor.submit_batch(
        ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()
    ))[0]
    assert result.failure.code == "unsupported_operation"
