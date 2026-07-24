# MiniAgent 上下文

本上下文描述 MiniAgent 会话与单次 Agent 执行中的核心概念。它用于统一主循环、消息历史、模型调用和工具调用之间的语言。

## Language

**Session（会话）**:
用户与 Agent 的一段持续交互，包含多次 AgentRun 以及它们产生的消息和事件。
_Avoid_: 对话轮次、单次调用

**SessionEngine（会话引擎）**:
Session 的所有者，也是会话事件有序记录与消息历史投影的边界。
_Avoid_: AgentLoop、模型循环

**SessionRepository（会话仓库）**:
发现、创建和打开本地 Session 的持久化边界。它返回带独占写入权的 Session handle，但不拥有 AgentRun 或内存输入队列。
_Avoid_: SessionEngine、Session 目录扫描器、聊天缓存

**Current Session（当前会话）**:
用户当前打开并交互的唯一 Session。切换当前会话会停止旧会话，不存在后台运行的 Session。
_Avoid_: Displayed Session、后台 Session

**Queued Input（排队输入）**:
SessionEngine 已接受并分配消息与运行身份、但尚未开始 AgentRun 的内存输入。它不是 Transcript 事实，切换、关闭或进程中断时可以丢失。
_Avoid_: UserMessage、已提交消息、持久化队列

**Session Update（会话更新）**:
SessionEngine 面向当前进程发布的通知，既可表达排队状态和流式草稿，也可表达已提交消息、运行终态和待处理 Permission Request；它不参与恢复。
_Avoid_: Journal Record、Trace Record、UI Update

**UI Projection（UI 投影）**:
Textual UI 根据 Session snapshot 和 Session Update 派生的非权威展示状态，只服务于 Current Session。
_Avoid_: Transcript、消息历史所有者

**TodoList（任务列表）**:
Current Session 在当前应用进程中的结构化任务状态，按提交顺序保存待处理、进行中和已完成事项；它不是 Transcript 或恢复事实。
_Avoid_: 项目 TODO 文件、执行计划、历史消息

**TodoStore（任务状态仓库）**:
应用进程内按 Session identity 保存 TodoList 的权威边界；同一进程重开 Session 时保留，进程退出后丢失。
_Avoid_: Message Journal、SessionRepository、模块全局字典

**Slash Command（斜杠命令）**:
位于输入开头、由公开命令集合精确识别的用户界面操作；未匹配的斜杠文本仍是普通用户输入。
_Avoid_: 内部 Action、工具调用

**Tool Presentation（工具展示）**:
由 ToolUse 和 ToolResult 派生的用户可读摘要及可展开正文，不暴露工具协议、原始参数或关联标识。
_Avoid_: ToolResult、调试输出

**AgentRun（Agent 运行）**:
由一条不可变用户输入触发的完整 Agent 执行，可能包含多次 ModelCall 和工具使用，直到产生明确的停止原因。
_Avoid_: AI 调用、模型调用、Turn

**AgentLoop（Agent 循环）**:
承载一个 AgentRun 的内层执行边界。
_Avoid_: Session、SessionEngine

**AgentRunEnvironment（Agent 运行环境）**:
SessionEngine 在 AgentRun 开始时收集各模块已经组装好的不可变固定上下文、模型和运行限制快照。
_Avoid_: ToolView、可变全局配置

**ToolView（工具视图）**:
工具模块根据冻结的工具注册表和当前 AgentRun 已接受的工具结果，在一次 ModelCall 前动态组装的不可变可见工具、schema、Prompt、恢复提示和执行定义快照。
_Avoid_: ToolRegistry、固定工具集合

**ModelCall（模型调用）**:
一次发送给模型的请求及其对应响应流。每次 ModelCall 使用一个新的 AssistantMessage 身份，但失败或作废的调用不会产生有效的已完成消息。
_Avoid_: AgentRun、Turn

**Model Provider（模型供应商）**:
通过 OpenAI-compatible 协议提供模型推理能力的外部服务。MiniAgent 在同一时刻只使用一个已配置的 Model Provider。
_Avoid_: ModelAdapter、模型客户端

