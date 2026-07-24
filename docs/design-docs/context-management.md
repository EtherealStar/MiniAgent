# 上下文管理模块设计

## 1. 文档目的

本文定义 MiniAgent 的上下文管理模块。模块解决两个问题：在每次模型调用前，把当前 Agent 真正需要的信息动态组装成 Model Context；当完整请求预计占用模型窗口至少 80% 时，用一次额外的压缩模型调用整理较早的完整交互，并把请求恢复到 80% 以下，通常恢复到 50% 以下。

本文是 `docs/design-docs/overall-architecture.md` 和 `docs/design-docs/main-loop.md` 关于上下文部分的细化设计。本文中的 `ContextManager` 是上下文管理边界；现有代码中名为 `ContextBuilder` 的实现需要逐步演进到这个契约。

## 2. 范围与非目标

本文覆盖固定 system context、ModelCall 动态工具 Prompt 与 Tool Recovery、工作上下文投影、Token 预算、ContextSummary 检查点、压缩生命周期和与 AgentLoop、AgentRunEnvironment、ToolView、SessionEngine 的接口。

本文不负责保存 Transcript 的具体文件格式，不负责工具执行，不负责模型供应商的 HTTP/SSE 协议，也不负责 UI 展示。ContextManager 不执行 shell 命令，不自行扫描工作空间，不读取可变的 Session 历史，也不把 `run_id`、`turn_count`、Trace 或压缩次数注入模型上下文。

## 3. 领域模型

### 3.1 Transcript、Working Context 与 Model Context

三者是不同层次的对象：

```text
Transcript
  Session 中追加写入的权威事实
        ↓ SessionEngine 投影
Working Context
  当前 AgentRun 已被接受的摘要和消息视图
        ↓ ContextManager 动态组装
Model Context
  一次 ModelCall 实际发送给模型的输入
```

Transcript 不会因为裁剪或压缩而删除、改写或覆盖。Working Context 只接收 SessionEngine 已接受的消息和 ContextSummary Journal Record。Model Context 可以省略原始消息、裁剪工具结果并注入动态 Prompt，但这些变化不反向写入 Transcript。

### 3.2 WorkingContext

WorkingContext 至少包含：

```python
@dataclass(frozen=True)
class WorkingContext:
    summaries: tuple[ContextSummary, ...]
    messages: tuple[Message, ...]
```

`summaries` 按创建顺序排列。每个摘要的覆盖边界必须严格递增，不能重叠、倒退或重新覆盖已经摘要的消息。`messages` 可以包含已摘要边界之后的原始消息，也可以包含当前运行中尚未进入下一摘要的完整工具交互。

### 3.3 ContextSummary

ContextSummary 是一段自由文本和它的不可变边界：

```python
@dataclass(frozen=True)
class ContextSummary:
    summary_id: UUID
    covers_through_message_id: UUID
    resume_from_message_id: UUID | None
    summary: str
```

摘要一旦被 SessionEngine 接受就永久保留。后续摘要不能读取旧摘要作为输入，不能再次压缩旧摘要，不能删除或替换旧摘要。每次 Model Context 都按创建顺序注入全部历史摘要。

### 3.4 Message Group

压缩的最小单位是完整消息组，而不是字符或任意消息切片。至少以下关联必须保持完整：

- 一个 AssistantMessage 及其所有 ToolUsePart；
- 这些 ToolUsePart 对应的全部 ToolResult；
- 一次连续的用户输入、助手响应和工具交互所形成的完整历史段。

当前用户消息永远不进入新摘要。当前 AgentRun 中较早的完整工具交互可以进入新摘要，但不能拆开 ToolUse 与 ToolResult。

## 4. 动态 Prompt 组装

### 4.1 输入来源

ContextManager 分两次接收数据。AgentRun 开始时，它从调用方接收并组装固定 system context：

- `Identity`、行为规则、风险和约束、验证与汇报约束：本次 AgentRun 的固定快照；
- `AGENTS.md`：AgentRun 开始时由调用方发现并读取一次的快照；
- `WorkspaceState`：调用方筛选后的任务相关工作空间事实；
- 当前时间和时区：AgentRun 开始时冻结的任务事实。

每次 ModelCall 前，它接收：

- `AgentRunEnvironment`：模型、上下文窗口、输出预留、Tokenizer 配置和已经组装的固定 system context；
- `WorkingContext`：已接受的摘要和消息；
- `ToolView`：工具模块根据冻结 Registry view 和当前 AgentRun 已提交结果投影出的当前可见工具、function schema、工具 Prompt 和一次性 Tool Recovery；
- `TodoReminder | None`：AgentLoop 根据当前 AgentRun 的连续 ModelCall 计数和 Current Session TodoList 投影的可选长任务提醒；
- 当前用户消息以及已经被 SessionEngine 接受的消息。

