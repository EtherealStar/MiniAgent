# read_docs / web_search 官方 API 研究记录

> 文档性质：API research notes，不是实现计划，也不修改稳定工具契约。  
> 核验日期：2026-07-24。供应商契约可能变化，实现前应再次检查链接。  
> 研究范围：MinerU 精准解析 API、Tavily Search REST API 与 Python SDK。未读取 `docs/design-docs/exec-plans/`。

## 1. 与仓库命名的对应关系

仓库现有工具名为 `read_docs` 与 `web_search`，对应设计文件分别是 [`read-docs.md`](read-docs.md) 和 [`web-search.md`](web-search.md)。本记录沿用下划线工具名，不采用产品文档中的标题式命名。

## 2. MinerU：read_docs 所需的一手资料

官方主文档：[MinerU 文档解析接口文档](https://mineru.net/apiManage/docs)。该站把精准解析 API 的端点、请求字段、状态、错误码集中在同一页面，以下条目均来自该页；压缩包结构另见 [MinerU 输出文件说明](https://opendatalab.github.io/MinerU/reference/output_files/)。

### 2.1 认证、配置与限制

- 精准解析 API 需要用户在 API 管理页面创建 Token，请求头为 `Authorization: Bearer <token>`。
- 官方 API 没有规定本地环境变量名；`MINERU_API_TOKEN` 是 MiniAgent 的配置契约，不应误称为 MinerU 官方字段。
- 精准解析单文件上限为 200 MB、200 页。官方页面列出的支持类型包含 PDF、常见图片、Doc/Docx、Ppt/Pptx、Xls/Xlsx；MiniAgent 首版只开放 `.pdf`、`.doc`、`.docx` 是产品收窄。
- `model_version` 可选 `pipeline`、`vlm`、`MinerU-HTML`，官方默认是 `pipeline`；MiniAgent 固定 `vlm` 是显式设计选择。

来源：[精准解析 API、文件限制、模型版本](https://mineru.net/apiManage/docs)。

### 2.2 本地文件预签名上传工作流

1. `POST https://mineru.net/api/v4/file-urls/batch`，JSON 至少包含 `files: [{"name": "demo.pdf"}]`；MiniAgent 应显式发送 `model_version: "vlm"`。
2. 成功业务响应以顶层 `code == 0` 表示，`data.batch_id` 是后续查询标识，`data.file_urls` 与请求文件顺序对应。
3. 对每个预签名 URL 执行原始文件字节的 HTTP `PUT`。官方特别说明上传时无需设置 `Content-Type`。
4. 上传完成后无需再调用“提交任务”端点，MinerU 会扫描已上传文件并自动提交解析任务。
5. 预签名 URL 有效期 24 小时；单次最多申请 50 个链接。`read_docs` 每次只有一个文件，但 client 仍应校验恰好返回一个 URL。

来源：[本地文件批量上传解析](https://mineru.net/apiManage/docs)。

### 2.3 异步查询、轮询与完成结果

- 查询端点：`GET https://mineru.net/api/v4/extract-results/batch/{batch_id}`，同样使用 Bearer Token。
- 响应顶层包含 `code`、`msg`、`trace_id`、`data.batch_id`；`data.extract_result` 是结果数组。
- 单项状态官方枚举为：`waiting-file`（等待上传/提交）、`pending`（排队）、`running`（解析）、`converting`（格式转换）、`done`、`failed`。
- `running` 时可能含 `extract_progress.extracted_pages`、`total_pages`、`start_time`；实现不应依赖这些可选进度字段推进状态机。
- `done` 时读取 `full_zip_url` 下载结果；`failed` 时 `err_msg` 有效。压缩包中 Markdown 文件名为 `full.md`，并可能包含 JSON 等其他产物。
- 官方只描述异步轮询流程，没有规定客户端轮询间隔、总超时、退避或自动重试策略；这些必须由 MiniAgent 按自己的执行预算确定。

来源：[批量获取任务结果](https://mineru.net/apiManage/docs)、[输出文件说明](https://opendatalab.github.io/MinerU/reference/output_files/)。

### 2.4 错误与额度事实

官方响应同时存在 HTTP 状态与 JSON 业务 `code`，不能只检查 HTTP 200。与实现映射直接相关的业务码包括：

- `A0202` Token 错误，`A0211` Token 过期；
- `-500` / `-10002` 参数错误，`-10001` 服务异常；
- `-60001` 生成上传 URL 失败；
- `-60002` 文件格式识别失败，`-60003` 文件读取失败，`-60004` 空文件；
- `-60005` 超过 200 MB，`-60006` 页数超限；
- `-60007` 模型服务暂不可用，`-60008` 文件读取超时，`-60009` 提交队列已满，`-60010` 解析失败；
- `-60011` 未获得有效上传文件，`-60012` 找不到任务，`-60013` 无权访问任务；
- `-60015` / `-60016` 文件转换失败，`-60017` 重试次数达到上限；
- `-60018` 每日解析任务数量达到上限；`-60022` 网页读取失败（官方称可能由网络或限频导致）。

官方精准解析部分没有给出通用的 HTTP 429 限流契约；页面中的 HTTP 429 明确属于免 Token 的 Agent 轻量 API，不能直接套到 `/api/v4/file-urls/batch`。因此精准 API 的限流应以实际 HTTP 状态和业务码做防御性映射，不能在计划中虚构具体阈值。

来源：[精准解析 API 常见错误码](https://mineru.net/apiManage/docs)。

### 2.5 实现时必须保留的防御性检查

- 同时校验 HTTP 成功、JSON 可解析、`code == 0` 和所需字段类型；错误消息不得携带 Token、预签名 URL 或原始响应正文。
- 轮询只把 `done`、`failed` 当终态；未知状态应视为供应商响应变化，而不是无限轮询。
- `full_zip_url` 是供应商返回的临时下载 URL，不应进入 ToolOutput、持久日志或缓存 manifest。
- ZIP 为不可信输入：限制下载字节数、成员数和解压总量，拒绝绝对路径、`..`、链接及重复/歧义的 `full.md`。

## 3. Tavily：web_search 所需的一手资料

主要来源：[API introduction](https://docs.tavily.com/documentation/api-reference/introduction)、[Search endpoint](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[官方 OpenAPI JSON](https://docs.tavily.com/documentation/api-reference/openapi.json)、[Python SDK reference](https://docs.tavily.com/sdk/python/reference)。

### 3.1 认证、端点与客户端

- REST 端点是 `POST https://api.tavily.com/search`，请求为 JSON，认证头为 `Authorization: Bearer tvly-...`。
- 官方 Python SDK 用 API key 构造 `TavilyClient`；参考文档同时明确提供 `AsyncTavilyClient`。MiniAgent 可直接采用异步 client，无需以线程包装同步 client 作为首选。
- 官方 API/SDK 要求 API key，但没有把 `TAVILY_API_KEY` 声明为该构造函数的必需环境变量；该变量名是 MiniAgent 的配置入口。官方另有 `TAVILY_PROJECT`，但它只用于可选项目用量归属，不是搜索认证。

来源：[API introduction](https://docs.tavily.com/documentation/api-reference/introduction)、[Python SDK client 初始化](https://docs.tavily.com/sdk/python/reference)。

### 3.2 请求契约

- OpenAPI 唯一必填 body 字段是字符串 `query`。
- `max_results` 默认 5，允许 0 到 20。MiniAgent 固定 5 条与官方默认一致。
- `search_depth` 默认 `basic`；普通 `basic` 搜索消耗 1 API Credit，`advanced` 消耗 2。若依赖稳定成本/语义，建议实现显式发送 `search_depth="basic"`，不要只依赖 SDK 将来的默认值。
- 必须显式发送 `include_answer=false`、`include_raw_content=false`、`include_images=false`，避免产生不需要的 LLM answer、全文和图片。若希望完全锁定输出，还可显式关闭 `include_image_descriptions`、`include_favicon`、`auto_parameters`、`include_usage`。
- `topic` 官方默认 `general`；其他高级过滤字段不需要暴露给模型。

来源：[Search 请求 schema](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[OpenAPI JSON](https://docs.tavily.com/documentation/api-reference/openapi.json)。

### 3.3 响应契约及设计文档差异

- 200 响应顶层包含执行过的 `query`、`answer`、`images`、按相关性排序的 `results`、`response_time`，并可能含 `auto_parameters`、`usage`、`request_id`。
- 每个 `results[]` 的官方字段是 `title`、`url`、`content`、`score`、`raw_content`、`favicon`、`images`。`content` 就是普通搜索时应映射为 snippet 的字段。
- 当前官方 OpenAPI 的 `results[]` **没有 `published_at` 或 `published_date`**。因此 `web-search.md` 中“明确提供时保留 published date”在当前 API 下通常只能得到 `None`；实现不得从 `content` 猜测日期，也不应把不存在的 SDK 字段当必需字段。
- `score` 是排序信号；保留供应商顺序即可，不必进入稳定 ToolOutput。

来源：[Search 响应 schema](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[OpenAPI JSON](https://docs.tavily.com/documentation/api-reference/openapi.json)。

### 3.4 HTTP 错误、限流与额度

官方 OpenAPI 为 `/search` 明确列出：

- `400`：请求无效；
- `401`：API key 缺失或错误；
- `429`：请求过多，超过 rate limit；
- `432`：API key limit 或套餐 usage limit 超出；
- `433`：Pay-as-you-go limit 超出；
- `500`：Tavily 内部服务错误。

错误体示例结构为 `{"detail": {"error": "..."}}`。实现可按 HTTP 状态稳定映射，但不得把供应商错误正文原样暴露给模型。官方页面没有给出固定的每分钟请求数，也没有承诺 `Retry-After`；自动重试不能假定某个限流窗口。`429` 是明确限流，`432/433` 是额度/计划限制，不应自动重试；`500` 和连接失败才适合作为有限的 transient 候选。

来源：[Search 错误响应](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[OpenAPI JSON](https://docs.tavily.com/documentation/api-reference/openapi.json)。

## 4. 对后续实现计划的直接结论

1. `read_docs` 的 client 应建模为三段式异步状态机：申请预签名 URL并取得 `batch_id` -> 无 `Content-Type` 的 PUT 上传 -> 有界轮询批量结果并下载 `full_zip_url`；上传后没有额外 submit 调用。
2. MinerU client 必须识别顶层业务 `code`，并覆盖 `waiting-file/pending/running/converting/done/failed`；HTTP 200 不等于业务成功。
3. `web_search` 可使用官方 `AsyncTavilyClient.search()`；明确固定 basic 搜索、5 条结果并关闭 answer/raw content/images，随后只规范化 `results[].title/url/content`。
4. Tavily 当前无发布日期结果字段，计划和测试应接受 `published_at=None` 为常态，不应制作供应商并未承诺的日期 fixture。
5. 两个 client 的超时、轮询间隔、退避、取消收束、秘密清洗和缓存安全都属于 MiniAgent 机制，需要结合框架设计文档决定，不能声称由供应商文档规定。