**Provider Connection Configuration（供应商连接配置）**:
定位并认证一个 Model Provider 所需的不可变连接快照，不包含具体 AgentRun 使用的 model_id。
_Avoid_: AgentRunEnvironment、RuntimeConfig

**RuntimeConfig（运行配置）**:
应用当前使用的全局 model_id 和其他可变运行默认值；AgentRun 开始时将所需值冻结进 AgentRunEnvironment。
_Avoid_: Session 配置、Provider Connection Configuration

**ModelAdapter（模型适配器）**:
ModelCall 与 Model Provider 之间的协议边界，将 Model Context 和工具定义转换为供应商请求，并将供应商响应转换为 ModelEvent。
_Avoid_: Model Provider、AgentLoop

**ModelEvent（模型事件）**:
ModelAdapter 输出的单个规范化流增量或终态，供 AgentLoop 组装 AssistantMessage 并决定后续控制流。
_Avoid_: Journal Record、Session Update、SSE chunk

**Journal Record（日志记录）**:
SessionEngine 已接受并追加到 Message Journal 的恢复事实，例如完整消息、工具结果和 AgentRun 终态。
_Avoid_: Session Update、ModelEvent、Trace Record

**Turn（模型轮次）**:
`turn_count` 所计量的单位，即一次实际发出的 ModelCall；工具执行、Hook 和上下文处理都不是 Turn。
_Avoid_: 用户轮次、工具轮次

**Transcript（会话记录）**:
SessionEngine 在内存中维护的有效消息历史；它由 Message Journal 恢复，诊断细节由 Trace Record 承担。
_Avoid_: Model Context、Working Context

**Message Journal（消息日志）**:
Session 的唯一持久化事实源，按物理记录顺序保存完整消息、工具结果和运行终态；其物理文件为 `message.jsonl`。
_Avoid_: Transcript、Trace、聊天缓存

**Trace Record（追踪记录）**:
描述 AgentRun、ModelCall 或工具执行诊断细节的非权威记录；可异步写入、丢失或轮转，不参与 Session 恢复。
_Avoid_: Journal Record、Message Journal、恢复事实

**Trace Span（追踪跨度）**:
具有父子关系、开始和结束时间的 Trace Record 容器，用于表示 AgentRun、Turn、ModelCall 或 ToolCall 的持续活动。
_Avoid_: Turn、Journal Record

**Working Context（工作上下文）**:
一个 AgentRun 在内存中维护的、仅包含已被 SessionEngine 接受内容的消息视图；它包含按创建顺序保留的 ContextSummary 集合和未被摘要覆盖的有效消息。
_Avoid_: Transcript、完整历史

**ContextManager（上下文管理器）**:
在 AgentRun 开始时组装固定 SystemContext，并针对每次 ModelCall 结合 ToolView 动态组装 Model Context 的边界；它不拥有 Transcript。
_Avoid_: Transcript、SessionEngine、静态 Prompt

**SystemContext（系统上下文）**:
ContextManager 在 AgentRun 开始时组装并冻结的身份、规则和全局任务事实，不包含随 ModelCall 变化的工具内容。
_Avoid_: Model Context、原始全局变量

**Model Context（模型上下文）**:
ContextManager 针对一次 ModelCall，从工作上下文选择有效消息，注入任务所需的动态 Prompt、全部历史 ContextSummary、工具定义和必要的裁剪结果后得到的输入投影。
_Avoid_: Transcript、原始历史

**ContextSummary（上下文摘要）**:
替代一段较早完整消息组进入 Model Context 的不可变自由文本检查点，其覆盖范围由消息 ID 边界标识；已创建的摘要永久保留、按创建顺序注入，不能被后续摘要再次压缩、删除或替换。
_Avoid_: 历史消息、原地压缩

**Message（消息）**:
Session 历史中的完整交流单元，具有全局唯一 ID，并由一个或多个有序 Part 组成。
_Avoid_: Event、流式分块

**Part（消息部分）**:
Message 内保持原始输出顺序的结构化内容单元，例如文本、思考过程或工具使用。
_Avoid_: Message、Event

**ToolUse（工具使用）**:
AssistantMessage 中由模型提出的一次工具调用意图，通过全局唯一的 `tool_use_id` 标识。
_Avoid_: ToolResult、工具消息

