# 实现 read_docs 与 web_search 内置工具

本文档是一份持续维护的执行计划。实施期间必须同步更新 `Progress`、`Surprises & Discoveries`、`Decision Log` 和 `Outcomes & Retrospective`。本文遵守仓库根目录 `PLANS.md`，并且自身包含完成工作所需的背景、顺序、接口、验证方法和恢复方式。按照本计划工作时，不需要也不应读取其他未完成执行计划；若实现期间发现另一个计划正在修改同一公共边界，应先比较实际代码差异并在本文记录协调决定，不能覆盖他人改动。

## Purpose / Big Picture

完成后，模型在配置了 `TAVILY_API_KEY` 时可以调用 `web_search` 获取最多五条带标题、HTTP(S) URL 和摘要的实时搜索结果；在配置了 `MINERU_API_TOKEN` 且用户批准本地文档外传时，可以调用 `read_docs` 把 PDF、DOC 或 DOCX 转换为当前 Session 内受控的 Markdown，再用 `read_file` 分页读取。没有相应密钥时，工具的 schema、索引和 Prompt 都不应出现在模型上下文中。

用户可以通过两类可观察行为确认功能：普通搜索只返回来源结果而不返回 Tavily 生成的答案或网页全文；文档转换在上传前请求一次完整的“本地文件读取 + MinerU 外传”许可，完成后只返回 `DocumentRef` 和完整性信息，不把整篇 Markdown 塞进一次 ToolResult。自动化验收不依赖真实网络、真实密钥或用户文件，全部供应商交互使用 fake client 或 `httpx.MockTransport`。

## Progress

- [x] (2026-07-24) 阅读 `AGENTS.md`、`PLANS.md`、工具框架设计、工具作者指南和 `docs/design-docs/tools/` 下的稳定工具契约；按用户要求未读取当前未完成执行计划。
- [x] (2026-07-24) 核验 MinerU 与 Tavily 官方 API，并把逐项来源记录到 `docs/design-docs/tools/read-docs-web-search-api-research-notes.md`。
- [x] (2026-07-24) 对照当前代码确认公共缺口：外部服务 Target Authorization、通用 runtime capability 注入、结构化 ToolResult/受控 DocumentRef 尚未完全落地。
- [x] (2026-07-24) 里程碑一：补齐外部工具配置、统一 Target Authorization、不可变 runtime capability 和结构化 ToolResult/受控引用边界。
- [x] (2026-07-24) 里程碑二：实现 `web_search`、条件注册、Tavily 规范化与 transient-only retry，并完成独立验收。
- [x] (2026-07-24) 里程碑三：实现 MinerU client、安全 ZIP 提取、DocumentCache 和 `read_docs`，MockTransport 状态机验收通过。
- [x] (2026-07-24) 里程碑四：完成 `read_file` 对已提交 Current Session DocumentRef 的精确读取、篡改拒绝和恢复重建。
- [x] (2026-07-24) 里程碑五：完成依赖锁定、compileall、受影响模块测试和全量回归，最终 `183 passed`。

## Surprises & Discoveries

- Observation: 当前 Tavily Search OpenAPI 的 `results[]` 没有 `published_at` 或 `published_date` 字段。
  Evidence: 官方 Search 响应只声明 `title`、`url`、`content`、`score`、`raw_content`、`favicon` 和 `images`。因此首版 `published_at=None` 是常态，不能从 snippet 猜日期，也不能制作供应商未承诺的日期 fixture。

- Observation: MinerU 的本地文件流程在 PUT 到预签名 URL 后自动提交解析任务，没有额外 submit 请求。
  Evidence: 官方文档给出的流程是 `POST /api/v4/file-urls/batch`、原始字节 `PUT`、`GET /api/v4/extract-results/batch/{batch_id}`；上传 PUT 不要求 `Content-Type`，完成归档中的 Markdown 名为 `full.md`。

- Observation: 当前实现仍处于从旧工具框架向稳定设计迁移的中间态。
  Evidence: `miniagent/tools/models.py` 仍保留 `operation`、系统硬上限和只注入 `todo_store` 的兼容字段；`miniagent/tools/policy.py` 只接受 workspace 相对路径；`miniagent/tools/__init__.py` 固定注册本地工具。不得在新工具内复制临时旁路来绕过这些缺口。

- Observation: 撰写计划时工作区已有与本任务无关的未提交修改。
  Evidence: `git status --short` 显示工具框架、UI、依赖及旧执行计划已有修改。实施者必须逐文件保留这些修改，只叠加本计划所需变更。

- Observation: Windows 不允许删除仍由 `tempfile.mkstemp` 文件描述符占用的临时 Markdown。
  Evidence: 首次 ZIP 聚焦测试在 `Path.unlink()` 得到 `WinError 32`；提取器改为先关闭 descriptor，再创建受预算控制的输出流，随后相同测试通过。

