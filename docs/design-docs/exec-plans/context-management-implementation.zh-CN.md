# 实现上下文管理模块

本 ExecPlan 是一份持续维护的实现计划，必须遵守仓库根目录 `PLANS.md` 的 ExecPlan 约束。实现目标是把当前按字符数、只支持单个摘要的 `ContextBuilder` 演进为完整的 `ContextManager`：每次模型调用前生成准确的 Model Context，在完整请求达到上下文窗口 80% 时按完整消息组执行一次压缩，并通过 `SessionEngine` 追加不可变的 `ContextSummary`。完成后，长对话可以继续运行而不破坏 Transcript；无法通过历史压缩解决的超长请求会明确终止并给出结构化原因。

## Purpose / Big Picture

用户将能够持续进行包含工具调用的长对话。模型每次看到的是当前任务真正需要的固定系统上下文、全部历史摘要、未摘要的有效消息和当前可见工具，而不是被任意字符截断的历史。上下文达到 80% 时系统会自动压缩最早的完整消息组；压缩成功后继续调用模型，压缩结果低于 50% 时视为正常成功，介于 50% 和 80% 时带诊断继续，仍达到 80% 时安全终止，不静默丢弃受保护内容。

可观察的成功标准是：运行 `uv run python -m pytest` 时上下文相关测试全部通过；测试能证明摘要边界不拆开 ToolUse/ToolResult、当前用户消息不进入摘要、旧摘要永久保留、压缩调用不增加 `turn_count`，以及完整请求按输入 token 加预留输出 token 计算。

## Progress

- [x] （2026-07-23）阅读 `AGENTS.md`、`PLANS.md`、上下文管理设计及相关架构文档；完成代码现状盘点。
- [x] （2026-07-23）增加 `ContextSummary` 集合、`WorkingContext`、`PromptInputs`、`AgentRunEnvironment`、`ToolView` 和 `ModelContext` 所需的稳定领域接口。
- [x] （2026-07-23）用 `tiktoken` 实现完整请求计数、预算校验、工具 schema 计数和 provider usage 回填。
- [x] （2026-07-23）实现固定 SystemContext 和每次 ModelCall 的动态 Prompt 组装，保证工具索引、Prompt 和 schema 使用同一个 `ToolView`。
- [x] （2026-07-23）实现按完整 Message Group 选择压缩范围、单次 ContextCompressor 调用、50%/80% 判定和候选摘要提交协议。
- [x] （2026-07-23）将 `AgentLoop` 从 `ContextBuilder` 迁移到 `ContextManager.before_model_call`，保持真实 ModelCall 与 `turn_count` 语义。
- [x] （2026-07-23）补充压缩事件、窄提交适配器、持久化/恢复边界校验和失败边界。
- [x] （2026-07-23）完成 Fake Provider、可控计数器和真实 `tiktoken` 的行为测试，并运行全量验证。

## Surprises & Discoveries

- 现状：`miniagent/context.py` 的 `ContextBuilder` 使用字符长度估算预算，只保留一个 `ContextSummary`，并可能在构建时直接生成摘要；这不满足摘要永久保留和压缩模型调用要求。
- 现状：`miniagent/loop.py` 在每轮循环中直接调用 `build()`/`force_compress()`，且遇到 provider 的 `prompt_too_long` 才强制压缩；新的设计必须把检查点前移到每次 ModelCall 之前。
- 盘点修正：当前 `SessionEngine` 已有 `commit_context_summary(run_id, summary)` 与 `publish_live()`，因此实现只增加绑定当前 `run_id` 的窄适配器，并把严格边界校验放到 Journal 预演/恢复规则中，无需让 ContextManager 接触 Repository。
- 现状：`pyproject.toml` 尚未声明 `tiktoken`；实现 token 计数前需要增加运行时依赖并更新锁文件。
- 实现证据：原基线为 `111 passed`；新增上下文与 Loop 压缩场景并修正审查发现后，全量为 `130 passed in 3.13s`。
- 边界发现：同一 Run 的工具交互允许进入摘要，但当前用户消息必须保留。实现用 `resume_from_message_id` 指向被保护的当前用户消息，在投影时保留该消息并跳过它之后已由摘要覆盖的交互；下一 Run 可再把这条旧用户消息纳入新的摘要。

## Decision Log

