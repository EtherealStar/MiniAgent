# 实现 read_file、write_file 与 todo_write 工具

本计划依据仓库根目录的 `PLANS.md` 编写。它是一份持续更新的中文 ExecPlan；实现过程中必须维护 `Progress`、`Surprises & Discoveries`、`Decision Log` 和 `Outcomes & Retrospective`。本计划刻意不依赖或复述当前未完成执行计划，实施者应以本计划和列出的设计文档为准。

## Purpose / Big Picture

完成后，Agent 可以读取已知的 UTF-8 文本文件并分页浏览，使用上一次读取得到的完整文件 SHA-256 安全地创建或替换文件，并在当前 Session 内维护结构化待办列表。用户可通过工具结果、Session snapshot 和 UI 看到稳定的行号、哈希、写入摘要及 TodoList；大读取结果不会被截断后伪装成成功，而是以安全错误要求缩小范围。

## Progress

- [x] 确认现有 Registry、Executor、ExecutionContext、Target resolver、ArtifactStore、SessionEngine 和 UI 投影接口。
- [x] 实现 `read_file` 的模型、目标解析、分页读取、哈希与预算策略。
- [x] 实现 `write_file` 的输入约束、冲突检查、同目录临时文件和原子替换。
- [x] 实现按 Session 隔离的 `TodoStore`、`todo_write` 及结构化 ToolOutput 投影数据。
- [x] 把三个工具加入 composition root 的显式工具列表并完成 Prompt、schema、错误和重试边界。
- [ ] 完成编译、全量测试和无真实凭据/网络的验收；现有 grep 测试仍有 3 项基线失败。

## Surprises & Discoveries

- 现有 `ResultPolicy` 的系统硬上限为 50 KiB；为兼容 read_file 的 256 KiB error 策略，error 模式允许工具声明更高硬上限，其他工具的原有校验保持不变。
- 当前 `SessionEngine` 的 ToolResult 领域对象只保存模型可见 content，尚无结构化 ToolOutput 提交钩子；todo_write 已通过 Executor runtime capability 保存权威状态，UI/Session 事件接入仍需公共边界扩展。

## Decision Log

- Decision：三个工具沿用现有 `ToolSpec`、严格 Pydantic input/output model、Target resolver、classifier、异步 handler 和 `ResultPolicy`，不在工具内重建权限、重试或结果持久化。
  Rationale：工具设计指南明确这些是框架边界，绕过会造成 schema、授权和 ToolResult 协议分叉。
  Date/Author：2026-07-24 / Codex。
- Decision：`read_file` 采用 256 KiB 与 25,000 model-token 的 `overflow_behavior="error"`，不生成二次 ArtifactRef；`write_file` 和 `todo_write` 使用小型内联结果。
  Rationale：读取工具需要可继续分页的确定性页，写入和 Todo 只需摘要及结构化数据。
  Date/Author：2026-07-24 / Codex。
- Decision：`write_file` 只支持 create-only 或带完整原始字节哈希的整文件替换，不实现 append、局部编辑、目录创建或移动。
  Rationale：保持与 read/write 正交边界，并把并发编辑冲突显式暴露给模型。
  Date/Author：2026-07-24 / Codex。
- Decision：`todo_write` 的 TodoStore 是 composition root 创建的应用级进程内能力，不从 Journal 恢复，也不使用模块级全局字典。
  Rationale：设计要求 Session 隔离、同进程重开保留、进程重启丢失，并禁止历史 ToolResult 成为权威状态。
  Date/Author：2026-07-24 / Codex。
- Decision：保留现有 grep 的目录目标与输出格式行为，不在本次计划中修复无关基线失败。
  Rationale：新工具只复用公共执行边界，改变 grep 会扩大任务范围并影响既有消费者。
  Date/Author：2026-07-24 / Codex。

## Outcomes & Retrospective

实现完成后记录：用户可观察到的三项能力、通过的测试数量、任何未覆盖的 TOCTOU 或平台限制，以及对后续工具实现的建议。若某项设计约束因现有框架缺口无法满足，必须在此说明证据和后续工作，而不是静默放宽契约。

本次实现提供了三个可注册工具及注入式 TodoStore；编译通过，新增工具与公共工具专项测试通过。全量测试为 167 passed、3 failed，失败集中在既有 grep 目标/展示基线。SessionEngine 的结构化 TodosChanged/Snapshot 事件和 AgentLoop 第 11 次提醒尚未接入，原因是当前 SessionEngine/ToolResult 没有结构化输出提交边界；后续应先扩展该公共协议，再接 UI 投影。

