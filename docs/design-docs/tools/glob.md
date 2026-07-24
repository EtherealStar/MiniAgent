# glob 工具设计

本文定义内置 `glob` 的稳定契约。它服从 [`README.md`](README.md) 的共享文件搜索规则、[`tool-design-guidelines.md`](../tool-design-guidelines.md) 的工具作者约定和 [`tool-registry-and-execution.md`](../tool-registry-and-execution.md) 的执行语义。

## 1. 目的与边界

`glob` 在一个已授权 workspace 目录子树内按受控 glob pattern 发现文件和目录。它只读取目录结构，不读取普通文件内容。

模型已知单个文件且需要内容时应使用 `read_file`；需要按内容查找文件时使用 `grep`。`glob` 不提供分页、任意排序、文件内容过滤、链接跟随或 workspace 外路径访问。

Provider-visible description 固定为：

```text
Find files and directories by glob pattern within a workspace directory.
```

## 2. ToolInput

```python
class GlobInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    pattern: str
    path: str = "."
    kind: Literal["file", "directory", "any"] = "file"
    include_ignored: bool = False
    max_results: int = Field(default=200, ge=1, le=1000)
```

- `pattern` 非空且最长 512 个 Unicode 字符，必须通过共享 glob 编译器；
- `path` 是非空、workspace-relative、真实存在的目录，不能包含父目录跳转；
- `kind=file` 只返回普通文件，`directory` 只返回普通目录，`any` 返回两者；
- 路径模式始终区分大小写，不跟随宿主操作系统行为；
- `correction_of_tool_use_id` 是 Executor 包装字段，不属于 GlobInput。

模式匹配相对于 `path` 的完整路径。`*.py` 只匹配根目录直接子级；`**/*.py` 同时匹配根目录和任意后代；`src/**/test_*.py` 同时匹配 `src/test_a.py` 与 `src/a/test_b.py`。

## 3. ToolTarget 与执行特征

resolver 产生且只产生一个目标：

```text
kind=directory
capability=read
scope=subtree
value=<normalized search root>
```

普通 workspace 目录自动允许。硬排除目录作为显式根目录时在 handler 前返回 `target_denied`。显式 `.mini` 或其后代按 Protected Workspace Subtree 取得 Permission Decision；从普通祖先目录扫描时 walker 直接跳过 `.mini`，不产生 Permission Request。

handler 只使用 `ExecutionContext.targets[0]` 定位根目录，不能从 `args.path` 重建文件系统路径。classifier 是无副作用纯函数并固定返回 `concurrency_safe=True`。ToolSpec timeout 为 20 秒，RetryPolicy 为单次 attempt，ResultPolicy 使用 20 KiB 与 `overflow_behavior=externalize`。

## 4. 遍历与匹配

walker 按规范化相对路径的区分大小写字典序稳定遍历，因此达到上限时返回的前缀可重复。遍历依次应用：硬排除、Protected Workspace Subtree、symlink/junction、`.gitignore`，再应用业务 pattern 与 kind。

`include_ignored=false` 使用 `pathspec` 按 `.gitignore` 所在目录重新定位规则，并在被忽略目录处剪枝。`include_ignored=true` 不应用 `.gitignore`，但其他保护不变。

达到 `max_results` 后立即停止，不扫描剩余目录，也不计算完整总数。`truncated=true` 表示尚未证明没有更多匹配；工具不提供 offset 或 cursor，模型应缩小 `path` 或收紧 `pattern`。

同步目录遍历放入 `asyncio.to_thread()`。walker 在目录和文件边界检查 thread-safe cancellation signal；取消后必须等待线程收束，不能让后台扫描脱离 AgentRun。

## 5. ToolOutput

```python
class GlobMatch(BaseModel):
    path: str
    kind: Literal["file", "directory"]

class GlobMetadata(BaseModel):
    search_root: str
    returned_count: int
    scanned_entry_count: int
    skipped_ignored_count: int
    skipped_protected_count: int
    skipped_symlink_count: int
    truncated: bool

class GlobData(BaseModel):
    matches: list[GlobMatch]

class GlobOutput(ToolOutput):
    metadata: GlobMetadata
    data: GlobData
```

`content` 每行输出一个 workspace-relative 路径；目录显示时以 `/` 结尾，`data.path` 不带结尾 `/`。零结果是成功并返回 `No matching paths found.`。截断时在列表末尾追加 `[Results truncated; narrow path or pattern.]`。

metadata/data 不保存原始 pattern、原始 path 或 ignore 文件内容。完整 GlobOutput 超过 20 KiB 时由 ResultPolicy 外置，handler 不选择 artifact 路径或自行拼装预览。

## 6. Prompt

```python
PROMPT = """Purpose:
Find files or directories by path pattern within a workspace directory. This tool discovers paths and does not read file contents.

Use when:
- You need to locate files or directories by name, extension, or directory structure.

Prefer instead:
- Use `grep` when the selection depends on file contents.
- Use `read_file` when you already know the file and need its contents.

Rules:
- Patterns match complete paths relative to the search root and are case-sensitive.
- Use `**` as a complete path segment for recursive matching.
- Narrow the search root or pattern when a result is truncated.

Returns:
- Workspace-relative paths in stable order. Directories end with `/` in the text result.
- The result states when no path matched or when the result was truncated.

If it fails:
- Correct an invalid path or pattern, or choose an allowed workspace directory.
"""
```

Prompt 不描述 `.git`、缓存目录、`.mini` permission 或 symlink 安全规则；它们由 resolver、Target Authorization 和 walker 保证。

## 7. 失败

非法 schema、path 或 glob pattern 由 Executor 产生 `invalid_arguments`；被禁止或未授权根目录产生目标阶段失败。已授权根目录在执行时整体不可读时抛出 `ToolExecutionError(RESOURCE_UNAVAILABLE, ...)`。单个后代在扫描期间不可读时跳过，不逐项暴露路径或底层异常。

glob 不声明 transient execution retry。超时和取消由 Executor 处理，未知异常不得转换成带诊断细节的普通 ToolFailure。

## 8. 验收不变量

- schema 只有 `pattern`、`path`、`kind`、`include_ignored` 和 `max_results`；
- `*` 不跨目录，`**` 可匹配零层目录，Windows 与 POSIX 结果一致；
- 稳定排序、kind 筛选、上限和 truncated 语义准确；
- 分层 `.gitignore` 与 `include_ignored` 行为准确，硬排除永远不能绕过；
- 从普通祖先跳过 `.mini`，显式 `.mini` 请求 permission；
- symlink/junction 不遍历、不返回，且不能形成 workspace 或保护目录旁路；
- handler 只使用授权 target，classifier 固定并发安全，取消后无遗留线程任务；
- content、metadata、data 与 20 KiB 外置策略符合统一 ToolOutput 契约。