- Observation: 结构化 ToolResult 迁移会使 Artifact 的稳定载荷从裸 content 变为完整规范 JSON。
  Evidence: 执行器预算现在覆盖 `content/metadata/data`，ArtifactStore 相应写入 `result.json`；回归测试已更新为解析 JSON，并验证 inline output 与 artifact 互斥。

## Decision Log

- Decision: 先补齐两个工具必需的公共执行边界，再写供应商 handler。
  Rationale: `external_service/write` 必须在上传前由统一 Target Authorization 裁决，secret 和客户端必须由 composition root 注入；把这些逻辑写进 `read_docs` handler 会直接违反稳定设计。
  Date/Author: 2026-07-24 / Codex

- Decision: `web_search` 使用 Tavily 官方 `AsyncTavilyClient`，不建立供应商无关的公共搜索端口。
  Rationale: 稳定契约明确绑定 Tavily。测试所需 fake 可以实现一个工具包内部的窄类型协议，但该协议不成为产品级多供应商抽象。
  Date/Author: 2026-07-24 / Codex

- Decision: Tavily 调用显式固定 `max_results=5`、`include_answer=False`、`include_raw_content=False` 和 `include_images=False`，其余搜索策略沿用锁定 SDK 版本的普通默认值。
  Rationale: 官方当前默认 `search_depth=basic`，但 `docs/design-docs/tools/web-search.md` 明确要求其余参数使用锁定 SDK 的默认值。研究笔记中“显式 basic”是供应商层建议，不能覆盖仓库稳定契约；SDK 升级时用契约测试发现语义变化并重新审查设计。
  Date/Author: 2026-07-24 / Codex

- Decision: MinerU 不使用额外 SDK，使用项目已有 `httpx.AsyncClient` 封装一个具体 `MinerUClient`。
  Rationale: 官方协议是少量 HTTP 请求，项目已依赖 `httpx`；自行封装可以明确控制单步 timeout、取消、敏感字段清洗、业务 `code` 校验和不可信 ZIP 的预算。
  Date/Author: 2026-07-24 / Codex

- Decision: `read_docs` 即使最终命中缓存，也始终声明 MinerU write target，并在 handler 读取文件字节或计算 SHA-256 前完成授权。
  Rationale: 目标规划不能依赖未授权文件内容，保守声明还能避免“先读文件判断缓存、后决定是否提示外传”的权限旁路。用户选择 `allow_session` 后可避免同一 Session 的重复提示。
  Date/Author: 2026-07-24 / Codex

- Decision: MinerU 的轮询使用取消感知的有界退避，不恢复旧 batch，也不自动重放整个 ToolUse。
  Rationale: 创建 batch 后超时或断线时远端结果可能未知；自动重放会重复上传和创建任务。一次显式新调用从头开始，完成缓存只接纳校验并原子提交后的产物。
  Date/Author: 2026-07-24 / Codex

- Decision: SessionEngine 持有 runtime-only 的 TargetAuthorizer、DocumentRegistry 和受控 Artifact target 投影，composition root 只负责按配置注入具体 client/store。
  Rationale: `allow_session` 必须跨同一 Session 的多个 AgentRun 生效，同时不能写 Journal；DocumentRef/ArtifactRef 又只能在 ToolResult fsync 成功后登记。由 SessionEngine 持有这三类进程内状态能同时满足生命周期和提交顺序。
  Date/Author: 2026-07-24 / Codex

- Decision: `read_file` 对 DocumentRef 使用等价的精确 `file/read/exact` 受控 target，而不是新增第二条 handler 路径。
  Rationale: handler 已经只消费授权后的规范化文件 target；受控目录以精确 target 集合区分真实引用和模型手写 `.mini` 路径，保持单一读取实现且不扩大 capability/scope。
  Date/Author: 2026-07-24 / Codex

## Outcomes & Retrospective

五个里程碑均已完成。composition root 仅在 `TAVILY_API_KEY` 非空时构造锁定的 `tavily-python 0.7.26` `AsyncTavilyClient` 并注册 `web_search`，仅在 `MINERU_API_TOKEN` 非空时构造 `MinerUClient` 并注册 `read_docs`；四种独立注册组合已有测试。固定 Tavily read target 自动允许，MinerU write target 在读取源文件正文前产生包含全部规范化目标的 Permission Request；deny、allow once、allow session、等待不消耗 handler timeout 和取消收束均有自动化证据。

`web_search` 固定五条上限及三个关闭开关，只持久化规范化 title/URL/snippet/可空日期。`read_docs` 完成 POST、无 Content-Type PUT、四种非终态轮询、done/failed、HTTPS 临时 URL 校验、受预算 ZIP 提取和 Session 隔离缓存。DocumentRef 只有在成功 ToolResult 写入 Journal 并 fsync 后登记；同一 Session 恢复会重新验证路径、manifest、大小和 SHA-256，未提交、跨 Session、相邻路径及篡改内容不能获得读取豁免。