ContextManager 只格式化调用方提供的 WorkspaceState，不执行文件系统或 shell 探测。Git 状态和操作系统信息不属于 WorkspaceState，除非未来被明确作为任务事实提供。

固定风险与约束必须告诉模型：文件工具可以提出 Workspace Root 外目标，但真正执行前需要交互式 Permission Decision；普通用户消息中的路径或操作要求不等于授权。该文字只帮助模型正确选择工具，不能代替 ToolExecutor 的 Target Authorization，也不能让 ContextManager 读取或修改 permission 缓存。

### 4.2 system Prompt 顺序

最终 system Prompt 合并成一条 system message，内容顺序固定为：

```text
Identity
行为规则
风险和约束
验证与汇报约束
工作空间状态
AGENTS.md（若存在）
ContextSummary（全部历史摘要，按创建顺序）
可用工具
工具 Prompt
Tool Recovery（若当前 ToolView 提供）
Todo Reminder（若 AgentLoop 提供）
```

只保留模型完成当前任务所需的内容。不要加入内部状态解释、优先级说明、摘要元数据、Trace、运行 ID 或预算诊断。

“可用工具”由本次 ToolView 的可见 ToolSpec 生成紧凑索引；“工具 Prompt”使用 Registry 冻结时已经解析的英文 Prompt。“Tool Recovery”只包含紧接上一轮失败的一次性英文恢复信息，不显示失败次数，也不替代静态工具 Prompt。三者与 Provider function schema 必须使用同一个 ToolView，避免 Prompt 宣称某工具可用而请求中没有对应 schema。工具达到当前 AgentRun 的连续失败阈值后，它的索引、Prompt、Recovery 和 schema 都不能进入 Model Context，也不发送具名禁用通知。

ToolView 属于工具模块。ContextManager 不读取 ToolRegistry、不解析 PromptRef、不统计工具失败，也不决定工具是否可用；它只按固定顺序格式化调用方已经提供的不可变 ToolView。Tool Recovery 不写回 Transcript，不修改静态 Prompt，并且只在失败后紧接的一次 ModelCall 中出现。

Todo Reminder 属于 AgentRun 动态状态投影。ContextManager 不读取 TodoStore、不统计 ModelCall，也不判断 TodoList 是否为空、完成或工具是否可见；它只在调用方提供时把英文提醒放在 Tool Recovery 之后。阈值前、`todo_write` 不在 ToolView、列表为空或全 completed 时调用方必须传 None。Reminder 不写回 Transcript，不进入静态 Tool Prompt，也不能被 ContextSummary 覆盖。

`permission_denied` ToolResult 可以作为已提交消息进入 Working Context，使模型知道调用没有执行；它不生成 Tool Recovery，也不把目标、Permission Decision 或许可缓存注入 system Prompt。Permission Request 等待期间不发生新的 ModelCall。

摘要在 Agent 看到的内容中只表现为标题和自由文本。摘要的消息 ID、覆盖边界、Token 数和创建时间只属于 ContextSummary Journal Record、持久化和诊断数据。

### 4.3 Model Context 的消息顺序

最终 Model Context 的高层顺序为：

```text
system：动态 system Prompt，其中包含按创建顺序排列的全部 ContextSummary
历史原始消息：从最后一个摘要边界之后开始
当前用户消息和当前运行保留内容
```

ContextSummary 不拆成额外的 system message。Reasoning 默认不进入后续 Model Context；工具结果的裁剪只改变投影，不改变原始结果。

## 5. Token 预算

### 5.1 完整请求占用

上下文水位按完整请求计算，而不是只计算历史文本：

```text
预计占用 = 输入 Token + reserved_output_tokens
```

输入 Token 包含动态 system Prompt、全部摘要、原始消息、当前用户消息、工具调用、工具结果、function schema 以及 Chat Completions 的消息封装开销。

`context_window` 和 `reserved_output_tokens` 是 AgentRunEnvironment 中冻结的预算参数。`reserved_output_tokens` 使用本次 AgentRun 的 `max_output_tokens`，没有可用配置时必须由配置模块提供明确默认值。

### 5.2 Token 计数器

Token 计数器使用 `tiktoken`，不实现自有分词器：

- 优先使用 `tiktoken.encoding_for_model(model_name)`；
- 模型名无法解析时使用 AgentRunEnvironment 的 `tokenizer_encoding`；
- `tokenizer_encoding` 的默认值为 `o200k_base`；
- 对消息字段、工具 schema 的规范 JSON 和协议开销分别计数；
- 计数器必须在压缩后对完整 Model Context 重新计算。