## Context and Orientation

工具包应位于 `miniagent/tools/<tool_name>/`，每个包至少有 `__init__.py`、`tool.py` 和 `prompt.py`。`tool.py` 定义严格输入/输出模型、目标解析器、纯 classifier、异步 handler 和 `ToolSpec` 所需策略；`prompt.py` 导出英文 `PROMPT`。composition root 只把显式完成的工具名交给 Registry，Registry 冻结后生成 Provider function schema 和 output schema。

`ToolExecutor` 负责参数校验、目标解析与统一授权、超时/取消、重试、输出校验、结果预算和 ToolResult 提交。handler 只能使用 `ExecutionContext.targets` 和注入的 runtime capability。普通 workspace 文件由统一 Target Authorization 判断；`.mini`、越界路径、ArtifactRef/DocumentRef 和 session_state 不能通过工具参数自行豁免。

`read_file` 与 `write_file` 通过文件 target 协作，前者是 `read/exact` 且可读取受控引用，后者是 `write/exact`；`todo_write` 使用隐式绑定 Current Session 的 `session_state/write/exact/todos`。三者都必须返回声明的 `ToolOutput` 子类，不能返回裸字符串、failure 信封或 artifact。

## Plan of Work

### 阶段一：建立实现边界和参考基线

先从仓库根目录检查 Python 3.11、`uv`、测试布局和现有模块，不读取或依赖其他未完成执行计划。阅读顺序为：`docs/design-docs/tool-design-guidelines.md` 第 2--12 节，`docs/design-docs/tool-registry-and-execution.md` 第 3--12 节，`docs/design-docs/tools/README.md`，以及三个工具契约全文。随后按需要阅读 `docs/design-docs/overall-architecture.md`、`docs/design-docs/main-loop.md`、`docs/design-docs/context-management.md`、`docs/design-docs/persistence-and-observability.md` 和 `docs/design-docs/textual-ui.md`，确认 composition root、ToolResult 提交、TodoReminder、snapshot/update 的真实边界。

阶段验收是形成一份接口映射：指出每个设计对象对应的现有模块/函数、缺失的能力和需要新增的测试夹具；若代码与设计冲突，按用户请求优先并在 `Decision Log` 记录，不在工具内部绕开公共边界。

### 阶段二：实现 `read_file`

创建 `miniagent/tools/read_file/`。在 `tool.py` 定义 `ReadFileInput(path, offset=0, limit=...)`，严格禁止额外字段，只暴露 `path`、`offset`、`limit`，并校验非空路径、非负 offset、正 limit 及文档规定的范围。定义 `ReadFileMetadata`、`ReadFileData` 和 `ReadFileOutput`，其中 `content` 为带 1-based 行号的当前页，metadata 保存规范化展示路径、完整原始字节 SHA-256、字节/Token/换行/分页统计；不得回显原始 input path 或全文正文。

resolver 只产生一个 `file/read/exact` target，并遵守统一路径规范化、Workspace Root、Protected `.mini` 和受控 ArtifactRef/DocumentRef 规则。classifier 为纯函数并返回 `concurrency_safe=True`。handler 从授权 target 取得文件，使用 `asyncio.to_thread()` 执行同步读取、原始字节哈希、UTF-8（含 BOM）解码和行切分，并协作取消；拒绝目录、二进制、NUL 或无效 UTF-8，稳定处理 LF/CRLF/CR/mixed/none、EOF 和超长单行。

设置 256 KiB、25,000 token、`overflow_behavior="error"` 的 `ResultPolicy`；超限返回 `RESOURCE_EXHAUSTED`，不提交部分正文、不创建 artifact。为 `prompt.py` 写入设计文档规定的英文 Prompt，并在 `__init__.py` 保持窄导出。

本阶段实现前回读 `docs/design-docs/tools/read-file.md` 全文和注册/执行文档第 3.4、3.5、5、6、11 节；测试阶段回读工具指南第 3--12 节以及 `docs/design-docs/tools/write-file.md` 第 2、5 节，验证 SHA-256 能直接作为写入冲突凭据。

### 阶段三：实现 `write_file`

