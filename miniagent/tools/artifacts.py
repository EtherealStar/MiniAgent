from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from miniagent.trace import MemoryTraceSink

from .models import ArtifactRef, ToolProtocolError


class FileArtifactStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve(strict=True)

    def persist(self, session_id: str, tool_use_id: str, content: str) -> ArtifactRef:
        self._validate_segment(session_id, "session_id")
        self._validate_segment(tool_use_id, "tool_use_id")
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        directory = self.workspace_root / ".mini" / "sessions" / session_id / "tool_result" / tool_use_id
        directory.mkdir(parents=True, exist_ok=True)
        result_path = directory / "result.txt"
        metadata_path = directory / "metadata.json"
        if result_path.exists():
            existing = result_path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ToolProtocolError(f"tool_use_id {tool_use_id} 已存在不同 artifact")
            return self._ref(result_path, data, digest)
        metadata = json.dumps(
            {"byte_count": len(data), "sha256": digest, "encoding": "utf-8"},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        # 临时文件与目标文件同目录，确保 replace 在同一文件系统内原子提交。
        self._atomic_write(result_path, data)
        self._atomic_write(metadata_path, metadata)
        return self._ref(result_path, data, digest)

    def _ref(self, path: Path, data: bytes, digest: str) -> ArtifactRef:
        return ArtifactRef(path=path.relative_to(self.workspace_root).as_posix(), byte_count=len(data), sha256=digest)

    @staticmethod
    def _validate_segment(value: str, label: str) -> None:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ToolProtocolError(f"{label} 不是安全的路径段")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
