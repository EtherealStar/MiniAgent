# web_search 工具设计

本文定义内置 `web_search` 的稳定契约。它服从 [`tool-design-guidelines.md`](../tool-design-guidelines.md) 与 [`tool-registry-and-execution.md`](../tool-registry-and-execution.md)。

## 1. 目的与边界

`web_search` 使用 Tavily 官方 Python SDK 执行普通 Web 搜索。它返回来源标题、URL 和 snippet，供 Agent 自行综合；它不抓取网页全文、不生成 Tavily answer、不返回图片，也不开放任意 URL 请求。

本工具明确绑定 Tavily，不定义供应商无关的 WebSearchClient，也不把 Tavily 的高级参数投影给模型。

Provider-visible description 固定为：

```text
Search the public web with Tavily and return concise source results.
```

## 2. 配置与可用性

配置入口固定为：

```text
TAVILY_API_KEY=tvly-...
```

composition root 按项目既有规则从进程环境和 workspace `.env` 加载，进程环境优先。API key 在配置对象中 `repr=False`，不得进入 ToolInput、ToolOutput、Permission Request、Message Journal、Trace Record、artifact 或异常文本。

未配置或 trim 后为空时，composition root 不把 `web_search` 加入 ToolRegistry；schema 与 Prompt 对模型完全不可见。配置修改后重启应用生效，不做运行时热加载。非空但无效的 key 允许完成注册，实际认证失败按执行失败处理。

composition root 使用 API key 构造 Tavily 官方 client，并作为不可变 runtime capability 注入 ExecutionContext。handler 取得具体 Tavily client，不读取全局环境或 `.env`。这只是通用 runtime capability 传递机制，不把工具契约抽象为多供应商搜索端口。

## 3. ToolInput 与固定搜索参数

```python
class WebSearchInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    query: str
```

query trim 后不得为空，最长 400 个 Unicode 字符。规范化 query 原样传给 Tavily，不自动附加日期、语言、站点或隐藏关键词。

Provider-visible schema 不包含结果数、search depth、topic、domain、时间范围或任何 Tavily 高级参数。SDK 调用固定最多返回 5 条，并固定：

```text
include_answer=false
include_raw_content=false
include_images=false
```

其余搜索策略使用项目锁定 Tavily SDK 版本的普通默认值。SDK 升级若改变结果语义，必须重新审查本文，不能把新参数自动暴露给模型。

## 4. ToolTarget、执行与重试

resolver 产生一个固定目标：

```text
kind=external_service
capability=read
scope=exact
value=api.tavily.com
```

只有配置 `TAVILY_API_KEY` 后该 target 与工具才会启用。启用配置本身构成该固定服务的部署授权，不发起每个 Session 的 Permission Request。模型不能传入或改变 host。

classifier 固定返回 `concurrency_safe=False`；网络调用在工具批次中形成串行屏障。ToolSpec timeout 为 30 秒。SDK 层请求 timeout 必须短于工具总 timeout，不能只依靠 Executor 外层取消。

优先使用锁定 SDK 版本的官方异步 client。若该版本没有可靠异步接口，允许把 `TavilyClient.search()` 放入 `asyncio.to_thread()`，但取消时必须等待线程和底层有界请求收束，不能让网络工作脱离 AgentRun。

RetryPolicy 最多 2 次 attempt。只有连接失败、明确临时的服务端错误或确定未完成的请求 timeout 映射为 transient `RESOURCE_UNAVAILABLE` 并允许第二次 attempt。认证失败、额度不足、限流、非法请求、无效响应和取消不自动重试。搜索是只读操作，可以安全重放，但安全重放不等于所有失败都应重试。

## 5. 响应规范化

工具只消费 Tavily response 中结果列表所需的 title、url、content/snippet 和明确提供的 published date。不得把 raw response 放入 output、Trace 或错误。

每条候选结果按以下顺序处理：