- Decision: 在 `miniagent/context.py` 保留模块路径，但把 `ContextBuilder` 迁移为 `ContextManager`，必要时保留一个仅用于兼容旧调用方的适配器。
  Rationale: 设计文档明确现有 `ContextBuilder` 是待演进实现；保留路径可降低 AgentLoop 和测试迁移风险，同时让新契约成为唯一真实行为。
  Date/Author: 2026-07-23 / Codex
- Decision: 使用 `tiktoken` 而不是自有分词器；优先按模型名解析，失败时使用冻结的 `tokenizer_encoding`，默认 `o200k_base`。
  Rationale: 设计要求真实接近 provider 的 token 预算，且自定义字符计数无法计入消息封装、工具 schema 和协议开销。
  Date/Author: 2026-07-23 / Codex
- Decision: 摘要只由 `SessionEngine` 接受后才进入 Working Context；ContextManager 只产生候选摘要和请求提交。
  Rationale: Transcript 是权威事实源，未提交候选不能污染后续 Model Context，也必须支持提交失败后的干净终止。
  Date/Author: 2026-07-23 / Codex
- Decision: 压缩范围以完整 Message Group 为单位，从最后摘要边界之后的最早组逐组扩大，当前用户消息永不进入新摘要。
  Rationale: ToolUse 与 ToolResult 的关联不能被边界拆开，逐组扩大还可以使压缩尽量小并保留近期上下文。
  Date/Author: 2026-07-23 / Codex
- Decision: 本轮不实现具体压缩提示词，运行时只使用 `CONTEXT_COMPRESSION_PROMPT_PLACEHOLDER`。
  Rationale: 用户明确要求先占位、之后再实现具体 prompt；占位仍允许验证独立模型调用、截断处理和摘要提交生命周期。
  Date/Author: 2026-07-23 / Codex
- Decision: 保留 `ContextBuilder = ContextManager` 的临时名称兼容层，但移除旧字符裁剪和隐式字符串摘要行为。
  Rationale: 现有 composition root 与测试仍从原模块名构造对象；别名让迁移保持小步，同时所有实际行为已经只走新契约。
  Date/Author: 2026-07-23 / Codex

## Outcomes & Retrospective

已完成 ContextManager、固定 PromptInputs/SystemContext、AgentRunEnvironment、ToolView、MessageGroup、真实 tiktoken 计数、50%/80% 压缩判定、独立 Provider 压缩调用、SessionSummary 候选提交和 AgentLoop 调用前检查点。新增测试证明压缩调用不增加 turn、ToolUse/ToolResult 不被拆开、当前用户消息不进入摘要、截断/压缩不足/提交失败均不污染 Session；审查后又补齐完整固定输入、工具 prompt 占位、压缩 Trace/usage、真实触发调用 ID、79%/80%/81% 阈值、同一 Run 工具交互压缩、恢复边界损坏校验和 Provider 窗口回退。全量测试为 `130 passed in 3.13s`。

保留的 `ContextBuilder` 只是名称别名，可在 composition root、测试与外部导入全部改用 `ContextManager` 后删除。Provider token 计数仍是 Chat Completions 协议近似值；已通过实际 `prompt_tokens` 差值回填后续预检，但不同供应商的隐藏封装开销仍需用真实流量持续校准。具体压缩 prompt 按用户要求仍为占位，不属于本轮结果。

## Context and Orientation

`miniagent/domain.py` 定义不可变的 `Message`、`Part`、`ToolUsePart`、`ToolResultPart` 和现有 `ContextSummary`。`miniagent/context.py` 当前包含 `WorkingContext` 与旧 `ContextBuilder`。`miniagent/ports.py` 定义 `ModelContext`、`ModelAdapter`、`ToolSpec` 和 `EventSink` 等端口。`miniagent/loop.py` 是 AgentRun 的执行循环，目前负责把消息、工具和模型流串起来。`miniagent/session.py` 的 `SessionEngine` 是事件写入和有效消息投影边界；任何摘要只有在它接受后才是 Working Context 的事实。`miniagent/events.py` 定义可持久化或实时发布的事件载荷。`miniagent/provider/*` 提供 ModelAdapter 和 provider usage/错误信息。

