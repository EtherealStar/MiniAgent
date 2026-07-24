from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import regex
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .._filesystem_search import compile_pattern, walk
from ..models import ExecutionContext, ExecutionTraits, ResultPolicy, ToolOutput, ToolSpec, ToolTarget
from ..policy import resolve_workspace_target

class GrepInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pattern: str
    path: str = "."
    mode: Literal["regex", "literal"] = "regex"
    include: list[str] = Field(default_factory=list, max_length=20)
    exclude: list[str] = Field(default_factory=list, max_length=20)
    case_sensitive: bool = True
    context_lines: int = Field(default=0, ge=0, le=10)
    include_ignored: bool = False
    max_matches: int = Field(default=100, ge=1, le=1000)

    @field_validator("pattern")
    @classmethod
    def pattern_valid(cls, value: str) -> str:
        if not value or len(value) > 4096: raise ValueError("pattern must contain 1..4096 characters")
        return value
    @field_validator("path")
    @classmethod
    def path_valid(cls, value: str) -> str:
        if not value or Path(value).is_absolute(): raise ValueError("path must be a relative directory")
        return value
    @field_validator("include", "exclude")
    @classmethod
    def patterns_valid(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)): raise ValueError("patterns must not contain duplicates")
        for value in values:
            if len(value) > 512: raise ValueError("glob pattern is too long")
            compile_pattern(value)
        return values
    @model_validator(mode="after")
    def regex_valid(self):
        if self.mode == "regex":
            try: regex.compile(self.pattern)
            except regex.error as exc: raise ValueError("pattern is not a valid regular expression") from exc
        return self

class MatchSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_column: int; end_column: int
class GrepLine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    line_number: int; role: Literal["match", "context"]; text: str; window_start_column: int; truncated: bool; spans: list[MatchSpan]; spans_truncated: bool
class GrepGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str; lines: list[GrepLine]
class GrepMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    search_root: str; matched_line_count: int; scanned_file_count: int; skipped_binary_count: int; skipped_non_utf8_count: int; skipped_unreadable_count: int; skipped_ignored_count: int; skipped_protected_count: int; skipped_symlink_count: int; truncated_line_count: int; truncated: bool
class GrepData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    groups: list[GrepGroup]
class GrepOutput(ToolOutput):
    metadata: GrepMetadata; data: GrepData

def resolve_targets(args: BaseModel, workspace_root: Path):
    assert isinstance(args, GrepInput)
    resolved, target = resolve_workspace_target(workspace_root, args.path)
    if not resolved.is_dir(): raise ValueError("path must name a directory")
    return (ToolTarget("directory", "read", target.value, scope="subtree"),)
def classify(args, targets): return ExecutionTraits(concurrency_safe=True)

def _literal_spans(line: str, needle: str, sensitive: bool) -> list[tuple[int,int]]:
    hay, find = (line, needle) if sensitive else (line.casefold(), needle.casefold())
    spans=[]; pos=0
    while len(spans) < 100:
        found=hay.find(find,pos)
        if found < 0: break
        # casefold 可能扩展字符；用前缀折叠长度映射回原文列。
        start=next((i for i in range(len(line)+1) if len(line[:i].casefold()) >= found), len(line))
        end=next((i for i in range(start,len(line)+1) if len(line[:i].casefold()) >= found+len(find)), len(line))
        spans.append((start,end)); pos=found+max(1,len(find))
    return spans

def _scan(args: GrepInput, context: ExecutionContext) -> GrepOutput:
    root=(context.workspace_root/context.targets[0].value).resolve()
    includes=[compile_pattern(p) for p in args.include]; excludes=[compile_pattern(p) for p in args.exclude]
    flags=0 if args.case_sensitive else regex.IGNORECASE|regex.FULLCASE
    compiled=regex.compile(args.pattern, flags) if args.mode=="regex" else None
    groups=[]; matched=scanned=binary=bad_utf8=unreadable=truncated_lines=0; truncated=False
    for rel,path,is_dir in walk(root,context.workspace_root,include_ignored=args.include_ignored,explicit_mini=root.name==".mini",cancellation=context.cancellation):
        if is_dir: continue
        local=path.relative_to(root).as_posix()
        if includes and not any(p.fullmatch(local) for p in includes): continue
        if any(p.fullmatch(local) for p in excludes): continue
        try: raw=path.read_bytes()
        except OSError: unreadable+=1; continue
        if b"\0" in raw: binary+=1; continue
        try: text=raw.decode("utf-8-sig")
        except UnicodeDecodeError: bad_utf8+=1; continue
        scanned+=1; source=text.splitlines(); match_indices=[]; span_map={}
        for i,line in enumerate(source):
            context.cancellation.raise_if_cancelled()
            try:
                spans=[m.span() for m in compiled.finditer(line, timeout=.05)][:100] if compiled else _literal_spans(line,args.pattern,args.case_sensitive)
            except TimeoutError: raise RuntimeError("Regular expression evaluation timed out.")
            if spans: match_indices.append(i); span_map[i]=spans; matched+=1
            if matched>=args.max_matches: truncated=True; break
        if match_indices:
            indices=sorted({j for i in match_indices for j in range(max(0,i-args.context_lines),min(len(source),i+args.context_lines+1))})
            lines=[]
            for i in indices:
                original=source[i]; spans=span_map.get(i,[]); start=0
                if len(original)>500 and spans: start=max(0,min(spans[0][0]-200,len(original)-500))
                shown=original[start:start+500]; was=len(original)>500; truncated_lines+=int(was)
                lines.append(GrepLine(line_number=i+1,role="match" if i in span_map else "context",text=shown,window_start_column=start+1,truncated=was,spans=[MatchSpan(start_column=a+1,end_column=b+1) for a,b in spans],spans_truncated=len(spans)>=100))
            groups.append(GrepGroup(path=rel,lines=lines))
        if truncated: break
    content_lines=[]
    for group in groups:
        for line in group.lines: content_lines.append(f"{'>' if line.role=='match' else ' '} {group.path}:{line.line_number}:{line.text}")
    content="\n".join(content_lines) if content_lines else "No matching lines found."
    if truncated: content+="\n[Results truncated; narrow the search.]"
    metadata=GrepMetadata(search_root=context.targets[0].value,matched_line_count=matched,scanned_file_count=scanned,skipped_binary_count=binary,skipped_non_utf8_count=bad_utf8,skipped_unreadable_count=unreadable,skipped_ignored_count=0,skipped_protected_count=0,skipped_symlink_count=0,truncated_line_count=truncated_lines,truncated=truncated)
    return GrepOutput(content=content,metadata=metadata,data=GrepData(groups=groups))

async def handler(args: BaseModel, context: ExecutionContext) -> GrepOutput:
    assert isinstance(args,GrepInput)
    task=asyncio.create_task(asyncio.to_thread(_scan,args,context))
    try: return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task; raise

grep_spec=SPEC=ToolSpec(name="grep",description="Search UTF-8 text files by literal text or regular expression within a workspace directory.",input_model=GrepInput,output_model=GrepOutput,handler=handler,prompt_ref="miniagent.tools.grep.prompt:PROMPT",resolve_targets=resolve_targets,classify=classify,timeout_seconds=30.0,result_policy=ResultPolicy(threshold_bytes=20*1024,hard_limit_bytes=50*1024))
