# grep 工具设计

本文定义内置 `grep` 的重置契约。它服从 [`README.md`](README.md) 的共享文件搜索规则、[`tool-design-guidelines.md`](../tool-design-guidelines.md) 的工具作者约定和 [`tool-registry-and-execution.md`](../tool-registry-and-execution.md) 的执行语义。旧 `miniagent/tools/grep/grep.py` 的输入、裸字符串输出和中文错误不构成兼容约束。

## 1. 目的与边界

`grep` 在一个已授权 workspace 目录子树内逐行搜索 UTF-8 文本。它用共享 glob 方言筛选候选文件，但不承担通用路径发现，也不读取一个已知文件的完整内容。

单个具体文件由未来 `read_file` 处理；按名称发现文件使用 `glob`。grep 不支持跨行正则、任意编码、分页、链接跟随或 workspace 外路径。

Provider-visible description 固定为：

```text
Search UTF-8 text files by literal text or regular expression within a workspace directory.
```

## 2. ToolInput

```python
class GrepInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    pattern: str
    path: str = "."
    mode: Literal["regex", "literal"] = "regex"
    include: list[str] = Field(default_factory=list, max_length=20)
    exclude: list[str] = Field(default_factory=list, max_length=20)
    case_sensitive: bool = True
    context_lines: int = Field(default=0, ge=0, le=10)
    include_ignored: bool = False
    max_matches: int = Field(default=100, ge=1, le=1000)
```

- `pattern` 不得为空且最长 4096 个 Unicode 字符；不 trim 合法的首尾空格；
- `mode=regex` 是默认行为，validator 使用 `regex` 包编译；`literal` 必须显式选择；
- `path` 只接受非空、workspace-relative、真实存在的目录；单个文件无效；
- `include` 和 `exclude` 中每个 pattern 非空、最长 512 字符、数组内不得重复；
- include 数组内部是 OR；省略或空数组表示全部候选文件；exclude 数组内部是 OR 且优先于 include；
- include/exclude 相对于 `path` 匹配完整路径并始终区分大小写；
- `case_sensitive` 只控制内容匹配，不改变路径 pattern；
- `max_matches` 以匹配行为单位，不以 occurrence 为单位。

## 3. ToolTarget 与执行特征

resolver 产生且只产生一个 `directory/read/subtree/<normalized search root>` ToolTarget。硬排除和 Protected Workspace Subtree 规则与 [`glob`](glob.md) 相同。handler 只从 `ExecutionContext.targets[0]` 取得根目录，不能从 `args.path` 重新建立路径。

classifier 固定返回 `concurrency_safe=True`。ToolSpec timeout 为 30 秒；RetryPolicy 为单次 attempt；ResultPolicy 使用 20 KiB 与 `overflow_behavior=externalize`。

## 4. 候选文件与文本解码

grep 复用 `_filesystem_search` 的稳定 walker，先执行硬排除、Protected Workspace Subtree、symlink/junction 和 `.gitignore` 规则，再执行 include/exclude。候选普通文件按 workspace-relative 路径的区分大小写字典序扫描。

文件包含 NUL 字节时视为二进制并跳过。文本只接受 UTF-8，可含 UTF-8 BOM；解码失败的文件跳过，不猜测 GBK、系统代码页或其他编码。单个文件读取失败计入 `skipped_unreadable_count` 并继续；搜索根目录整体不可用才是执行失败。

同步遍历和文件读取放入 `asyncio.to_thread()`。扫描在目录、文件和行边界检查 thread-safe cancellation signal；取消后等待线程收束，不能脱离 AgentRun。

## 5. 匹配语义

regex 模式使用 `regex` 包的 Python-compatible regular expression 语义，逐行调用 search，不支持跨行模式。pattern 最长 4096 字符，每次行匹配最多 50ms；单次超时终止整个 ToolUse 并映射为 `DEADLINE_EXCEEDED`，不做 execution retry。

literal 模式使用普通字符串查找，不解释元字符。大小写不敏感时使用 Unicode case folding，并维护折叠文本到原始文本的索引映射，使 span 坐标仍指向原始行。两种模式都收集同一行的非重叠 occurrence，最多保存 100 个 span；更多 occurrence 设置 `spans_truncated=true`。零宽 regex span 合法。

同一行无论 occurrence 数量只计一个 match。文件内按行号、文件间按稳定路径顺序返回。达到 `max_matches` 后立即停止，不计算完整总数并设置 `truncated=true`。