本计划使用以下术语：Transcript 是 SessionEngine 维护的权威有效消息历史；Working Context 是本次 AgentRun 只包含已接受摘要和消息的内存视图；Model Context 是某一次 ModelCall 实际发送给模型的输入投影；Message Group 是不能拆开的完整交互单元，至少包含带 ToolUse 的 AssistantMessage 及其全部 ToolResult；受保护内容是动态 system Prompt、全部历史摘要、当前用户消息和其他设计规定不可通过历史压缩删除的内容。

## 参考文档与阅读顺序

实现前和每个相关里程碑开始时，按以下顺序阅读，并把新发现写回本计划的 `Surprises & Discoveries`：

1. `PLANS.md`：ExecPlan 的自包含格式、里程碑、进度、决策和验收要求。
2. `docs/design-docs/context-management.md`：本模块的权威契约，包括动态 Prompt 顺序、token 预算、压缩生命周期、错误边界和最低测试集。
3. `CONTEXT.md`：Session、AgentRun、Working Context、ContextManager、Model Context 等术语的规范含义。
4. `docs/design-docs/main-loop.md`：AgentLoop 的 ModelCall、工具结果、取消、turn_count 和停止原因语义。
5. `docs/design-docs/overall-architecture.md`：ContextManager、SessionEngine、AgentRunEnvironment 和 ToolView 的系统边界。
6. `docs/design-docs/persistence-and-observability.md`：Message Journal、ContextSummary 持久化、SessionUpdate 与 Trace 的权威性边界。
7. `docs/design-docs/tool-registry-and-execution.md`：ToolView、function schema、工具结果顺序和完整工具交互的约束。
8. `docs/design-docs/openai-compatible-model-provider.md`：ModelAdapter、usage、finish reason、上下文窗口和 provider 错误映射。
9. `docs/reference/agent-prompt-conversation-summarization.md`：作为 ContextCompressor 总结提示词的参考模板，借鉴其按时间顺序提炼用户请求、技术概念、文件/代码、错误修复、问题解决和待办事项的结构。
10. `miniagent/domain.py`、`miniagent/context.py`、`miniagent/ports.py`、`miniagent/loop.py`、`miniagent/session.py`、`miniagent/events.py`：确认实现现状和迁移兼容点。
11. `tests/test_domain_context.py`、`tests/test_loop.py`、`tests/test_session.py` 及 `tests/provider/*`：复用现有 Fake/Scripted Model 测试模式，避免引入真实网络调用。

## Plan of Work

### Milestone 1: 固定领域模型与端口

在 `miniagent/domain.py` 确认 `ContextSummary` 是不可变对象，增加或校验摘要边界递增所需的辅助类型；在 `miniagent/context.py` 定义不可变的 `WorkingContext`（`summaries: tuple[ContextSummary, ...]` 与 `messages: tuple[Message, ...]`）、`PromptInputs`、`SystemContext`、`AgentRunEnvironment`、`ToolView` 和 `ModelContext`。`AgentRunEnvironment` 必须冻结 model、provider、context_window、reserved_output_tokens、tokenizer_encoding、固定 system context 和生成限制。`ToolView` 必须同时携带可见工具的紧凑索引、PromptRef 和 function schema。

在 `miniagent/ports.py` 定义 `ContextManager`、`ContextCommitPort` 和 `ContextCompressor` Protocol。`ContextCommitPort` 只暴露 `commit_context_summary(summary)` 与 `publish_live(update)`；不暴露 Repository 或通用事件总线。所有返回的上下文都是不可变快照。先运行类型/单元测试，确保旧消息序列化和现有循环仍可导入。

### Milestone 2: 实现固定 Prompt 与完整 token 预算

在 `miniagent/context.py` 实现 `ContextManager.start_run(prompt_inputs)`。它按固定顺序生成单条 SystemContext：Identity、行为规则、风险和约束、验证与汇报约束、WorkspaceState、初始 `AGENTS.md`、冻结的时间/时区、全部历史摘要占位规则、工具占位规则。ContextManager 不读取文件、不扫描 workspace、不获取 Git 或操作系统信息；调用方传入的 `AGENTS.md`、WorkspaceState 和时间快照在整个 AgentRun 内保持不变。

实现 `before_model_call(working, environment, tools, session)` 的首次组装路径。system message 中按创建顺序注入全部 ContextSummary；随后加入最后摘要边界之后的原始消息、当前用户消息和保留内容。工具索引、工具 PromptRef 和 function schema 必须都来自同一个 ToolView；不可见工具三者全部排除。工具结果可以只在投影中裁剪，不能修改 Transcript。

