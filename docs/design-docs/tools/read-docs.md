# read_docs 工具设计

本文定义内置 `read_docs` 的稳定契约。它服从
[`tool-design-guidelines.md`](../tool-design-guidelines.md) 与
[`tool-registry-and-execution.md`](../tool-registry-and-execution.md)。它把受支持的本地文档交给 MinerU 转换为当前 Session 的受控 Markdown `DocumentRef`；正文读取继续由 `read_file` 承担。

## 1. 目的与边界

`read_docs` 支持 `.pdf`、`.doc` 和 `.docx`，封装 MinerU 的上传、异步解析、轮询和结果下载。它不把完整文档正文放入 ToolOutput，不读取生成 Markdown 的页面，不处理 PowerPoint、电子表格、图片或普通 UTF-8 文本。

Provider-visible description 固定为：

```text
Convert a PDF or Word document to a session-scoped Markdown document with MinerU.
```

## 2. 配置与可用性

配置入口固定为：

```text
MINERU_API_TOKEN=...
```

项目配置加载器在应用启动时从既有 `.env` 来源读取 token。composition root 构造具体 `MinerUClient` 和 `DocumentCache` capability 并注入 ExecutionContext。handler 不读取 `.env`、进程环境或全局 client。

token trim 后为空或缺失时，composition root 不注册 `read_docs`，其 schema 与 Prompt 对模型不可见。token 不进入 input/output、Permission Request、Journal、Trace、cache manifest、错误或 repr。配置修改后重启生效。

MinerU model version 固定为 `vlm`，batch 上传 URL 申请入口固定为 `https://mineru.net/api/v4/file-urls/batch`，使用 Bearer token。对应的上传、状态和下载协议由 `MinerUClient` 封装，不进入 Provider schema。

## 3. ToolInput 与文档类型

```python
class ReadDocsInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    path: str
```

path trim 后不得为空，必须解析为真实存在的普通文件。后缀不区分大小写，首版白名单只有 `.pdf`、`.doc` 和 `.docx`。MIME 或 magic sniffing 只用于拒绝与后缀明显冲突的内容，不能扩大白名单。未知后缀在上传前以 `UNSUPPORTED_OPERATION` 失败。

## 4. ToolTarget、外传许可与执行

首次解析需要两个目标：

```text
file/read/exact/<normalized source document>
external_service/write/exact/mineru.net
```

`external_service/write` 表示把本地内容传给外部服务并创建远程任务，不因服务已配置而自动允许。Target Authorization 必须在上传前展示完整多目标 Permission Request；`allow_once` 或 `allow_session` 继续使用统一语义。模型不能改变 MinerU host。

目标规划对重复调用采取保守语义：`read_docs` 是可能产生外传的命令，因此即使 DocumentCache 最终命中，ToolUse 仍声明 MinerU write target；选择 `allow_session` 可以避免同一 Current Session 的重复提示。正常分页不重复调用 `read_docs`，而是使用返回的 DocumentRef 调用 `read_file`。

classifier 固定返回 `concurrency_safe=False`。ToolSpec 使用单次 attempt，工具总 timeout 为 5 分钟，HTTP 请求、上传和轮询单步 timeout 都必须更短。创建 batch 后的 timeout、取消或连接结果不确定不自动重放；下次显式调用重新创建任务。

## 5. MinerU 工作流

MinerUClient 在一次 handler 内封装：

1. 申请 batch 文件上传 URL，固定 model_version=vlm；
2. 将已授权源文件上传到预签名 URL；
3. 有界轮询 batch 状态；
4. 下载并校验完成结果；
5. 提取 Markdown，拒绝路径穿越、链接逃逸和超出解包预算的归档；
6. 通过 DocumentCache 原子提交 `content.md` 与安全 manifest；
7. 登记 Current Session 的 DocumentRef。

handler 协作响应取消。取消或失败时不得留下可登记的半成品 DocumentRef。超时 batch 不恢复，不在 manifest 保存 batch_id、上传 URL 或未完成状态；下一次调用重新创建任务。

## 6. DocumentCache 与 DocumentRef

完成产物布局为：

```text
.mini/sessions/<session_id>/document_cache/<source_sha256>/
  content.md
  manifest.json
```

DocumentCache 是受控 runtime capability，负责路径生成、同目录临时文件、原子提交和引用登记。缓存 key 是源文档原始字节 SHA-256；同一 Session 的相同内容可以命中完成缓存，跨 Session 不共享。

manifest 只保存 schema 版本、源 SHA-256、源类型、MinerU model version、完成时间和 content.md 的字节数/哈希。它不保存 token、batch_id、预签名 URL、原始 API response、源文档正文或任意外部路径。

`DocumentRef` 只指向已完成的 `content.md`，绑定 session_id、源哈希、Markdown 相对路径、字节数和 SHA-256。它随成功 ToolOutput 结构化持久化，Session 恢复时可重建只读受控引用索引；缓存缺失或哈希错误时引用失效并要求重新运行 `read_docs`。

## 7. ToolOutput

```python
class ReadDocsMetadata(BaseModel):
    source_type: Literal["pdf", "doc", "docx"]
    cache_hit: bool
    model_version: Literal["vlm"]
    markdown_byte_count: int
    markdown_sha256: str

class ReadDocsData(BaseModel):
    document: DocumentRef

class ReadDocsOutput(ToolOutput):
    metadata: ReadDocsMetadata
    data: ReadDocsData
```

content 只返回简短说明和后续调用方式：

```text
Document converted to Markdown. Use `read_file` with path `<controlled path>`, offset, and limit to read it.
```

ToolOutput 不包含 Markdown 正文、MinerU response、batch id 或 upload URL，因此使用默认内联 ResultPolicy且不得触发外置。

## 8. Prompt

```python
PROMPT = """Purpose:
Convert a PDF or Word document to a session-scoped Markdown document with MinerU.

Use when:
- You need to read a known `.pdf`, `.doc`, or `.docx` file.

Prefer instead:
- Use `read_file` for UTF-8 text or for the Markdown DocumentRef returned by this tool.
- Use a spreadsheet or image tool for other document types.

Rules:
- The source document is uploaded to MinerU and requires an external data-transfer permission decision.
- After conversion, call `read_file` on the returned controlled Markdown path to page through the contents.

Returns:
- A completed DocumentRef, source type, cache status, and Markdown integrity metadata; it does not return the document text.

If it fails:
- Choose a supported document, allow the external upload, fix MinerU configuration, or call the tool again after a timeout.
"""
```

## 9. 失败与验收不变量

- 非法 path 或后缀在上传前失败；
- token 缺失时工具不可见，无效 token 映射为 `AUTHENTICATION_FAILED`；
- quota、rate limit、服务不可用、无效响应和超时分别使用框架级错误码与安全英文 message；
- 文件 read 与 MinerU write 目标整体授权，拒绝时不读取正文、不上传、不创建 cache；
- timeout 或取消不恢复 batch、不自动 retry、不登记半成品 DocumentRef；
- 解包拒绝路径穿越、链接、异常数量和大小，不信任远端文件名；
- 完成缓存按 Session 与源 SHA-256 隔离，manifest 不含 secret 或原始响应；
- ToolOutput 小且内联，正文只由后续 `read_file` 返回；
- 当前 Session 的有效 DocumentRef 可豁免 `.mini` read，伪造路径和其他 cache 文件不能；
- 测试使用 fake client 或 `httpx.MockTransport`，不访问真实网络、凭据或用户文件。