验收命令 `uv run python -m compileall miniagent tests main.py` 退出码为 0；受影响模块聚焦测试为 `38 passed`；最终 `uv run python -m pytest -q` 为 `183 passed`。无密钥 TUI 启动行为由 Textual `run_test` 生命周期测试覆盖，未进行真实 Tavily/MinerU 网络冒烟，因为本轮没有使用真实测试账号或凭据；这不影响离线自动化验收。

## Context and Orientation

MiniAgent 的工具定义由 `ToolSpec` 聚合。每个工具包位于 `miniagent/tools/<tool_name>/`，其中 `tool.py` 定义严格 Pydantic 输入/输出、目标解析、并发分类、异步 handler 和策略，`prompt.py` 导出英文 `PROMPT`，`__init__.py` 只导出 `SPEC`。`miniagent/tools/registry.py` 根据 composition root 显式提供的名称加载并冻结工具，不能扫描目录自动注册。

`ToolTarget` 是工具在执行前声明的资源能力。`web_search` 只声明固定的 `external_service/read/exact/api.tavily.com`，配置并启用该服务就构成部署授权，不逐次弹窗。`read_docs` 同时声明源文件 `file/read/exact` 和 `external_service/write/exact/mineru.net`；后者表示把本地内容交给外部服务，必须由统一 Target Authorization 在 handler 启动前一次性裁决全部目标。`allow_once` 只覆盖当前 ToolUse，`allow_session` 在当前进程的 Current Session 内缓存同能力、同范围授权，任何 permission 状态都不能写入 Journal 或 Trace。

runtime capability 是 composition root 构造后放进 `ExecutionContext` 的不可变运行时对象，例如 Tavily client、MinerU client 和 DocumentCache。它们不属于模型参数，密钥也不得进入 input schema、output、repr、Permission Request、Journal、Trace、manifest 或异常文本。缺少某个密钥时不构造相应 client，也不注册相应工具。

`DocumentCache` 是 Current Session 的受控 Markdown 存储。它以源文件原始字节 SHA-256 为 key，把完成产物原子提交到 `.mini/sessions/<session_id>/document_cache/<source_sha256>/content.md`，并生成只允许当前 Session 精确读取该文件的 `DocumentRef`。`read_file` 是读取 `DocumentRef` 的唯一模型工具；伪造相似 `.mini` 路径、manifest、其他 Session 的路径或 subtree 访问都不能获得豁免。

当前 composition root 位于 `miniagent/ui/app.py` 的 `_ConfiguredLoop.run()`：它加载 Provider 配置、构造 Registry、模型和 ToolExecutor。当前配置加载器 `miniagent/provider/config.py` 只处理 OpenAI-compatible Provider，不能把工具密钥塞进 `ProviderConfiguration`。应新建工具配置模块，并在 composition root 将可用工具名与对应 capability 一起构造，保证注册可见性和运行能力不会漂移。

当前框架尚未完整实现稳定设计中的结构化 ToolResult、external service authorization 和受控引用目录。里程碑一必须先完成两个工具实际依赖的最小闭环；若实施时这些边界已经由别的工作完成，应通过下述测试确认后复用，删除本计划中重复的编辑，不得另建第二套权限或引用系统。

## Reference Reading Strategy

每个里程碑开始前只刷新该阶段需要的资料，并把供应商文档中的必要事实写入测试或代码常量，不让实现依赖执行者记忆。

里程碑一开始前，完整阅读 `docs/design-docs/tool-registry-and-execution.md` 的第 3、5、7、8、9、10、11 节，以及 `docs/design-docs/tool-design-guidelines.md` 的第 2、3、6、7、8、9、10、11、12、13 节。然后阅读 `miniagent/tools/models.py`、`registry.py`、`executor.py`、`policy.py`、`artifacts.py`、`miniagent/domain.py`、`miniagent/session.py` 和 `miniagent/ui/app.py` 的当前版本。目标是先确认统一授权、结构化结果和 runtime capability 是否已经落地，不凭本计划撰写时的旧快照覆盖新代码。

里程碑二开始前，完整阅读 `docs/design-docs/tools/web-search.md` 和本计划的 Decision Log，再核对 `docs/design-docs/tools/read-docs-web-search-api-research-notes.md` 的 Tavily 部分、官方 API Introduction、Search endpoint 与 Python SDK reference。重点核对锁定 SDK 的异步方法签名、异常类型和固定四个参数；若官方响应仍无发布日期，保持 `published_at=None`。

