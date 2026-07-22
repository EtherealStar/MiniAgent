import hashlib
import json

import pytest

from miniagent.tools.artifacts import FileArtifactStore
from miniagent.tools.models import ToolProtocolError


def test_artifact_is_atomic_idempotent_and_hashable(tmp_path):
    store = FileArtifactStore(tmp_path)
    ref = store.persist("session", "call", "中文 content")
    result = tmp_path / ref.path
    assert result.read_text(encoding="utf-8") == "中文 content"
    assert ref.sha256 == hashlib.sha256("中文 content".encode()).hexdigest()
    metadata = json.loads((result.parent / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["byte_count"] == ref.byte_count
    assert not list(result.parent.glob("*.tmp"))
    assert store.persist("session", "call", "中文 content") == ref
    with pytest.raises(ToolProtocolError, match="不同 artifact"):
        store.persist("session", "call", "different")


@pytest.mark.parametrize("value", ["", "..", "a/b", "a\\b"])
def test_artifact_rejects_unsafe_ids(tmp_path, value):
    with pytest.raises(ToolProtocolError):
        FileArtifactStore(tmp_path).persist(value, "call", "x")
