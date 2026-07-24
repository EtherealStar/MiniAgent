from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, field_validator

from .tools.models import ToolTarget


class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str
    source_sha256: str
    path: str
    byte_count: int
    sha256: str

    @field_validator("source_sha256", "sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("hash must be lowercase SHA-256")
        return value


class DocumentRegistry:
    """Current Session 已提交文档引用的内存投影。"""

    def __init__(self, workspace_root: Path, session_id: str) -> None:
        self.workspace_root = workspace_root.resolve(strict=True)
        self.session_id = session_id
        self._refs: dict[str, DocumentRef] = {}
        self._lock = RLock()

    def register(self, ref: DocumentRef) -> bool:
        if ref.session_id != self.session_id or not self._validate_path(ref):
            return False
        with self._lock:
            self._refs[ref.path] = ref
        return True

    def targets(self) -> frozenset[ToolTarget]:
        with self._lock:
            return frozenset(ToolTarget("file", "read", ref.path) for ref in self._refs.values())

    def _validate_path(self, ref: DocumentRef) -> bool:
        expected = Path(".mini") / "sessions" / ref.session_id / "document_cache" / ref.source_sha256 / "content.md"
        if Path(ref.path).as_posix() != expected.as_posix():
            return False
        try:
            path = self.workspace_root / expected
            path.resolve(strict=True).relative_to(self.workspace_root)
            data = path.read_bytes()
            manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return (
            manifest.get("schema_version") == 1
            and manifest.get("source_sha256") == ref.source_sha256
            and manifest.get("markdown_byte_count") == ref.byte_count
            and manifest.get("markdown_sha256") == ref.sha256
            and len(data) == ref.byte_count
            and hashlib.sha256(data).hexdigest() == ref.sha256
        )


class DocumentCache:
    def __init__(self, workspace_root: Path, registry: DocumentRegistry) -> None:
        self.workspace_root = workspace_root.resolve(strict=True)
        self.registry = registry

    def lookup(self, session_id: str, source_sha256: str) -> DocumentRef | None:
        directory = self._directory(session_id, source_sha256)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            content = (directory / "content.md").read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if (manifest.get("schema_version") != 1 or manifest.get("source_sha256") != source_sha256
                    or manifest.get("markdown_byte_count") != len(content)
                    or manifest.get("markdown_sha256") != digest):
                return None
            return self._ref(session_id, source_sha256, len(content), digest)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def commit(self, session_id: str, source_sha256: str, source_type: str,
               model_version: str, markdown_temp_path: Path) -> DocumentRef:
        directory = self._directory(session_id, source_sha256)
        directory.mkdir(parents=True, exist_ok=True)
        data = markdown_temp_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        manifest = {
            "schema_version": 1, "source_sha256": source_sha256, "source_type": source_type,
            "model_version": model_version, "completed_at": datetime.now(timezone.utc).isoformat(),
            "markdown_byte_count": len(data), "markdown_sha256": digest,
        }
        # 同目录临时文件保证 replace 不跨文件系统；先 fsync 内容，再提交 manifest。
        self._atomic_write(directory / "content.md", data)
        self._atomic_write(directory / "manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        return self._ref(session_id, source_sha256, len(data), digest)

    def validate_and_register(self, ref: DocumentRef) -> bool:
        return self.registry.register(ref)

    def _directory(self, session_id: str, source_sha256: str) -> Path:
        if (Path(session_id).name != session_id or len(source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in source_sha256)):
            raise ValueError("unsafe document cache key")
        return self.workspace_root / ".mini" / "sessions" / session_id / "document_cache" / source_sha256

    def _ref(self, session_id: str, source_sha256: str, count: int, digest: str) -> DocumentRef:
        path = Path(".mini") / "sessions" / session_id / "document_cache" / source_sha256 / "content.md"
        return DocumentRef(session_id=session_id, source_sha256=source_sha256, path=path.as_posix(), byte_count=count, sha256=digest)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