里程碑三开始前，完整阅读 `docs/design-docs/tools/read-docs.md`、研究笔记的 MinerU 部分和 MinerU 官方文档中的“本地批量文件上传解析”“批量获取任务结果”“错误码”，并刷新 MinerU 输出文件说明。重点核对业务 `code`、六种状态、`full_zip_url` 和 `full.md`；不能从旧 fixture 猜字段。

里程碑四开始前，完整阅读 `docs/design-docs/tools/read-file.md` 的受控引用部分、`tool-registry-and-execution.md` 的“受控引用目录”和“结果持久化”，再读当前 `miniagent/tools/read_file/tool.py`、Journal codec 与 Session 恢复代码。重点验证引用只在 ToolResult 成功提交后生效，恢复时重新做根目录和哈希校验。

里程碑五开始前，阅读 `AGENTS.md` 的 Verification、两个具体工具设计文档的“验收不变量”和 `tool-design-guidelines.md` 的“新工具最低测试/提交前检查”。供应商文档只在锁定 SDK 或 API 页面发生变化时重读；测试不得访问真实网络。

## Plan of Work

### 里程碑一：建立共享执行边界

本里程碑结束时，应用可以在启动期安全加载两类工具密钥，按 capability 是否存在决定工具注册，并能正确区分固定外部只读服务和本地内容外传。它不实现具体搜索或解析业务，但应通过测试证明：缺密钥时工具不可见；Tavily read target 自动允许；MinerU write target 在文件读取和 handler 之前等待一次完整 Permission Decision；secret 不出现在任何可持久化或可展示对象中。

在 `miniagent/tools/config.py` 新增独立的 `ExternalToolConfiguration` 与 loader。它从进程环境和 workspace `.env` 读取 `TAVILY_API_KEY` 与 `MINERU_API_TOKEN`，进程环境优先，trim 后空值按缺失处理；两个 secret 字段使用 `repr=False`。工具配置与 `ProviderConfiguration` 分离，OpenAI Provider 缺失时的现有行为不变。

把 `miniagent/tools/models.py` 和 `miniagent/tools/executor.py` 收敛到稳定设计：`ExecutionContext` 能取得 composition root 注入的不可变 runtime capabilities；`ToolTarget` 使用 `kind/capability/scope/value`；handler 的成功返回值按声明的 output model 校验并以完整规范 JSON 应用 ResultPolicy。不要为这两个工具保留裸字符串特例。若结构化 ToolResult/ArtifactRef 的迁移仍未完成，同步更新 `miniagent/domain.py`、Journal codec 和现有测试，使成功小结果持久化 `output`，大结果只持久化 `artifact`，失败只持久化 `failure`，三者互斥。

在 `miniagent/tools/policy.py` 或稳定设计规定的独立授权模块中实现统一 Target Authorization 所需闭环。固定启用的 `external_service/read/exact/api.tavily.com` 自动允许；`external_service/write/exact/mineru.net` 必须产生 Permission Request；同一 ToolUse 的 file/read 与 external_service/write 整体裁决。为 SessionEngine 增加只存在内存中的 `allow_session` grant 和 AgentRun deny cache，并通过窄 port 把请求交给 UI。Textual 侧在 `miniagent/ui/modals/permission.py` 增加一个展示工具名、全部规范化目标、能力和范围的 modal，提供 deny、allow once、allow session；不要展示 token、上传 URL或原始 arguments。等待用户决定不计入工具 timeout，取消/切换 Session/退出取消待定请求且不写 Journal/Trace。

在 `miniagent/ui/app.py` 的 composition root 一次性构造工具配置、启用的固定 external read target、客户端与 store，再用同一份可用性事实构造 Registry 和 ToolExecutor。调整 `build_default_registry`，让基础本地工具始终显式列出，`web_search` 和 `read_docs` 只在对应 capability 构造成功时加入显式名称元组。应用关闭或 AgentRun 结束时关闭由本层拥有的异步 HTTP client，不能留下后台连接或任务。

为这一里程碑添加 focused tests：配置环境优先和 repr 脱敏；两种密钥的独立注册矩阵；固定 read target 自动允许；write target 的 deny/allow_once/allow_session；多目标整体拒绝时 handler 不执行且源文件内容未被读取；permission 等待不消耗执行 timeout；取消能收束等待；Journal/Trace 不含 permission 状态和 secret。完成后运行这些 focused tests，只有公共边界通过后才进入供应商工具。

### 里程碑二：把 web_search 实现为有界 Tavily 适配器

本里程碑结束时，配置 Tavily key 的应用能执行普通 Web 搜索，并稳定得到最多五条规范化来源。新建 `miniagent/tools/web_search/__init__.py`、`tool.py` 和 `prompt.py`，按 `docs/design-docs/tools/web-search.md` 原样落实 Provider-visible description、严格 `query` schema、英文 Prompt、串行 classifier、30 秒工具 timeout、两次 attempt 和默认 ResultPolicy。

