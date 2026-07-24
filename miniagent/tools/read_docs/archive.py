from __future__ import annotations

import stat
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from ..models import ExecutionErrorCode, ToolExecutionError

MAX_MEMBERS = 4096
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_MARKDOWN_SIZE = 256 * 1024 * 1024


def extract_full_markdown(archive_path: Path, output_directory: Path) -> Path:
    target: Path | None = None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS or sum(item.file_size for item in infos) > MAX_TOTAL_SIZE:
                raise ToolExecutionError("MinerU archive exceeds the extraction budget.", code=ExecutionErrorCode.RESOURCE_EXHAUSTED)
            normalized: set[str] = set()
            candidates = []
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (not name or "\x00" in name or "\\" in name or path.is_absolute()
                        or (path.parts and path.parts[0].endswith(":"))
                        or ".." in path.parts or stat.S_ISLNK(mode)):
                    raise ToolExecutionError("MinerU returned an unsafe archive.", code=ExecutionErrorCode.INVALID_RESPONSE)
                canonical = path.as_posix()
                if canonical in normalized:
                    raise ToolExecutionError("MinerU returned an ambiguous archive.", code=ExecutionErrorCode.INVALID_RESPONSE)
                normalized.add(canonical)
                if path.name == "full.md":
                    candidates.append(info)
            if len(candidates) != 1 or candidates[0].file_size > MAX_MARKDOWN_SIZE:
                raise ToolExecutionError("MinerU archive does not contain one valid full.md.", code=ExecutionErrorCode.INVALID_RESPONSE)
            fd, temp_name = tempfile.mkstemp(prefix="full-", suffix=".md", dir=output_directory)
            os.close(fd)
            Path(temp_name).unlink(missing_ok=True)
            target = Path(temp_name)
            total = 0
            with archive.open(candidates[0]) as source, target.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_MARKDOWN_SIZE:
                        raise ToolExecutionError("MinerU Markdown exceeds the extraction budget.", code=ExecutionErrorCode.RESOURCE_EXHAUSTED)
                    destination.write(chunk)
            target.read_text(encoding="utf-8")
            return target
    except ToolExecutionError:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        if target is not None:
            target.unlink(missing_ok=True)
        raise ToolExecutionError("MinerU returned an invalid archive.", code=ExecutionErrorCode.INVALID_RESPONSE) from exc
