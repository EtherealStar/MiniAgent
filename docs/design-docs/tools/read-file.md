# read_file 工具设计

本文定义内置 `read_file` 的稳定契约。它服从
[`tool-design-guidelines.md`](../tool-design-guidelines.md) 与
[`tool-registry-and-execution.md`](../tool-registry-and-execution.md)。

## 1. 目的与边界

`read_file` 分页读取一个已授权 UTF-8 文本文件。它用于模型已经知道确切路径、需要查看文件内容的场景，也是读取当前 Session 已提交 `ArtifactRef` 和 `DocumentRef` 的统一入口。

`read_file` 不发现路径、不搜索内容、不解析 PDF 或 Word 文档、不猜测编码，也不读取目录。文档转换使用 `read_docs`，路径发现使用 `glob`，内容搜索使用 `grep`。

Provider-visible description 固定为：

```text
Read a UTF-8 text file by line range with stable line numbers.
```

## 2. ToolInput

```python
class ReadFileInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=2000)
```

- `path` trim 后不得为空，必须解析为真实存在的普通文件或当前 Session 的受控引用；
- `offset` 是从文件开头跳过的行数，因此 `offset=0` 从第 1 行开始；
- `limit` 是本次最多返回的行数；
- `correction_of_tool_use_id` 是 Executor 包装字段，不属于 `ReadFileInput`；
- offset 位于文件末尾或之后时返回成功的空页，不伪装成文件不存在。

按行分页只控制选择范围，最终结果仍受第 5 节的字节和 Token 双重预算约束。超长单行可能使 `limit=1` 仍然超限；工具不得截断该行或把结果外置。

## 3. ToolTarget 与受控引用

普通路径解析为一个目标：

```text
kind=file
capability=read
scope=exact
value=<normalized file path>
```

当前 Session 已提交 ToolResult 中的 `ArtifactRef` 解析为 `artifact/read/exact`；当前 Session 已登记的 `DocumentRef` 解析为 `document/read/exact`。这两类受控引用即使位于 Protected Workspace Subtree `.mini/` 中也自动允许，因为路径由 ArtifactStore 或 DocumentCache 生成并已绑定 Current Session。

豁免只覆盖引用指向的精确 `result.json` 或 `content.md`。模型手写的相似 `.mini` 路径、其他 Session 的引用、`message.jsonl`、trace、metadata、MinerU 原始下载文件和图片都不获得豁免。

handler 只从 `ExecutionContext.targets[0]` 取得已授权路径。classifier 固定返回 `concurrency_safe=True`。ToolSpec timeout 为 15 秒，RetryPolicy 为单次 attempt。

## 4. 文本、分页与哈希

文件只接受 UTF-8 或 UTF-8 BOM。包含 NUL 字节的文件视为二进制；解码失败时不猜测 GBK、系统代码页或其他编码。两者都以 `UNSUPPORTED_OPERATION` 失败，并建议模型选择 `read_docs` 或其他合适工具。

工具识别 LF、CRLF、CR 和 mixed 换行，输出统一使用 LF。每个返回行使用文件中的 1-based 真实行号：

```text
1 | from __future__ import annotations
2 |
3 | class ToolTarget:
```

行号前缀是模型展示的一部分，不属于原始文件正文。工具不在 `data` 中保存一份无行号原文。

`sha256` 始终基于完整文件的原始磁盘字节计算，不受 offset、limit、BOM 或换行展示规范化影响。该值供 `write_file.expected_sha256` 使用。同步读取、解码和哈希放入 `asyncio.to_thread()`，并在可重复的块与行边界检查 thread-safe cancellation signal；取消后等待线程收束。

## 5. 结果自限流

`read_file` 不允许触发 ToolResult 外置。它声明：

```python
ResultPolicy(
    max_inline_bytes=256 * 1024,
    overflow_behavior="error",
    max_model_tokens=25_000,
)
```

ResultPolicy 对完整规范 ToolOutput JSON 计算 UTF-8 字节数，对模型可见 `content` 使用当前 AgentRun 冻结的 tokenizer 计算 Token。任一预算达到或超过上限时，Executor 返回 `RESOURCE_EXHAUSTED`，不提交部分正文、不创建 ArtifactRef，并用安全英文提示缩小 `limit` 或推进 `offset`。

默认 50 KiB 外置阈值只适用于未显式配置 ResultPolicy 的工具，不能覆盖 `read_file` 的 `error` 行为。测试必须证明 `read_file` 的每个成功结果都内联。

## 6. ToolOutput

```python
class ReadFileMetadata(BaseModel):
    path: str
    sha256: str
    source_byte_count: int
    returned_byte_count: int
    returned_token_count: int
    newline: Literal["lf", "crlf", "cr", "mixed", "none"]
    offset: int
    limit: int
    start_line: int | None
    end_line: int | None
    returned_line_count: int
    next_offset: int
    has_more: bool

class ReadFileData(BaseModel):
    pass

class ReadFileOutput(ToolOutput):
    metadata: ReadFileMetadata
    data: ReadFileData
```

`content` 是带行号的当前页。空文件返回 `File is empty.`；offset 位于 EOF 之后返回 `No lines available at this offset.`。`next_offset` 等于 offset 加实际返回行数；`has_more` 只表示当前页之后仍有文件行。

metadata 不保存原始输入 path、完整无行号正文或未经授权的真实路径。普通 workspace 文件显示 workspace-relative 路径；越界文件使用 Target Authorization 提供的安全展示值。

## 7. Prompt

```python
PROMPT = """Purpose:
Read a known UTF-8 text file by line range and show stable line numbers.

Use when:
- You know the exact file path and need its contents.
- You need to page through a committed tool result or converted document.

Prefer instead:
- Use `glob` when you need to discover a path.
- Use `grep` when you need to search file contents.
- Use `read_docs` to convert a PDF or Word document before reading it.

Rules:
- `offset` is the number of lines to skip and `limit` is the maximum number of lines to return.
- Use the returned `next_offset` to continue reading.
- Reduce the line range if the result exceeds the inline byte or token budget.

Returns:
- UTF-8 text with 1-based file line numbers, page metadata, and the full-file SHA-256.
- The result states whether more lines remain and is never externalized.

If it fails:
- Choose a supported text file, correct the path, or request a smaller line range.
"""
```

## 8. 失败与验收不变量

非法 schema 由 Executor 产生 `invalid_arguments`。目标不存在或整体不可读映射为 `RESOURCE_UNAVAILABLE`；目录、二进制或非 UTF-8 输入映射为 `UNSUPPORTED_OPERATION`；结果预算映射为 `RESOURCE_EXHAUSTED`。错误使用安全英文，不包含底层异常、原始文件内容或未经处理路径。

- schema 只有 `path`、`offset` 和 `limit`；
- 行号、offset、limit、next_offset、EOF 和 mixed newline 语义稳定；
- SHA-256 覆盖完整原始字节且可用于 `write_file` 冲突检查；
- 普通路径、越界路径、ArtifactRef、DocumentRef 和伪造 `.mini` 路径分别遵守授权规则；
- UTF-8 BOM 可读，NUL 与无效 UTF-8 被拒绝；
- 超长单行与普通多行结果在 256 KiB/25,000-token 边界上整体失败而不截断；
- classifier 并发安全，取消后无遗留线程；
- 所有成功结果都内联，任何路径都不会生成新的 ArtifactRef。