在 `pyproject.toml` 加入 `tavily-python`，使用 `uv lock` 更新锁文件，不能手工编辑 `uv.lock`。composition root 使用 secret 构造官方 `AsyncTavilyClient`。handler 只从 `ExecutionContext` 获取该具体 client，调用 `search(query=<trimmed query>, max_results=5, include_answer=False, include_raw_content=False, include_images=False)`；底层请求 timeout 必须短于 30 秒总 timeout。若锁定 SDK 的异步接口无法设置有界 timeout，在工具包内部用 `asyncio.timeout` 包住 await，并验证取消时 client 请求会收束；不要回退到无界后台线程。

在 `tool.py` 中把供应商 response 立即收窄为稳定模型，不保存 raw response。逐条要求 title/content 为可处理字符串，URL 可解析为 `http` 或 `https`、host 非空且总长不超过 2048；规范化 scheme/host、默认端口和 fragment 后去重并保持首次出现的 Tavily 排名。空标题用 `Untitled result`，空 content 用 `No snippet available.`，snippet 超过 1000 个 Unicode 字符时截断并标记。当前官方响应没有发布日期，因此缺失时写 `None`；只有供应商未来明确返回且类型可接受的字段才保留，绝不从摘要推断。最多保留五条，零条是成功。

建立窄错误翻译函数，只读取异常的稳定状态属性，不把异常 repr、response body、headers、query 或 key放进 `safe_message`。401 映射 `AUTHENTICATION_FAILED`；432/433 映射 `QUOTA_EXCEEDED`；429 映射 `RATE_LIMITED` 且不自动 retry；400 映射 `OPERATION_FAILED`；连接失败和明确的 5xx 映射 transient `RESOURCE_UNAVAILABLE`，只允许第二次 attempt；响应类型错误映射 `INVALID_RESPONSE`。取消不翻译成普通供应商失败。

新增 `tests/tools/test_web_search.py`，用 fake async Tavily client 覆盖 schema 与固定调用参数、条件注册、目标、串行分类、URL 拒绝与规范化去重、稳定排序、标题/摘要缺失、1000 字符截断、当前无发布日期、零结果、各错误映射、仅 transient 重试、timeout/取消、输出和日志脱敏。增加一个从 Registry 到 Executor 的集成测试，证明模型可见 content 只有稳定编号结果，`data.results` 保留同顺序结构，query、score、raw response 和 key 均不存在。

### 里程碑三：实现 MinerU 转换与 DocumentCache

本里程碑结束时，`read_docs` 能在一次获准的 handler 内完成申请上传 URL、上传、轮询、下载、安全提取和原子缓存提交，失败或取消不会产生有效 DocumentRef。新建 `miniagent/tools/read_docs/__init__.py`、`tool.py`、`prompt.py`、`client.py` 和 `archive.py`，并在 `miniagent/documents.py` 定义 `DocumentCache`、`DocumentRef`、manifest 模型和 Current Session 引用登记。`DocumentCache` 是应用级 runtime capability，不是模块全局变量。

`ReadDocsInput` 只接收 `path`。目标解析先规范化真实普通文件，再声明 `file/read/exact/<source>` 与 `external_service/write/exact/mineru.net`。后缀大小写不敏感且只允许 PDF、DOC、DOCX；空文件、超过 MinerU 官方 200 MiB 上限、明显与后缀冲突的 magic 在上传前失败，但 magic 检查不能扩大白名单。具体地，PDF 要求文件头是 `%PDF-`，旧 DOC 要求 OLE Compound File 签名，DOCX 要求 ZIP 容器中存在 `[Content_Types].xml` 和 `word/` 条目；一个内容满足其他受支持类型但与声明后缀冲突的文件必须拒绝，未知内容也不能借 sniffing 获得新后缀。classifier 固定串行，ToolSpec 只有一次 attempt，总 timeout 五分钟。

`MinerUClient` 使用一个有界 `httpx.AsyncClient` 实现明确状态机。先 POST `https://mineru.net/api/v4/file-urls/batch`，body 是单个文件名和 `model_version="vlm"`；同时检查 HTTP 状态、JSON object、顶层 `code == 0`、非空 `batch_id` 和恰好一个上传 URL。随后对该 HTTPS 预签名 URL PUT 原始文件字节，不主动设置 `Content-Type`。上传成功后无需 submit，循环 GET `https://mineru.net/api/v4/extract-results/batch/{batch_id}`。`waiting-file`、`pending`、`running`、`converting` 是非终态，`done` 必须提供合法 HTTPS `full_zip_url`，`failed` 是终态业务失败，未知状态是 `INVALID_RESPONSE`。

