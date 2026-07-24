import hashlib
import json
import zipfile

import pytest

from miniagent.documents import DocumentCache, DocumentRegistry
from miniagent.tools.models import ToolExecutionError
from miniagent.tools.read_docs.archive import extract_full_markdown


def test_document_cache_is_session_scoped_and_revalidates_content(tmp_path):
    registry = DocumentRegistry(tmp_path, "session-a")
    cache = DocumentCache(tmp_path, registry)
    source_hash = hashlib.sha256(b"source").hexdigest()
    markdown = tmp_path / "full.md"
    markdown.write_text("# Title\n", encoding="utf-8")
    ref = cache.commit("session-a", source_hash, "pdf", "vlm", markdown)
    assert cache.lookup("session-a", source_hash) == ref
    assert registry.targets() == frozenset()
    assert cache.validate_and_register(ref)
    assert len(registry.targets()) == 1
    (tmp_path / ref.path).write_text("tampered", encoding="utf-8")
    assert cache.lookup("session-a", source_hash) is None
    assert not DocumentRegistry(tmp_path, "session-b").register(ref)


def test_manifest_contains_only_stable_integrity_fields(tmp_path):
    registry = DocumentRegistry(tmp_path, "session")
    cache = DocumentCache(tmp_path, registry)
    source_hash = hashlib.sha256(b"source").hexdigest()
    markdown = tmp_path / "full.md"
    markdown.write_text("body", encoding="utf-8")
    ref = cache.commit("session", source_hash, "docx", "vlm", markdown)
    manifest = json.loads((tmp_path / ref.path).with_name("manifest.json").read_text())
    assert set(manifest) == {
        "schema_version", "source_sha256", "source_type", "model_version",
        "completed_at", "markdown_byte_count", "markdown_sha256",
    }


def test_archive_extracts_unique_utf8_full_md_and_rejects_traversal(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("nested/full.md", "# ok")
        value.writestr("meta.json", "{}")
    extracted = extract_full_markdown(archive, tmp_path)
    assert extracted.read_text(encoding="utf-8") == "# ok"

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as value:
        value.writestr("../full.md", "bad")
    with pytest.raises(ToolExecutionError) as captured:
        extract_full_markdown(unsafe, tmp_path)
    assert captured.value.code.value == "invalid_response"
