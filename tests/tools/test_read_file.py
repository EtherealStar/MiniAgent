import hashlib
import json
import uuid
from datetime import datetime, timezone

from miniagent.documents import DocumentCache
from miniagent.domain import Message, Role, ToolExecutionBatch, ToolResultPart, ToolUsePart
from miniagent.journal import JournalRecord, JournalRecordType, UserMessagePayload
from miniagent.ports import Cancellation
from miniagent.repository import SessionRepository
from miniagent.session import SessionEngine
from miniagent.tools.authorization import TargetAuthorizer
from miniagent.tools.executor import ToolExecutor
from miniagent.tools.registry import ToolRegistry


async def test_committed_document_ref_is_exactly_readable_and_recovers(tmp_path):
    session_id, run_id = uuid.uuid4(), uuid.uuid4()
    user = Message.text(Role.USER, "convert")
    repository = SessionRepository(tmp_path / ".mini" / "sessions")
    opened = await repository.create_session(
        session_id,
        JournalRecord(1, JournalRecordType.USER_MESSAGE, session_id, run_id,
                      datetime.now(timezone.utc), UserMessagePayload(user)),
    )
    engine = SessionEngine(opened)
    document_registry = engine.ensure_document_registry(tmp_path)
    cache = DocumentCache(tmp_path, document_registry)
    source_hash = hashlib.sha256(b"source").hexdigest()
    markdown = tmp_path / "converted.md"
    markdown.write_text("first\nsecond\n", encoding="utf-8")
    ref = cache.commit(str(session_id), source_hash, "pdf", "vlm", markdown)
    assert document_registry.targets() == frozenset()

    assistant = Message(
        role=Role.ASSISTANT,
        parts=(ToolUsePart("read_docs", "{}", "convert-call"),),
    )
    await engine.commit_assistant(run_id, assistant, "tool_calls")
    output = {
        "content": "converted",
        "metadata": {
            "source_type": "pdf", "cache_hit": False, "model_version": "vlm",
            "markdown_byte_count": ref.byte_count, "markdown_sha256": ref.sha256,
        },
        "data": {"document": ref.model_dump(mode="json")},
    }
    tool_message = Message(
        role=Role.TOOL,
        parts=(ToolResultPart(
            "convert-call", assistant.message_id, "converted", tool_name="read_docs", output=output,
        ),),
    )
    await engine.commit_tool_result(run_id, tool_message)
    assert len(document_registry.targets()) == 1

    registry = ToolRegistry(available_names=("read_file",)); registry.freeze()
    executor = ToolExecutor(
        registry.enabled_view(), tmp_path, str(session_id),
        target_authorizer=TargetAuthorizer(tmp_path, controlled_read_targets=document_registry.targets),
    )
    use = ToolUsePart("read_file", json.dumps({
        "path": ref.path, "offset": 0, "limit": 1, "correction_of_tool_use_id": None,
    }), "read")
    result = (await executor.submit_batch(
        ToolExecutionBatch(uuid.uuid4(), uuid.uuid4(), (use,)), Cancellation()
    ))[0]
    assert result.content == "1 | first"
    assert result.output["metadata"]["has_more"] is True

    await engine.close()
    reopened = await repository.open_session(session_id)
    restored = SessionEngine(reopened)
    restored_registry = restored.ensure_document_registry(tmp_path)
    assert len(restored_registry.targets()) == 1
    await restored.close()


async def test_uncommitted_cross_session_and_tampered_refs_are_not_granted(tmp_path):
    from miniagent.documents import DocumentRegistry

    source_hash = hashlib.sha256(b"source").hexdigest()
    registry_a = DocumentRegistry(tmp_path, "a")
    cache = DocumentCache(tmp_path, registry_a)
    markdown = tmp_path / "converted.md"
    markdown.write_text("body", encoding="utf-8")
    ref = cache.commit("a", source_hash, "pdf", "vlm", markdown)
    assert not DocumentRegistry(tmp_path, "b").register(ref)
    (tmp_path / ref.path).write_text("tampered", encoding="utf-8")
    assert not registry_a.register(ref)