轮询等待使用 cancellation-aware 的 1、2、4、5 秒退避并封顶 5 秒；每个控制面请求和下载请求不超过 30 秒，上传单步不超过 60 秒，且都短于五分钟总 timeout。任何 await 前后检查取消。创建 batch 后的 timeout、断线、取消或未知结果都不重放，不把 batch id 写入 manifest 或 ToolOutput。只有创建 batch 前明确未发送的连接失败可以在 client 内做一次安全连接恢复；ToolSpec 本身仍是单 attempt。

所有由 MinerU 返回的预签名上传 URL和下载 URL都视为不可信的临时 capability：只接受 HTTPS、非空 host、无 userinfo，跟随 redirect 时重新应用相同检查，禁止降级到 HTTP；它们从不进入 output、Trace、Journal、manifest 或异常文本。API 错误翻译同时看 HTTP 状态与业务 `code`：Token 错误映射 `AUTHENTICATION_FAILED`，每日任务额度映射 `QUOTA_EXCEEDED`，服务不可用/队列满映射 `RESOURCE_UNAVAILABLE`，格式和大小问题映射 `UNSUPPORTED_OPERATION` 或 `RESOURCE_EXHAUSTED`，未知可预期解析失败映射 `OPERATION_FAILED`，缺字段/未知状态映射 `INVALID_RESPONSE`。`safe_message` 只使用仓库定义的英文常量。

`archive.py` 先以流式下载限制压缩包最多 256 MiB，再读取 ZIP central directory。最多允许 4096 个成员、总声明解压大小 512 MiB、目标 `full.md` 最多 256 MiB；拒绝绝对路径、盘符、`..`、反斜杠逃逸、NUL、symlink、重复规范路径和多个歧义 `full.md`。只把唯一 `full.md` 流式提取到同目录临时文件，按实际读取字节再次执行预算并验证 UTF-8；不提取图片、JSON 或其他成员。若官方输出布局改变而没有唯一 `full.md`，以 `INVALID_RESPONSE` 失败，不能猜选最大 Markdown 文件。

授权完成进入 handler 后，先以流式方式计算源文件 SHA-256，再查询 Current Session 完成缓存。缓存命中时重新验证 manifest、`content.md` 大小和 SHA-256，登记并返回 `cache_hit=True`；虽然不发网络请求，调用仍已按保守目标得到 MinerU write 授权。缓存未命中才运行 MinerU 状态机。`DocumentCache` 在 `.mini/sessions/<session_id>/document_cache/<source_sha256>/` 内使用同目录随机临时名，fsync 文件与目录后以原子 replace 提交 `content.md` 和 `manifest.json`。manifest 只包含设计文档列出的 schema version、源 hash/type、`vlm`、完成时间和 Markdown 大小/hash。

`ReadDocsOutput.content` 只给出成功说明和后续 `read_file` 用法；metadata/data 严格采用 `read-docs.md` 的形状。新增 `tests/tools/test_read_docs.py`、`tests/test_documents.py` 和固定的最小 ZIP fixture，覆盖后缀/magic/大小预检、permission 前不读文件、完整 HTTP 工作流、无 Content-Type PUT、六种状态、业务 code、退避和取消、未知状态、下载 URL 校验、ZIP 穿越/链接/重复/预算、原子提交、cache hit、同内容 Session 隔离、失败无半成品、output/manifest/Trace 全面脱敏。HTTP 测试只使用 `httpx.MockTransport`，时钟和等待函数可注入 fake，不能真实 sleep 或联网。

### 里程碑四：完成受控读取与恢复

本里程碑结束时，成功 `read_docs` 产生的精确 `DocumentRef` 能立即由 `read_file` 分页读取，并在关闭后重开同一 Session 时重新验证并恢复；任何伪造或损坏引用都失败。先确保 `DocumentRef` 作为 `ReadDocsOutput.data.document` 随成功 ToolResult 结构化持久化，并且只有 SessionEngine 接受该 ToolResult 后才进入 Current Session 受控引用目录。handler 完成但 Journal 提交失败时，磁盘缓存可以保留为未登记完成缓存，但不能自动授权给当前上下文。

扩展统一 target authorization 和 `read_file`，使 Current Session 已登记的 DocumentRef 对其精确 `document/read/exact` 或等价精确 file/read target 自动允许。handler 仍从 `ExecutionContext.targets` 取得已经解析的真实文件，不能根据 input path 自行拼接 `.mini`。普通模型手写 `.mini/sessions/.../content.md`、manifest、同目录其他文件、父目录 subtree、write/delete 或其他 Session 引用都必须拒绝或请求普通受保护路径权限，不能享受 DocumentRef 豁免。

Session 恢复时从已持久化、成功且结构匹配的 `read_docs` ToolResult 重建候选引用，再验证路径位于当前 Session 的 document_cache 根、source hash 目录名、manifest schema、content 大小和 SHA-256；失败引用不登记，并让后续读取安全失败并提示重新运行 `read_docs`。不要扫描整个 cache 目录推断引用，也不要从失败/取消/outcome unknown 的 ToolResult 恢复。