1. URL 必须能解析为 `http` 或 `https`，host 非空，长度不超过 2048；否则丢弃；
2. 规范化 scheme/host 大小写、默认端口和 fragment 后按 URL 去重，保留 Tavily 排名最高的一条；
3. title 为空时使用 `Untitled result`，异常超长或非法类型的结果丢弃；
4. snippet 为空时保留结果并显示 `No snippet available.`；超过 1000 个 Unicode 字符时截断并设置标志；
5. `published_at` 只有 Tavily 明确返回可接受字符串时保留，不从 snippet 猜测日期；
6. 保持 Tavily 相关性顺序，最多输出 5 条，不按标题、URL 或日期重新排序。

Tavily relevance score 不进入 content 或 data；它是供应商内部排序信号，不构成稳定工具契约。

## 6. ToolOutput

```python
class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    published_at: str | None
    snippet_truncated: bool

class WebSearchMetadata(BaseModel):
    returned_count: int
    dropped_invalid_count: int
    deduplicated_count: int
    truncated_snippet_count: int

class WebSearchData(BaseModel):
    results: list[WebSearchResult]

class WebSearchOutput(ToolOutput):
    metadata: WebSearchMetadata
    data: WebSearchData
```

content 使用稳定编号格式：

```text
[1] Result title
URL: https://example.com/page
Snippet: Concise result snippet.
Published: 2026-07-24
```

没有 published_at 时省略 Published 行。结果间有一个空行。零结果是成功并返回 `No search results found.`。

output 不回显 query，不保存 API key、Tavily score、raw response、SDK 请求参数或响应头。结果规模固定较小，ResultPolicy 使用系统默认值。

## 7. Prompt

```python
PROMPT = """Purpose:
Search the public web and return concise source results from Tavily.

Use when:
- You need current or externally published information that is not available in the workspace or conversation.

Prefer instead:
- Use workspace tools when the answer depends on local files.
- Use a dedicated web page reader when you need the full contents of a known URL.

Rules:
- Write a focused search query. Refine the query in a new call if the results are insufficient.
- Treat snippets as search summaries and use the returned URLs as sources; this tool does not read full pages.

Returns:
- Up to five ranked results with title, HTTP(S) URL, snippet, and an optional published date.
- No result is a successful search outcome, not an execution failure.

If it fails:
- Retry with a revised query only for a valid search that returned insufficient results; configuration, authentication, quota, or service failures require their indicated recovery.
"""
```

## 8. 失败

query schema 错误由 Executor 产生 `invalid_arguments`。Tavily 执行失败只使用框架级 ExecutionErrorCode：

- 无效 API key：`AUTHENTICATION_FAILED`；
- 额度耗尽：`QUOTA_EXCEEDED`；
- 限流：`RATE_LIMITED`；
- 临时网络或服务故障：`RESOURCE_UNAVAILABLE`；
- SDK response 不符合预期结构：`INVALID_RESPONSE`；
- 其他可预期搜索失败：`OPERATION_FAILED`。

safe_message 使用英文且不得包含 key、SDK 异常 repr、response body、headers、query 或环境值。未知实现异常属于内部错误，不伪装成普通 ToolFailure。

## 9. 验收不变量

- schema 只有 `query`；固定五条、关闭 answer/raw content/images，不暴露 Tavily 高级参数；
- 未配置 key 时工具、schema 和 Prompt 同时不可见，配置后固定 external_service target 自动允许；
- handler 只使用 ExecutionContext 中注入的具体 Tavily client，不读取全局 secret；
- classifier 串行，SDK timeout 小于 30 秒，只有 transient 资源不可用允许第二次 attempt；
- URL scheme 校验、规范化去重、稳定排名、snippet 截断和零结果行为准确；
- content、metadata、data 不包含 query、score、raw response、secret 或未经处理异常；
- 测试只使用 fake Tavily client 或 `httpx.MockTransport`，不访问真实网络或用户凭据。