创建 `miniagent/tools/write_file/`。定义严格 `WriteFileInput(path, content, expected_sha256=None)`：trim 后路径非空，UTF-8 编码内容不超过 256 KiB 且不含 NUL，哈希必须是 64 位小写十六进制；不修改换行、Unicode 或 BOM 语义。resolver 只产生一个 `file/write/exact` target，要求直接父目录最终存在且为目录；classifier 固定串行，timeout 15 秒，RetryPolicy 为单次 attempt。

handler 仅使用授权 target。`expected_sha256=None` 走 create-only 的原子创建，目标存在返回 `CONFLICT`；覆盖场景在提交前再次读取原始字节并核对哈希，目标不存在或哈希变化也返回 `CONFLICT`。将完整 UTF-8 字节写入目标同目录、非模型提供名称的临时文件，flush/fsync 后在取消允许时原子替换；尽力清理临时文件。原子提交已开始后无法确认结果的取消/超时返回 `outcome_unknown`，不自动重放。

定义 `WriteFileMetadata` 和 `WriteFileOutput`，content 只返回 created/replaced、字节数和新 SHA-256 的稳定摘要，不回显正文、expected hash 或临时路径；使用默认内联 ResultPolicy。Prompt 使用工具契约中的英文版本。

本阶段实现前回读 `docs/design-docs/tools/write-file.md` 全文、工具指南第 7--12 节和执行文档第 6 节；验证前回读 `docs/design-docs/tools/read-file.md` 第 5--8 节以及现有文件 target/取消测试，确保无隐式重试或权限旁路。

### 阶段四：实现 `TodoStore` 与 `todo_write`

先阅读并落实 `docs/design-docs/tools/todo-write.md` 全文，再阅读 `docs/design-docs/main-loop.md` 第 5、8、12 节，`docs/design-docs/context-management.md` 的 TodoReminder/ToolView 章节，`docs/design-docs/textual-ui.md` 的 snapshot、`TodosChanged` 章节和 `docs/design-docs/persistence-and-observability.md` 的恢复边界。根据现有 composition root 位置新增注入式 `TodoStore`，按 session_id 保存不可变 TodoList；实现原子 replace、当前 Session 读取、同进程切换/重开保留、进程退出丢失，且不从 Journal 回放。

创建 `miniagent/tools/todo_write/`。定义严格 `TodoItem` 与 `TodoWriteInput(todos)`：id 只允许 ASCII 字母/数字/`_`/`-` 且 1--64，content trim 后非空且不超过 500 Unicode 字符，id 唯一，最多一个 `in_progress`，最多 100 项，规范 JSON UTF-8 不超过 32 KiB；空数组执行显式清空。resolver 固定产生 `session_state/write/exact/todos`，classifier 串行，timeout 5 秒，单次 attempt。handler 通过窄 runtime capability 在线性化点交换完整列表，验证或能力失败时旧值不变。

定义共享的 TodoList/TodoItem 模型、`TodoWriteOutput` metadata/data 和摘要 content；ToolResult 被 SessionEngine 接受后发布 `TodosChanged`，snapshot 直接从 TodoStore 读取，UI 只消费结构化字段，不解析 content。AgentLoop 为每个 AgentRun 维护 `model_calls_since_todo_write`：实际 ModelCall 增加，压缩调用不增加，成功提交清零；达到连续 10 次后，从第 11 次起仅在工具仍可见且存在 pending/in_progress 时注入 TodoReminder，新 AgentRun 重置计数。

本阶段修改 composition root、SessionEngine、AgentLoop、ContextManager 或 UI 时，必须保持它们的既有所有权边界；任何新增提醒或事件都应是不可变投影，不写入 Journal、Trace 或恢复事实。

### 阶段五：注册、集成和统一错误协议

在 composition root 的显式工具名列表中按 `read_file`、`write_file`、`todo_write` 加入完成后的工具；确认 Registry 按同名包加载并冻结，schema 只接受 alias，PromptRef 可解析，output model 严格。确认三者的 Provider-visible description 与契约固定文本一致。

把预期业务失败映射为框架封闭的 `ExecutionErrorCode`：read_file 使用 `RESOURCE_UNAVAILABLE`、`UNSUPPORTED_OPERATION`、`RESOURCE_EXHAUSTED`；write_file 使用 `RESOURCE_UNAVAILABLE`、`CONFLICT`、`OPERATION_FAILED`；todo_write 使用安全的参数/状态错误。所有模型可见 code、stage、message 和 recovery 使用英文且不泄露异常、环境值、secret、原始正文或未经处理路径。校验错误、授权拒绝、取消、outcome unknown 和 output protocol error 继续由 Executor 负责。