`context_lines` 在每个匹配行前后取对称上下文。同一文件内重叠或相邻区间合并，不同区间用统一分隔符表示。max_matches 只统计匹配行，不统计上下文行。

## 6. 长行窗口

每个输出行最多保存 500 个 Unicode 字符。上下文行保留前 500 字符；匹配行以首个匹配位置为中心选择包含该匹配的窗口，前后用明确省略标记表示被裁切部分。

`window_start_column` 是窗口首字符在原始行中的 1-based 列号。match span 也使用原始行的 1-based、end-exclusive 坐标，因此零宽匹配可以有 `start_column == end_column`。data 不保存完整长行，不能借结构化字段绕过 ResultPolicy。

## 7. ToolOutput

```python
class MatchSpan(BaseModel):
    start_column: int
    end_column: int

class GrepLine(BaseModel):
    line_number: int
    role: Literal["match", "context"]
    text: str
    window_start_column: int
    truncated: bool
    spans: list[MatchSpan]
    spans_truncated: bool

class GrepGroup(BaseModel):
    path: str
    lines: list[GrepLine]

class GrepMetadata(BaseModel):
    search_root: str
    matched_line_count: int
    scanned_file_count: int
    skipped_binary_count: int
    skipped_non_utf8_count: int
    skipped_unreadable_count: int
    skipped_ignored_count: int
    skipped_protected_count: int
    skipped_symlink_count: int
    truncated_line_count: int
    truncated: bool

class GrepData(BaseModel):
    groups: list[GrepGroup]

class GrepOutput(ToolOutput):
    metadata: GrepMetadata
    data: GrepData
```

content 中匹配行以 `>` 标记，上下文行以空格标记，所有行带 workspace-relative path 和 1-based 行号；区间之间用 `--`。零结果为成功并返回 `No matching lines found.`。达到上限时追加 `[Results truncated; narrow the search.]`。

metadata/data 不保存原始 pattern、原始 path、完整长行或被跳过文件的逐项路径。完整 GrepOutput 超过 20 KiB 时交给 ResultPolicy 外置。

## 8. Prompt

```python
PROMPT = """Purpose:
Search UTF-8 text files line by line within a workspace directory. The default pattern mode is regular expression.

Use when:
- You need to find files or lines based on text content.
- You need nearby line context around content matches.

Prefer instead:
- Use `glob` when the selection depends only on path names or extensions.
- Use `read_file` when you already know the file and need its full contents.

Rules:
- The search root must be a directory; this tool does not read a single known file.
- File include and exclude patterns are case-sensitive and use the same glob rules as `glob`.
- Searches are line-based. Narrow the path or patterns when a result is truncated.

Returns:
- Matching lines in stable path and line order, with workspace-relative paths and line numbers.
- Optional context is grouped by file, and the result states when matches or individual lines were truncated.

If it fails:
- Correct an invalid regular expression or glob pattern, narrow an expensive expression, or choose an allowed directory.
"""
```

## 9. 失败

非法 schema、path、regex 或 glob pattern 由 Executor 生成 `invalid_arguments`；目标拒绝发生在 handler 前。已授权根目录整体不可读映射为 `RESOURCE_UNAVAILABLE`，单次 regex 匹配超时映射为 `DEADLINE_EXCEEDED`。两者都使用安全英文 message，不包含底层异常或未经处理路径。

grep 不声明 transient execution retry。取消和工具总 timeout 由 Executor 处理。

## 10. 验收不变量

- schema 只有已声明九个业务字段，mode 默认 regex，path 拒绝单个文件；
- include/exclude 使用共享 glob 编译器，exclude 优先，数组重复项被拒绝；
- 只搜索 UTF-8/UTF-8 BOM，二进制、非 UTF-8、不可读文件按契约计数；
- 匹配以行为单位，span、零宽匹配、大小写折叠和 occurrence 上限坐标准确；
- context 合并、稳定排序、max_matches 和 truncated 行为准确；
- 500 字符窗口始终包含首个匹配，data 不保存完整长行；
- 分层 `.gitignore`、硬排除、`.mini` 和链接规则与 glob 一致；
- 50ms regex timeout、30 秒工具 timeout、协作取消和并发安全分类符合执行契约；
- content、metadata、data 与 20 KiB ArtifactStore 外置互斥符合统一 ToolOutput 契约。