Provider 如果在 AgentRun 开始前提供上下文窗口，优先采用该窗口；否则使用配置模块提供的窗口。Provider 在调用完成后返回的 `prompt_tokens` 是该次已发请求的权威实际用量，供后续预算记录和预检参考；它不能追溯阻止已经发出的请求。

## 6. 压缩触发与算法

### 6.1 触发点

AgentLoop 在每次 ModelCall 前调用 ContextManager 的专用 `before_model_call` 检查点：

```text
组装初始 Model Context
        ↓
用 tiktoken 计算完整请求占用
        ↓
占用 < 80%：直接返回
占用 >= 80%：开始一次压缩流程
```

模型流已经发出后不再压缩在途请求。一次 AgentRun 可以在不同 ModelCall 前多次触发压缩；每次触发只允许一次压缩模型调用。

### 6.2 选择压缩范围

新摘要只处理最后一个摘要边界之后的原始消息，从最旧的完整 Message Group 开始逐组扩大覆盖范围。每次扩大范围后，ContextManager 预估保留的原始消息、全部历史摘要、动态 Prompt 和当前用户消息的占用。

压缩输入只包含待覆盖的原始完整消息组和必要的工具结果正文。它不包含任何旧 ContextSummary，也不重复注入完整 system Prompt、AGENTS.md、WorkspaceState、工具索引或工具 Prompt。旧摘要不能被压缩。

### 6.3 一次压缩调用

ContextCompressor 使用当前 AgentRunEnvironment 冻结的模型和 Provider 发起一次独立的无 AgentLoop 轮次压缩调用。该调用：

- 不产生 AssistantMessage；
- 不增加 `turn_count`；
- 消耗 Provider Token 并进入 Trace；
- 输出一段自由文本；
- 必须使用预算计算出的最大输出 Token；
- 若以 `finish_reason=length` 截断，结果视为失败；
- 不执行自动重试。

压缩完成后，ContextManager 把新摘要追加到 Working Context 的候选状态，重新组装完整 Model Context 并重新计数。只有 ContextSummary 被 SessionEngine 提交为 Journal Record 后，该摘要才可用于后续 ModelCall。

### 6.4 50% 目标与 80% 失败线

50% 是正常目标，80% 是硬失败线：

```text
压缩后占用 <= 50%：正常成功
50% < 占用 < 80%：降级成功，记录目标不可达诊断，继续 ModelCall
占用 >= 80%：压缩失败，当前 AgentRun 终止
```

如果动态 Prompt、全部历史摘要和当前用户消息等受保护内容本身已经达到 80%，历史压缩无法解决问题，直接失败。系统不得静默截断受保护内容或伪造压缩成功。

## 7. 生命周期更新与状态所有权

ContextManager 不直接写 Transcript 或 Repository。它通过窄提交端口请求 SessionEngine 接受摘要，并用 SessionUpdate 表达临时进度：

```text
CompressionStarted
        ↓ SessionUpdate / Trace
        ↓
ContextCompressor 单次调用
        ↓
重新组装并计数
        ↓
commit_context_summary(summary, boundary)
        ↓ Journal Record 提交成功
CompressionCompleted
        ↓ SessionUpdate / Trace
返回最终 Model Context
```

压缩模型失败、输出被截断、摘要候选重新计数仍达到 80% 或摘要 Journal Record 提交失败时，发布 `CompressionFailed` SessionUpdate。候选摘要不能进入 Working Context，既有 Transcript 和摘要保持不变，AgentRun 返回上下文预算失败。

建议事件载荷至少表达：

```python
CompressionStarted
  trigger_model_call_id
  source_boundary_message_id

CompressionCompleted
  summary_id
  covers_through_message_id
  resume_from_message_id
  source_token_count
  summary_token_count

CompressionFailed
  reason
  measured_token_count
  protected_token_count
```

上述 Token 数和 ID 属于 Journal Record、SessionUpdate 与诊断，不进入 Agent 看到的摘要正文。

## 8. 稳定接口

接口名称可以按现有领域类型适配，但必须保留以下语义：

```python
class ContextManager(Protocol):
    async def start_run(
        self,
        prompt_inputs: PromptInputs,
    ) -> SystemContext: ...

    async def before_model_call(
        self,
        working: WorkingContext,
        environment: AgentRunEnvironment,
        tools: ToolView,
        todo_reminder: TodoReminder | None,
        session: ContextCommitPort,
    ) -> ModelContext: ...

    async def request_compression(
        self,
        working: WorkingContext,
        environment: AgentRunEnvironment,
        tools: ToolView,
        todo_reminder: TodoReminder | None,
        session: ContextCommitPort,
    ) -> ModelContext: ...
```