新增 `TokenCounter`，使用 `tiktoken.encoding_for_model()`，模型不可解析时回退 `environment.tokenizer_encoding`，默认 `o200k_base`。分别计数消息字段、规范 JSON 工具 schema 和协议封装开销；完整请求预算为输入 token 加 `reserved_output_tokens`。在 `pyproject.toml` 增加 `tiktoken`，运行 `uv lock`。无效窗口、负数预留输出或 tokenizer 配置在发出 ModelCall 前抛出契约错误。

### Milestone 3: 实现压缩选择、调用和提交

新增 Message Group 分组器，确保 AssistantMessage 的全部 ToolUsePart 与对应全部 ToolResult 处于同一组；对连续用户输入、助手响应和工具交互保持整体边界。分组只处理最后摘要边界之后的原始消息，旧 ContextSummary 不作为压缩输入，当前用户消息必须被标记为受保护并排除。

当完整请求占用小于 80% 时直接返回 ModelContext。达到或超过 80% 时，从最旧的完整 Message Group 开始逐组扩大候选范围，并在每次扩大后重新预估保留原始消息、全部摘要、动态 Prompt、工具 schema 和当前用户消息的占用。若不存在可压缩完整组，立即发布 `CompressionFailed`。

实现 `ContextCompressor.compress(source_groups, environment, max_output_tokens)` 的单次独立调用。压缩请求只包含候选原始组和必要工具结果正文，不重复注入完整 system Prompt、AGENTS.md、WorkspaceState、工具索引或旧摘要；不经过 AgentLoop，不生成 AssistantMessage，不增加 `turn_count`，不自动重试。总结提示词以 `docs/reference/agent-prompt-conversation-summarization.md` 为参考，要求压缩模型按时间顺序保留用户明确请求、关键技术概念、重要文件/代码关系、错误及修复、已解决问题和仍待处理事项，保证后续 Agent 能仅凭摘要恢复工作状态。模板中的 `<analysis>` 思维过程要求不直接照搬到运行时输出；摘要只保留可供 Agent 使用的事实和决策，不输出 summary_id、消息边界、token 数、Trace、run_id 或其他内部元数据。`finish_reason=length`、provider 错误、取消或空结果都视为失败。

压缩返回后，把候选摘要与边界加入临时 Working Context，重新组装并用完整计数器计算。占用 `<= 50%` 发布 `CompressionCompleted` 并提交；`50% < 占用 < 80%` 记录“目标不可达”诊断后提交并继续；仍 `>= 80%` 不发目标 ModelCall，发布 `CompressionFailed` 并终止 AgentRun。只有 `commit_context_summary()` 成功后才把摘要加入正式 Working Context；提交失败时丢弃候选且保留既有 Transcript/摘要。

### Milestone 4: 扩展 SessionEngine 与可观测事件

在 `miniagent/events.py` 增加 `CompressionStarted`、`CompressionCompleted`、`CompressionFailed` 或等价的 `SessionUpdate` 载荷，包含设计要求的 trigger model call ID、边界、token 数和失败原因。这些 ID、边界和 token 数只能存在 Journal Record、SessionUpdate 或 Trace，不进入模型看到的摘要正文。

在 `miniagent/session.py` 增加 `commit_context_summary(summary)`：校验 summary ID 唯一、覆盖边界严格递增、边界消息已存在且不重叠，追加 `context_summary` Journal Record 后再更新内存 Working Context，并发布实时更新。实现持久化时遵守 `persistence-and-observability.md` 的 fsync 顺序；任何失败都不得更新 Transcript。补充从 Journal 恢复多个摘要的逻辑，保证物理创建顺序与 Model Context 注入顺序一致。

### Milestone 5: 接入 AgentLoop 并移除旧行为

修改 `miniagent/loop.py`：AgentRun 开始时调用 `start_run()`，每次实际 ModelCall 前调用 `before_model_call()`。删除基于 `compression_used` 的一次性 `force_compress()` 分支；provider 返回 `prompt_too_long` 只能作为 provider 侧保护性错误映射，不能替代调用前 80% 预检。压缩模型调用不计入 `turn_count`，目标 ModelCall 仍在真正发出时递增一次。