本阶段回读 `docs/design-docs/tool-registry-and-execution.md` 第 4--6、9--12 节和工具指南第 3--6、10--11 节，核对 retry、failure count、Tool Recovery、inline/artifact 互斥与 ToolView 刷新。

## Concrete Steps

所有命令从 `D:\study\MiniAgent` 执行。实现每个阶段后先运行针对工具的 pytest；若新增依赖，使用 `uv add` 后执行 `uv lock`、`uv sync`，不得手改 `uv.lock`。最终依次运行：

    uv run python -m compileall miniagent tests main.py
    uv run python -m pytest -q

需要观察到 compileall 无错误，pytest 全部通过。若启动 UI 有关集成变更，再运行：

    uv run python -m miniagent.ui

并通过测试夹具或临时目录验证：已知文件可分页读取并得到稳定行号/哈希；先读后写能替换，哈希冲突不会覆盖；TodoList 更新立即反映到 snapshot/事件，进程内重开保留而历史恢复不回填。

## Validation and Acceptance

Registry 测试必须证明三个同名包能加载、Prompt 和 function schema 正确、input/output `extra="forbid"`、malformed arguments 不调用 handler。Target 测试必须覆盖普通 workspace、越界、Protected `.mini`、符号链接/junction、ArtifactRef/DocumentRef 精确读取和 Current Session session_state，确认 capability/scope 不扩大且授权失败不触碰资源。

`read_file` 测试覆盖空文件、offset/limit/EOF、所有换行类型、BOM、NUL、无效 UTF-8、目录/二进制、超长单行、字节和 token 双预算、取消以及并发 classifier；成功结果永不外置。`write_file` 测试覆盖 create-only 不覆盖、父目录缺失无副作用、哈希匹配替换、目标消失/哈希变化冲突、UTF-8/BOM/NUL/换行、临时文件同目录、原子提交、取消/outcome unknown 且无重试。`todo_write` 测试覆盖所有字段约束、原子替换/失败保留旧值、空列表、Session 隔离、同进程生命周期、进程恢复不回放、结构化 UI 投影和第 11 次起的提醒边界。

集成测试还必须证明：写入和 Todo 工具串行，读取可并发；一次 ToolUse 的最终失败只计一次；permission 等待不消耗 timeout，拒绝不触发工具移除；成功清零连续失败；完整 ToolOutput 参与预算且 metadata/data 不能绕过预算。所有文件测试使用 pytest temporary directories，不依赖真实网络、凭据或用户 Session 数据。

## Idempotence and Recovery

计划中的创建和修改均为可重复的增量操作；重复运行测试使用临时目录和独立 Session。若原子写入中途失败，先确认目标内容和同目录临时文件，再清理由本次调用生成且已确认不再使用的临时文件；不得删除用户文件。若发现授权、Session 提交或 UI 接口与设计不一致，暂停该阶段，在 `Surprises & Discoveries` 和 `Decision Log` 记录证据并优先修复公共边界，不把旁路逻辑塞进 handler。

## Artifacts and Notes

最终计划执行应留下三组工具包、TodoStore/投影相关实现、对应 focused/integration tests，以及简短验收记录。示例成功摘要应类似：`Read ... lines ...`、`Created ... (N bytes, sha256: ...)`、`Todo list updated: ...`；这些只是展示协议的观察点，实际路径、计数和哈希由测试产生。

## Interfaces and Dependencies

必须复用现有框架类型：`ToolSpec`、`ToolOutput`、`ToolTarget`、`ExecutionContext`、`ExecutionTraits`、`ToolExecutionError`、`ExecutionErrorCode`、`ResultPolicy`、`TodoReminder`、`TodosChanged` 和 `SessionSnapshot`。新增接口应保持窄且注入式：`TodoStore.replace(session_id, todos)`、`TodoStore.get(session_id)`；工具 handler 形态为 `async def handler(args, context) -> ToolOutput`。文件 I/O 使用 Python 标准库 `pathlib`、`hashlib`、`tempfile`、`os`，同步阻塞部分放入 `asyncio.to_thread()`；除非现有锁文件或设计明确要求，不增加外部依赖。

变更说明（2026-07-24）：初始版本依据用户指定的设计指南、注册执行设计、`PLANS.md` 和三个工具契约编写；遵照用户要求未阅读当前未完成执行计划。