新增跨模块测试：转换后用返回 path 分两页读完 Markdown；伪造相邻路径失败；篡改内容后引用失效；跨 Session 引用失败；恢复同一 Session 后有效引用可读；未提交 ToolResult 不可读；普通递归工具仍跳过 `.mini`。最后增加 AgentLoop 集成场景，fake model 先调用 `read_docs`、再把其 path 传给 `read_file`、最后回答，验证工具消息顺序、结构化 output 和模型可见 content 都符合契约。

### 里程碑五：完成验证与可观察验收

先运行每个新增测试文件和受影响的 registry/executor/read_file/session/UI 测试，修复后再运行仓库要求的完整命令。所有测试通过后，启动 TUI 做不带密钥的冒烟验证，确认两个工具都不可见且应用其他能力正常；再用测试专用 fake composition root 启动一次，确认两工具可见、Tavily 无需逐次许可、MinerU 在外传前弹出完整许可。真实供应商冒烟是可选的人工证据，不能成为自动化验收条件，也不能把真实 key 或用户文件写入日志。

若有可用测试账号，真实 Tavily 冒烟只搜索无敏感内容并确认最多五条结果；真实 MinerU 冒烟只使用仓库内专门创建的无敏感小 PDF，选择 allow once，完成后用 `read_file` 读取首屏 Markdown。完成后删除测试 Session 只能由用户明确授权；默认保留并在 Outcomes 中注明位置，避免计划隐含破坏性清理。

## Concrete Steps

所有命令都从仓库根目录 `D:\study\MiniAgent` 运行。实施者在每个 stopping point 更新本计划的 Progress、Discoveries 和 Decision Log。

首先记录工作区，不要清理或还原既有修改：

    git status --short
    git diff -- docs/design-docs/tool-design-guidelines.md docs/design-docs/tool-registry-and-execution.md

完成里程碑一后运行公共边界测试，实际文件名以实现为准，但至少包含：

    uv run python -m pytest -q tests/provider/test_config.py tests/tools/test_registry.py tests/tools/test_executor.py tests/ui/test_app_lifecycle.py

添加 Tavily 依赖后更新并同步环境：

    uv lock
    uv sync

完成里程碑二后运行：

    uv run python -m pytest -q tests/tools/test_web_search.py tests/tools/test_integration.py

完成里程碑三和四后运行：

    uv run python -m pytest -q tests/tools/test_read_docs.py tests/test_documents.py tests/tools/test_read_file.py tests/tools/test_integration.py tests/test_session.py

若仓库中 read_file 测试实际位于其他文件，使用 `rg --files tests | rg "read.*file|document"` 找到它，不能因示例路径暂不存在而跳过相关覆盖。

最终必须运行：

    uv run python -m compileall miniagent tests main.py
    uv run python -m pytest -q

预期 compileall 退出码为 0，全量 pytest 无失败、错误或意外跳过。随后启动 TUI：

    uv run python -m miniagent.ui

不配置工具密钥时，模型工具列表不含 `web_search` 与 `read_docs`。在测试专用配置中只设置 Tavily key 时只出现 `web_search`；只设置 MinerU token 时只出现 `read_docs`；两者都设置时两者同时出现。任何输出、通知、trace、journal、artifact、manifest 和 `repr(configuration)` 都不能包含测试 key。

## Validation and Acceptance

验收以行为为准，而不是只检查文件存在。

`web_search` 的 schema 只有 `query` 和框架字段 `correction_of_tool_use_id`。一次成功 fake 搜索必须证明 SDK 收到固定的五条上限和三个关闭开关；返回顺序与供应商相关性顺序一致；非法 URL 被丢弃、重复 URL 去重、长摘要截断；零结果返回 `No search results found.`；当前官方无发布日期时结构化字段为 `None`。401、429、432/433、500 和连接失败分别进入规定错误分类，只有明确 transient 的资源不可用最多执行第二次 attempt。

`read_docs` 的 schema 只有 `path` 和框架字段。deny 时既不读取源文件内容，也不请求 MinerU；allow 后严格执行 POST、无 Content-Type PUT、GET 轮询、ZIP 下载和唯一 `full.md` 提取。成功 output 不含正文、batch id、临时 URL 或原始 response，只含简短读取指引、完整性 metadata 和 DocumentRef。任一步失败、超时或取消都不登记半成品引用；相同 Session、相同源字节的后续调用在授权后可命中完成缓存。

受控读取必须证明只有已提交的 Current Session DocumentRef 可精确读取，伪造路径、manifest、相邻文件、父目录和其他 Session 不可借此访问。篡改缓存后恢复会使引用失效，而不是静默读取不可信内容。