AgentLoop 只消费 ContextManager 返回的不可变 ModelContext，不直接修改 Working Context。AssistantMessage 完成、ToolResult 提交后，从 SessionEngine 获取已接受消息视图再交给 ContextManager；草稿、失败流和未提交工具结果绝不能进入上下文。上下文压缩失败映射为明确的 `PROMPT_TOO_LONG` 或上下文预算错误，并按既有 `RunTerminated` 语义收尾。

### Milestone 6: 测试、迁移兼容和清理

在 `tests/test_domain_context.py` 增加动态 Prompt 顺序、固定快照、ToolView 一致性、全部摘要保留、摘要边界和 ToolUse/ToolResult 完整性的测试。在新的 `tests/test_context_management.py` 使用 Fake Provider、可控 TokenCounter 和 Fake ContextCommitPort，覆盖 80% 恰好触发一次压缩、50% 成功、50%-80% 降级、仍达 80% 失败、压缩截断失败、提交失败不污染 Working Context、当前用户消息不被摘要等场景。增加总结模板契约测试：压缩输入包含足够的对话分组信息，生成提示词要求保留用户请求、技术概念、文件/代码、错误修复和待办事项；生成结果不得包含 `<analysis>` 块、摘要内部 ID 或预算诊断。

保留至少一项真实 `tiktoken` 计数测试，验证工具 schema、消息封装和预留输出被计入；provider usage 返回 `prompt_tokens`/窗口时，验证后续预算记录采用实际值。更新 `tests/test_loop.py`，确认压缩调用不增加 turn_count，目标 ModelCall 次数和 `StopReason` 正确。所有测试不得访问网络或真实 API。

若仓库中仍有外部代码调用 `ContextBuilder`，先提供薄适配器并让新测试覆盖其行为等价性；待 `AgentLoop`、测试和入口全部迁移后再删除旧字符计数、隐式摘要和任意尾部裁剪逻辑。清理必须是最后一步，且每次删除后立即运行全量测试。

## Concrete Steps

所有命令均在 `D:\study\MiniAgent` 执行。

1. 阅读参考文档并检查基线：

       uv run python -m pytest

   记录基线测试数量；若 `uv` 不可用，使用项目虚拟环境运行 `python -m pytest`，并在本计划中记录替代命令。

2. 增加 `tiktoken` 依赖并更新锁文件：

       uv add tiktoken
       uv lock

3. 每完成一个里程碑运行聚焦测试和全量测试：

       uv run python -m pytest tests/test_domain_context.py tests/test_context_management.py -q
       uv run python -m pytest

4. 检查静态语法：

       uv run python -m compileall miniagent tests main.py

5. 最终运行全量测试并记录通过数量、压缩场景结果及任何降级诊断：

       uv run python -m pytest -q

预期结果是退出码为 0；上下文专用测试明确显示摘要按顺序提交、压缩调用次数为 1、压缩失败不会出现目标 ModelCall，且旧消息对象未被修改。

## Validation and Acceptance

行为验收必须覆盖以下事实。给定固定 PromptInputs 和 WorkspaceState，多次 ModelCall 的 system context 完全一致；时间、时区和 `AGENTS.md` 不随调用变化。给定一个 ToolView，工具索引、PromptRef 和 schema 的名称集合完全相同，禁用工具在三者中都不存在。给定多个历史摘要，它们全部按创建顺序进入同一个 system message，旧摘要不会被新摘要读取或修改。

构造含 Assistant ToolUse 和对应 ToolResult 的历史，压缩边界只能覆盖完整组；构造当前用户消息后，压缩摘要正文和覆盖边界都不能包含该消息。让可控计数器返回恰好 79%、80% 和 81% 的完整请求占用，验证只有后两者触发压缩且每次最多一次。让压缩结果分别把占用降到 50% 以下、50%-80% 和 80% 以上，验证继续、带诊断继续和终止三种结果。

让压缩 provider 返回长度截断、异常、取消和空结果，验证候选摘要不会被 SessionEngine 接受。让 `commit_context_summary()` 抛出异常，验证 Working Context、Transcript 和旧摘要保持不变。让 provider 返回实际 `prompt_tokens` 与窗口信息，验证下一次预算记录使用实际值但不会追溯阻止已发送请求。

最后运行 `uv run python -m pytest -q`，并确认既有 `tests/test_loop.py`、`tests/test_session.py`、`tests/provider/*` 全部通过。验收不是只看类型或导入成功，而要通过 Fake Provider 的调用记录证明压缩调用独立、不产生 AssistantMessage、不增加 `turn_count`，目标 ModelCall 收到的是重新计数后的完整 Model Context。