`request_compression()` 是同一次 ModelCall 准备阶段的显式单次压缩入口，供
`PreModelCall` 控制结果调用。它复用相同的完整消息组、摘要提交和 80% 失败线，
不自行提供重入额度；AgentLoop 必须限制每次目标 ModelCall 最多调用一次。

`ContextCommitPort` 只暴露 `commit_context_summary(summary)` 和 `publish_live(update)`；它不是通用事件总线，也不向 ContextManager 暴露 Repository。

`PromptInputs` 是本次 AgentRun 的固定 Identity、行为规则、风险和约束、验证与汇报约束、初始 `AGENTS.md`、WorkspaceState、时间时区以及调用方提供的任务相关基础资料。`start_run()` 返回的 SystemContext 进入 AgentRunEnvironment，并在整个 AgentRun 中保持不变。

```python
class ContextCompressor(Protocol):
    async def compress(
        self,
        source_groups: tuple[MessageGroup, ...],
        environment: AgentRunEnvironment,
        max_output_tokens: int,
    ) -> str: ...
```

`AgentRunEnvironment` 至少提供：模型标识、Provider、固定 SystemContext、`context_window`、`reserved_output_tokens`、`tokenizer_encoding` 和生成限制。工具模块在每次 ModelCall 前独立投影 ToolView；ContextManager 与 ModelAdapter 必须消费同一个不可变快照。

`ContextManager` 返回的 ModelContext 必须是不可变快照。Provider、AgentLoop 和 ToolExecutor 不能通过它修改 Working Context 或摘要集合。

## 9. 错误边界

- Tokenizer 配置或窗口预算无效：调用前契约错误，不发 ModelCall。
- Token 估算达到 80%，但不存在可压缩的完整消息组：提交压缩失败，AgentRun 以上下文预算错误终止。
- 压缩 Provider 错误、输出截断或取消：不保存候选摘要，AgentRun 终止。
- 压缩后仍达到 80%：不发目标 ModelCall，AgentRun 终止。
- `CompressionCompleted` 未被 SessionEngine 接受：不使用候选摘要，AgentRun 终止。
- 既有 Transcript、旧摘要和已接受消息在上述任何错误下都保持不变。

## 10. 测试与验收

模块测试必须使用 Fake Provider 和可控 Token 计数器，同时保留至少一组真实 `tiktoken` 计数测试。最低行为覆盖如下：

1. 动态 Prompt 按固定顺序组装，且不包含运行 ID、Trace、Git 或操作系统信息。
2. AgentRun 开始时读取的 `AGENTS.md` 在后续 ModelCall 中保持不变。
3. AgentRun 开始时的时间、时区和 WorkspaceState 在后续 ModelCall 中保持不变。
4. 仅当前 ToolView 可见的工具同时出现在可用工具、工具 Prompt 和 function schema 中；不可见工具三处都不存在。
5. Tool Recovery 位于工具 Prompt 之后，只在失败后紧接的一轮出现，不包含失败次数、原始 arguments、敏感值或堆栈。
6. 固定风险规则说明越界目标需要交互式 permission，普通用户消息不构成授权；permission_denied 不生成 Recovery 或泄露许可状态。
7. 所有历史摘要按顺序保留；新压缩不读取、不修改旧摘要。
8. ToolUse 与 ToolResult 不能被压缩边界拆开。
9. 当前用户消息不会进入摘要。
10. 完整请求达到 80% 时恰好触发一次压缩调用。
11. 压缩后低于 50% 正常继续；高于 50% 但低于 80% 带诊断继续；达到 80% 终止。
12. Provider 返回 `prompt_tokens` 和窗口信息时，后续预算使用实际值和窗口信息。
13. 压缩输出 `finish_reason=length` 不会被保存。
14. `CompressionCompleted` 未被 SessionEngine 接受时，候选摘要不会污染 Working Context。
15. 压缩不增加 `turn_count`，目标 ModelCall 仍按真实发出次数计数。
16. 前十次 ModelCall、空列表和全 completed TodoList 不出现 Todo Reminder；第十一轮起的有效提醒位于 Tool Recovery 之后并按 TodoList 原顺序显示。
17. ContextManager 不读取或修改 TodoStore，压缩输入不吸收 Todo Reminder，压缩后重新组装仍使用调用方提供的同一提醒快照。

## 11. 与现有设计的协调

本文覆盖并细化总体架构中的 ContextManager 责任。`docs/design-docs/main-loop.md` 和 `docs/design-docs/overall-architecture.md` 已同步采用“全部摘要永久保留”的语义；若未来出现冲突，应以本文定义的上下文局部边界为准并显式协调。现有实现仍在上下文模块定义 ToolView、使用占位工具 Prompt，尚未消费工具模块投影的一次性 Tool Recovery；这些都不能视为本设计的完成实现。