**ToolResult（工具结果）**:
外部工具执行层针对一个 `tool_use_id` 返回的终态信封，关联成功的 ToolOutput 或结构化 ToolFailure，并携带必要的执行状态。
_Avoid_: ToolUse、AgentRunResult

**ToolOutput（工具业务输出）**:
工具成功执行后返回的结构化业务结果，包含模型可见的 `content` 以及供 UI、Trace 或 SDK 使用的 `metadata` 和 `data`；它不表达执行失败或重试状态。
_Avoid_: ToolResult、ToolResultPart

**Tool Recovery（工具恢复提示）**:
紧随工具失败后的下一次 ModelCall 中提供给模型的一次性恢复信息，说明失败阶段、原因和下一步调用建议；它不修改工具的静态使用规则。
_Avoid_: Tool Prompt、ToolFailure

**Tool Availability（工具可用性）**:
一次 AgentRun 中根据已提交工具结果投影出的工具是否仍可进入下一次 ToolView；连续最终失败达到阈值时，工具从后续 ToolView 中移除。
_Avoid_: ToolRegistry、ToolView

**Draft AssistantMessage（Assistant 草稿消息）**:
尚未收到完整模型响应、只由流式增量构成的临时 AssistantMessage 投影。
_Avoid_: 已完成消息

**Discard（作废）**:
使草稿消息退出有效消息历史和后续上下文的运行时事实。它可以作为 Session Update 或 Trace Record 存在，但不写入 Message Journal。
_Avoid_: 物理删除、回滚事件

**AgentRunResult（Agent 运行结果）**:
一个 AgentRun 结束时返回的结构化终止结果，包含停止原因和必要的运行统计。
_Avoid_: ToolResult、AssistantMessage

**ToolRegistry（工具注册表）**:
启动阶段由 composition root 构建并冻结的工具定义集合；按全局唯一短名解析工具，并提供当前会话的不可变启用视图。
_Avoid_: 全局可变工具表、ToolExecutor

**ToolSpec（工具定义）**:
一个工具的不可变注册描述，包含名称、输入输出模型、异步 handler、Prompt 引用、目标解析器、执行策略以及派生 schema。
_Avoid_: ToolUse、工具实例

**ToolExecutor（工具执行器）**:
负责调用前快筛、Pydantic 校验、目标解析与授权、执行重试、取消、结果封装和批次调度的执行边界。
_Avoid_: ToolRegistry、AgentLoop

**ToolInput（工具输入）**:
由工具自己的 Pydantic 模型定义、经过严格校验后传给 handler 的业务参数；框架包装字段在进入 ToolInput 前剥离。
_Avoid_: 原始 arguments、ToolTarget

**ToolTarget（工具目标）**:
由已验证 ToolInput 派生的内部资源目标，描述文件、目录、外部服务、Current Session 状态或受控引用的规范化位置、所需 Target Capability 和 Target Scope。
_Avoid_: 模型参数、未经解析的路径

**Workspace Root（工作空间根目录）**:
Current Session 启动时冻结的本地目录边界；位于其中的文件目标不需要越界许可。
_Avoid_: 实时 cwd、进程工作目录

**Target Capability（目标能力）**:
ToolUse 对 ToolTarget 要求的操作能力，固定分为 read、write 和 delete；一种能力不隐含另一种能力。
_Avoid_: 工具权限、operation 字符串

**Target Scope（目标范围）**:
Target Capability 生效的位置范围，分为仅目标自身的 exact 和包含全部后代的 subtree。
_Avoid_: 路径 glob、隐式递归

**Target Authorization（目标授权）**:
在 handler 执行前统一裁决全部 ToolTarget 的执行边界；它自动允许普通 Workspace Root 内目标、Current Session 状态、受控引用和已配置的固定外部只读服务，并为越界、受保护或本地内容外传目标取得 Permission Decision。
_Avoid_: Execution Classification、PreToolUse Hook、路径校验散点