## Idempotence and Recovery

重复运行测试和重新构建 Model Context 不修改 Transcript；Model Context 和 Working Context 都是不可变快照。摘要提交使用唯一 `summary_id` 和边界校验，重复事件被 SessionEngine 去重，不会生成重复摘要。压缩失败时只丢弃内存候选，不删除或改写既有 Journal。若依赖安装或某个里程碑中途失败，保留已通过的增量测试，从失败步骤重试；不得使用 Git reset、强制回滚或删除用户文件恢复环境。

恢复测试从包含多个 `context_summary` Journal Record 的 Session 重新打开，验证摘要按创建顺序恢复，后续原始消息从最后边界继续；若 Journal 在摘要提交前失败，恢复结果中不出现该候选摘要。SessionEngine 不在恢复时重新调用压缩模型，也不重放工具副作用。

## Artifacts and Notes

实施过程中只在此追加能证明行为的短证据，例如：

    压缩测试：80% 阈值触发 1 次 ContextCompressor；压缩后 47%，目标 ModelCall 成功发出。
    降级测试：压缩后 63%，记录 target_unreachable，目标 ModelCall 继续发出。
    失败测试：压缩后 82%，未发目标 ModelCall，StopReason=PROMPT_TOO_LONG。
    全量测试：130 passed in 3.13s。

不要把完整 Transcript、API 响应、提示词原文或敏感配置写入本计划。

## Interfaces and Dependencies

最终接口语义至少如下，具体类型可以与现有模块命名适配，但不能削弱边界：

    class ContextManager(Protocol):
        async def start_run(self, prompt_inputs: PromptInputs) -> SystemContext: ...
        async def before_model_call(
            self,
            working: WorkingContext,
            environment: AgentRunEnvironment,
            tools: ToolView,
            session: ContextCommitPort,
        ) -> ModelContext: ...

    class ContextCommitPort(Protocol):
        async def commit_context_summary(self, summary: ContextSummary) -> None: ...
        async def publish_live(self, update: object) -> None: ...

    class ContextCompressor(Protocol):
        async def compress(
            self,
            source_groups: tuple[MessageGroup, ...],
            environment: AgentRunEnvironment,
            max_output_tokens: int,
        ) -> str: ...

    class TokenCounter(Protocol):
        def count(self, context: ModelContext, tools: ToolView) -> int: ...

`AgentRunEnvironment` 必须提供 model、provider、固定 `SystemContext`、`context_window`、`reserved_output_tokens`、`tokenizer_encoding` 和生成限制；`ToolView` 必须是当前 ModelCall 的冻结工具集合。`ContextManager` 依赖 `tiktoken` 和现有 domain/ports 类型，但不得依赖具体 HTTP、UI、Shell、Repository 或可变 Session 历史。压缩 Provider 可以复用现有 `ModelAdapter` 的底层 provider 端口，但必须通过独立的无 AgentLoop 调用表达压缩语义。

变更说明（2026-07-23）：首次创建上下文管理模块中文 ExecPlan。根据 `PLANS.md` 补充自包含目的、参考文档、现状发现、分阶段机制、精确命令、验收、恢复策略和接口约束；当前仅完成计划编写，尚未开始实现。

变更说明（2026-07-23）：根据用户要求加入 `docs/reference/agent-prompt-conversation-summarization.md`。将其作为总结提示词的参考结构，明确运行时只输出可恢复事实，不泄露 `<analysis>` 思维过程、内部 ID、token 预算和 Trace 元数据，并补充相应测试验收。

变更说明（2026-07-23）：完成 ContextManager、tiktoken 预算、动态 ToolView、独立压缩调用、摘要 Journal 提交、AgentLoop 预检和恢复边界实现。按用户要求，压缩与工具 prompt 仅保留显式占位；首次全量测试为 `118 passed in 2.73s`，随后进入双轴代码审查。

变更说明（2026-07-23）：根据 Standards/Spec 双轴审查修正完整 PromptInputs 接入、工具 Prompt 占位、压缩 Trace/usage、触发调用 ID、同一 Run 工具交互压缩、Journal 恢复边界校验和 Provider 窗口回退，补齐边界与失败测试；最终全量测试为 `130 passed in 3.13s`。
