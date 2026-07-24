# write_file 工具设计

本文定义内置 `write_file` 的稳定契约。它服从
[`tool-design-guidelines.md`](../tool-design-guidelines.md) 与
[`tool-registry-and-execution.md`](../tool-registry-and-execution.md)。

## 1. 目的与边界

`write_file` 创建一个 UTF-8 文本文件，或在调用者持有当前内容哈希时完整替换已有文件。它只修改一个精确文件，不提供 append、局部编辑、搜索替换、移动、删除或目录创建。

Provider-visible description 固定为：

```text
Create or atomically replace one UTF-8 text file with conflict protection.
```

## 2. ToolInput

```python
class WriteFileInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    path: str
    content: str
    expected_sha256: str | None = None
```

- `path` trim 后不得为空；目标可以尚不存在，但直接父目录必须真实存在；
- `content` 按 UTF-8 编码后最多 256 KiB，不能包含 NUL；
- 工具不添加或删除末尾换行，不转换 LF/CRLF，不规范化 Unicode；
- 默认不添加 BOM；content 显式以 U+FEFF 开头时按 UTF-8 BOM 字节写入；
- `expected_sha256` 若提供，必须是 64 位小写十六进制。

`expected_sha256=None` 表示 create-only：目标已经存在时返回 `CONFLICT`。覆盖已有文件必须提供此前 `read_file.metadata.sha256`；目标不存在或当前原始字节哈希不匹配时同样返回 `CONFLICT`。

## 3. ToolTarget 与执行特征

resolver 产生且只产生一个：

```text
kind=file
capability=write
scope=exact
value=<normalized destination path>
```

resolver 可以按框架规则借最近存在祖先规范化一个尚不存在的目标，但 handler 执行前要求直接父目录存在且是目录。工具不隐式产生 directory target，也不创建缺失父目录。

handler 只使用已授权 target。`write_file` 没有 ArtifactRef、DocumentRef 或 `.mini` 豁免。classifier 固定返回 `concurrency_safe=False`，ToolSpec timeout 为 15 秒，RetryPolicy 为单次 attempt。

## 4. 冲突检查与原子写入

哈希检查和写后哈希都针对磁盘原始字节。覆盖流程在真正提交前再次读取并核对当前哈希；不把文本解码、BOM 或换行规范化纳入比较。

写入使用目标同目录的受控临时文件：完整写入 UTF-8 字节、flush、fsync，确认未取消后通过原子替换提交。create-only 路径必须使用不会覆盖竞态中新建文件的原子创建机制。临时文件名不由模型提供，失败时尽力清理。

首版接受框架已经记录的授权与提交之间 symlink/junction TOCTOU 限制。哈希检查尽量缩短普通外部编辑器竞态，但不宣称提供跨进程文件锁或事务。提交前取消不改变目标；原子提交已经开始后无法确认结果的取消或超时返回 `outcome_unknown`，且绝不自动重放。

## 5. ToolOutput

```python
class WriteFileMetadata(BaseModel):
    path: str
    operation: Literal["created", "replaced"]
    byte_count: int
    sha256: str

class WriteFileData(BaseModel):
    pass

class WriteFileOutput(ToolOutput):
    metadata: WriteFileMetadata
    data: WriteFileData
```

`content` 使用稳定摘要：

```text
Updated miniagent/example.py (1248 bytes, sha256: ...).
```

新建时使用 `Created`。ToolOutput 不回显写入正文，不保存 expected hash、临时路径或原始 input。结果固定较小，使用默认内联 ResultPolicy。

## 6. Prompt

```python
PROMPT = """Purpose:
Create a UTF-8 text file or atomically replace a previously read version.

Use when:
- You need to write the complete contents of one known text file.

Prefer instead:
- Use a future editing tool for partial changes, appends, or search-and-replace operations.
- Use a directory tool when the destination parent directory does not exist.

Rules:
- Omitting `expected_sha256` is create-only and fails if the file already exists.
- To replace a file, read it first and pass the full-file SHA-256 returned by `read_file`.
- Supply the complete desired content; this tool does not merge with existing text.

Returns:
- Whether the file was created or replaced, its byte count, and its new SHA-256.

If it fails:
- Re-read a conflicting file, choose an allowed destination, create the parent directory separately, or reduce oversized content.
"""
```

## 7. 失败与验收不变量

非法 path、content、哈希格式或大小由 Executor 产生 `invalid_arguments`。父目录不可用映射为 `RESOURCE_UNAVAILABLE`；create-only 目标存在、expected hash 缺失语义冲突、目标消失或哈希变化映射为 `CONFLICT`；普通 I/O 失败映射为 `OPERATION_FAILED`。

- schema 只有 path、content 和 expected_sha256；
- create-only 永不覆盖，replace 必须匹配完整原始字节哈希；
- 256 KiB、NUL、BOM、换行和 UTF-8 语义准确；
- 父目录缺失时没有目录或文件副作用；
- 临时文件与目标同目录，成功提交不暴露半写文件；
- workspace、越界、Protected Workspace Subtree 和链接目标遵守统一授权；
- handler 只使用授权 target，始终串行且无自动 retry；
- 成功结果不复制 content、不外置，并返回实际落盘字节的哈希；
- 冲突、取消和 outcome unknown 不会触发隐式重放。