**Protected Workspace Subtree（受保护工作空间子树）**:
Workspace Root 内仍需显式 Permission Decision 才能作为 ToolTarget 访问的目录树；普通祖先目录的递归工具必须跳过它，只有显式把它作为目标时才请求许可。
_Avoid_: 越界目录、隐藏目录、自动拒绝目录

**Permission Request（许可请求）**:
一个 ToolUse 含有尚未获准的越界 ToolTarget 或 Protected Workspace Subtree 时，等待用户对该调用全部待授权目标作出整体裁决的内存状态。
_Avoid_: ToolUse、持久化审批记录

**Permission Decision（许可裁决）**:
用户对一个 Permission Request 作出的拒绝、仅此次同意或 Current Session 同意。
_Avoid_: 用户消息中的操作要求、工具失败

**Session Permission Grant（会话许可）**:
Current Session 内存中按规范化目标、Target Capability 和 Target Scope 保存的越界或受保护目标许可；SessionEngine 生命周期结束时失效。
_Avoid_: 永久许可、配置项、Journal Record

**ExecutionTraits（执行特征）**:
目标解析后由无副作用分类器计算的执行元数据。只有明确无副作用的只读调用才可标记 `concurrency_safe=True`。
_Avoid_: 任意并发许可

**ToolFailure（工具失败）**:
模型可见的结构化预期失败，包含 code、stage、字段错误以及 correctable/retryable 等裁决信息；程序不变量破坏不属于 ToolFailure。
_Avoid_: 未分类异常

**ExecutionErrorCode（执行错误码）**:
工具 handler 对预期执行失败使用的框架级封闭分类；具体原因由安全 message 表达，工具不能自创顶层错误码。
_Avoid_: Python 异常类型、工具私有错误字符串

**ToolCorrection（工具修正）**:
针对参数校验失败的同一 ToolUse 发起的一次修正调用，通过 `correction_of_tool_use_id` 关联原调用；每个原调用最多一次。
_Avoid_: 执行重试、链式修正

**ArtifactRef（结果引用）**:
由 ArtifactStore 为外置的大工具结果生成的受控引用，指向 Session 下 `tool_result/<tool_use_id>/` 中的最终结果文件。
_Avoid_: 工具任意本地路径

**DocumentRef（文档引用）**:
由 DocumentCache 为已完成文档转换生成的 Current Session 受控引用，精确指向可由文件读取工具消费的 Markdown。
_Avoid_: MinerU 任务、任意 `.mini` 路径、原始文档路径

**Hook（生命周期插入点）**:
绑定 AgentRun 或其内部流程某个明确生命周期时机的可扩展入口；Hook 名称只描述触发时机，不描述扩展实现的业务功能。
_Avoid_: 按功能命名的回调、任意中间件

**PreModelCall（模型调用前插入点）**:
在 Model Context 首次组装后、Model Provider 发起请求前触发的 Hook 时机；它可以请求 ContextManager 压缩上下文或终止调用，但不直接修改上下文。
_Avoid_: ContextManager、模型调用策略

**PreToolUse（工具使用前插入点）**:
在模型产生 ToolUse、ToolExecutor 执行 handler 前触发的 Hook 时机；当前用于接入工具设计规定的快速 JSON/schema 检查。
_Avoid_: ToolExecutor、参数替换

**AssistantMessageCompleted（助手消息完成插入点）**:
AssistantMessageCompleted 被 SessionEngine 接受后触发的通知时机；它只表达生命周期事实，不改变已提交消息或 AgentLoop 决策。
_Avoid_: ModelEvent、Draft AssistantMessage

**PostToolUse（工具使用后插入点）**:
ToolResult 被 SessionEngine 接受后触发的通知时机；它当前不改写结果，也不改变后续循环。
_Avoid_: 工具执行器完成但尚未提交的结果

**HookRegistry（Hook 注册表）**:
由 composition root 构建并冻结的 Hook 实现集合，按生命周期插入点分别保存有序注册项；冻结后不可修改，空注册表也是合法配置。
_Avoid_: HookDispatcher、运行期可变回调表

**HookDispatcher（Hook 调度器）**:
消费冻结 HookRegistry，在各生命周期插入点构造对应的不可变上下文并调用异步 Hook 的执行边界。
_Avoid_: SessionEngine、万能 Hook 上下文