配置和安全验收必须扫描所有测试产物，确认 secret、query、原始供应商响应、预签名 URL和 batch id 未进入 output、Journal、Trace、artifact 或 manifest。所有自动化测试使用 fake、temporary directory、fake clock 和 MockTransport，断网环境下仍应通过。

## Idempotence and Recovery

配置加载、Registry 构造和 Session 恢复可以重复执行，不改变已经提交的缓存。DocumentCache 以内容 hash 定位，若目标目录已有相同且完整的 manifest/content，返回 cache hit；若内容或 manifest 不一致，视为损坏并在安全临时路径重新生成，不能原地拼接或信任部分文件。原子提交前的临时文件不登记为引用，下一次调用可以清理同一 cache 目录内由本实现命名且确认属于当前操作的 stale temp；不得递归删除未知目录。

MinerU batch 创建后的未知结果不恢复、不重放。用户可显式再次调用；如果前一次实际上成功但本地未提交，新调用创建新任务，这是为避免错误关联远端状态而接受的成本。Tavily 只读搜索可以按 RetryPolicy 重放一次，但 429、额度、认证和请求错误不重放。

实施时遇到既有未提交修改，先用 `git diff -- <file>` 理解内容并在其上编辑，绝不能运行 `git reset --hard`、`git checkout --` 或还原不属于本计划的改动。依赖变更只用 `uv lock`/`uv sync`；锁文件失败时先恢复网络或索引访问后重跑，不能手改 `uv.lock`。

## Artifacts and Notes

官方 API 调研记录位于 `docs/design-docs/tools/read-docs-web-search-api-research-notes.md`。其中已核验的关键供应商事实已经在本计划内重述，因此实施者即使离线也能理解流程；真正编码前仍按 Reference Reading Strategy 检查供应商页面是否变化。

建议在测试中保留最小且无敏感信息的协议 fixture：一个包含唯一 UTF-8 `full.md` 的 ZIP、一个按 `waiting-file -> pending -> running -> converting -> done` 变化的 MinerU MockTransport、一个 Tavily 五条结果 response。fixture 不保存真实 token、batch id、预签名 URL、用户文档或真实查询。

## Interfaces and Dependencies

完成里程碑后至少应存在以下稳定接口。具体 import 布局可随当前代码小幅调整，但不得改变职责边界。

在 `miniagent/tools/config.py`：

    @dataclass(frozen=True, slots=True)
    class ExternalToolConfiguration:
        tavily_api_key: str | None = field(default=None, repr=False)
        mineru_api_token: str | None = field(default=None, repr=False)

    class ExternalToolConfigLoader:
        def load(self, environment: Mapping[str, str], dotenv_path: Path | None) -> ExternalToolConfiguration: ...

在 `miniagent/documents.py`：

    class DocumentRef(BaseModel):
        session_id: str
        source_sha256: str
        path: str
        byte_count: int
        sha256: str

    class DocumentCache:
        def lookup(self, session_id: str, source_sha256: str) -> DocumentRef | None: ...
        def commit(self, session_id: str, source_sha256: str, source_type: str,
                   model_version: str, markdown_temp_path: Path) -> DocumentRef: ...
        def validate_and_register(self, ref: DocumentRef) -> bool: ...

如果现有 store 统一采用 async 接口，则上述磁盘方法可以是 async 并把阻塞 I/O 放进 `asyncio.to_thread()`；不能在事件循环直接执行大文件 hash、ZIP 或 fsync。

在 `miniagent/tools/read_docs/client.py`：

    class MinerUClient:
        async def convert(self, source_path: Path, *, model_version: Literal["vlm"],
                          cancellation: Cancellation, deadline: float) -> Path: ...
        async def close(self) -> None: ...

`convert` 返回受预算控制的临时 `full.md` 路径，不创建 DocumentRef、不决定 cache 路径、不持久化 batch 信息。DocumentCache 负责最终提交和引用。

在 `miniagent/tools/web_search/tool.py` 与 `read_docs/tool.py` 中分别定义设计文档给出的严格 Input、Metadata、Data、Output 模型和 `SPEC`。两个 handler 都只接收验证后的 input 与 `ExecutionContext`，只返回声明的 output model；预期失败抛出带封闭 `ExecutionErrorCode` 和安全英文消息的 `ToolExecutionError`。

唯一新增第三方运行依赖是 `tavily-python`。MinerU 继续使用已有 `httpx`，ZIP、hash、路径和原子文件操作使用 Python 3.11 标准库。不要通过系统 `curl`、浏览器、临时 shell 命令或机器上偶然存在的软件实现产品功能。

---

Revision note (2026-07-24): 初始版本。依据稳定工具设计、当前实现审计以及 MinerU/Tavily 官方 API 调研，确定先补公共授权与 capability 边界，再分别交付搜索、文档转换和受控读取恢复。
