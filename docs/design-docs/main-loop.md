# 主循环模块设计

## 1. 背景与目标

MiniAgent 的主循环分为两层：外层 `SessionEngine` 管理整个会话，内层 `AgentLoop` 执行一次完整的 `AgentRun`。

本文档定义两层之间的职责边界、消息与事件模型、模型/工具循环、上下文处理、流式输出、重试、中断及停止语义。目标是让主循环具备以下性质：

- 会话历史只有一个写入边界；
- 模型看到的上下文可压缩，但不会破坏原始历史；
- 流式输出、工具调用和 UI 展示可以被确定性重放；
- 重试、取消和达到限制都有明确且可观察的结果；
- 模型供应商协议和工具执行策略不进入核心循环。

## 2. 范围

### 2.1 本文覆盖

- `SessionEngine` 与 `AgentLoop` 的职责和交互；
- `AgentRun`、`ModelCall` 与 `turn_count` 的生命周期；
- 消息、Part、工具关联 ID 和 Session 事件；
- 模型上下文构建与摘要检查点；
- 模型流解析后的统一事件处理；
- 工具任务提交、等待和结果回填；
- 流式中断、输入过长、输出截断、取消和进程恢复语义；
- `AgentRunResult` 的错误边界。

### 2.2 本文不覆盖

- slash command 的语法与执行；
- Hook 的接口和执行协议；
- 工具执行内部采用串行还是并发；
- 工具参数校验和具体异常分类；
- artifact、附件及 transcript 的具体存储实现；
- UI 组件和渲染细节；
- 配置版本化或 `AgentRunConfig` 快照。

## 3. 核心概念与生命周期

```text
Session
  └── AgentRun                  一条用户消息触发的一次完整执行
        ├── ModelCall           一次模型请求与响应流
        ├── ToolExecutionBatch  同一 AssistantMessage 的工具调用集合
        ├── ModelCall
        └── ...
```

### 3.1 Session

`Session` 覆盖用户发起的整个会话，可以包含多条用户消息和多次 `AgentRun`。

### 3.2 AgentRun

`AgentRun` 是 `AgentLoop` 的生命周期边界。它绑定一条不可变的用户输入，可包含多次模型调用和工具调用，最终返回一个结构化 `AgentRunResult`。

`AgentRun` 运行期间不消费新的用户输入。新输入由 `SessionEngine` 排队；用户也可以显式取消当前运行后再开始下一次运行。

### 3.3 ModelCall 与 Turn

`ModelCall` 是一次实际发送给模型的请求及其响应流。一次 `ModelCall` 为一条新的 `AssistantMessage` 预留身份；成功完成时至多提交一条有效消息，失败或作废时不会产生有效的已完成消息。

`turn_count` 只统计 `ModelCall`：

- 模型请求实际发出时，`turn_count += 1`；
- 工具执行、Hook、上下文处理不增加计数；
- 重新发送完整模型请求是新的 ModelCall，需要再次计数；
- 同一请求内部由客户端完成、且未重新发送完整请求的连接恢复不重复计数。

## 4. 模块职责

### 4.1 SessionEngine

`SessionEngine` 是 Session 历史的唯一所有者和写入边界：

- 提供启动 `AgentRun` 所需的只读初始消息；
- 接收 `AgentLoop` 发出的结构化事件；
- 为事件分配 Session 内严格递增的 `sequence`；
- 更新会话消息投影，并预留 transcript 持久化入口；
- 将领域事件转换为 UI 可理解的事件；
- 管理输入队列和取消信号。

`SessionEngine` 接受事件和向 UI 投递事件是两个不同保证：前者是强保证，后者允许失败并在 UI 重连后按 `sequence` 重放。UI 断开不会自动终止 `AgentRun`。

### 4.2 AgentLoop

`AgentLoop` 只负责一次 `AgentRun`：

- 使用启动时传入的只读消息和本次运行的本地工作上下文；
- 在每次模型调用前构建 Model Context；
- 调用模型并消费规范化流事件；
- 组装、提交并追加 AssistantMessage；
- 在 AssistantMessage 完成后提交其中的工具调用；
- 等待所有工具调用产生终态结果；
- 根据工具调用、模型完成原因、取消和限制决定是否继续；
- 返回结构化 `AgentRunResult`。

`AgentLoop` 不允许在运行中反向读取 `SessionEngine` 的可变历史，也不直接操作 UI 或 transcript 存储。

### 4.3 ModelAdapter

`ModelAdapter` 隔离供应商协议差异，并将原始响应流规范化为内部事件：

```text
TextDelta
ReasoningDelta
ToolUseDelta
ResponseCompleted
ResponseFailed
```

`ModelAdapter` 只负责供应商协议外壳。供应商提供的结构化 `reasoning_content` 转换为 `ReasoningDelta`；工具调用按原始增量转换为 `ToolUseDelta`。`AgentLoop` 负责组装工具调用和 Assistant 草稿，适配器不判断内容是否完整。

特定模型写入普通文本的 `<think>...</think>` 仍作为 `TextDelta` 原样输出。若需要将其拆分为 reasoning，必须由适配器之后的独立文本处理层完成，不属于供应商适配或本文定义的主循环职责。OpenAI-compatible 供应商的具体请求、配置和错误协议见 `docs/design-docs/openai-compatible-model-provider.md`。

### 4.4 ContextBuilder

`ContextBuilder` 从 Working Context 生成一次 ModelCall 的 Model Context。裁剪工具结果、压缩历史和注入 memory 都只影响该投影，不反向修改 transcript。

### 4.5 工具执行接口

工具执行由外部类或函数负责。`AgentLoop` 只提交一个 AssistantMessage 中已完成的 `ToolUsePart`，并等待每个 `tool_use_id` 对应的终态 `ToolResult`。外部执行层自行决定串行或并发。

## 5. 消息模型

### 5.1 Message 与 Part

每条 Message 使用全局唯一 `message_id`，内容由保持原始顺序的 `parts` 组成：

```python
Message
  message_id: UUID
  role: user | assistant | tool | system
  parts: list[Part]
```

AssistantMessage 可包含：

- `TextPart`
- `ReasoningPart`
- `ToolUsePart`

`ReasoningPart` 至少记录内容来源与可见性策略：

```python
ReasoningPart
  part_id: UUID
  content: str
  source: structured | think_tag
  visibility: collapsed | hidden | visible
```

UI 默认可折叠展示 reasoning。若供应商只提供 reasoning summary，不得将其描述为原始思考过程。

### 5.2 工具调用关联

每个工具调用使用全局唯一的 `tool_use_id`。工具结果显式记录：

- 自身消息或 Part 的 ID；
- `tool_use_id`；
- 来源 `assistant_message_id`；
- 终态结果。

关联不得依赖数组位置。一个 AssistantMessage 中的多个工具结果可以任意顺序完成，但加入下一轮 Model Context 时按原始 `ToolUsePart` 顺序排列，以获得确定性上下文。

### 5.3 一次调用一条 AssistantMessage

每次 ModelCall 创建新的 `message_id`，不得复用：

- 输出达到长度上限后的续写使用新消息，并通过 `continuation_of_message_id` 关联；
- 草稿作废后的重试使用新消息，可通过 `retry_of_message_id` 关联已作废草稿；
- UI 可以合并展示连续消息，但 transcript 保留调用边界。

## 6. 事件模型与提交顺序

Session 事件至少具有以下信封：

```python
SessionEvent
  event_id: UUID
  session_id: UUID
  run_id: UUID
  sequence: int
  occurred_at: datetime
  payload: EventPayload
```

`AgentLoop` 发出的是尚未分配 `sequence` 的事件载荷；`SessionEngine` 接受事件时补充完整信封并返回确认。

- `event_id` 用于幂等去重；
- `sequence` 由 `SessionEngine` 分配，并作为持久化恢复与 UI 重放的权威顺序；
- `message_id`、`part_id` 和 `tool_use_id` 用于领域对象关联。

典型流式事件包括：

```text
AssistantMessageStarted
AssistantPartDelta
ToolUseDetected
AssistantMessageCompleted
AssistantMessageDiscarded
ToolResultRecorded
```

事件通过确认式异步接口提交：

```python
await event_sink.emit(event)
```

`emit()` 返回表示 `SessionEngine` 已校验并接受事件。主循环遵循“先提交，后可见”：

1. 在草稿缓冲区组装模型响应；
2. 提交消息完成事件；
3. `emit()` 成功后，将完整 AssistantMessage 加入 Working Context；
4. 再提交工具调用；
5. ToolResult 同样先提交，再加入 Working Context。

因此，下一次 ModelCall 使用的每条消息都已经被 Session 接受。关键事件提交失败时，`AgentRun` 以结构化失败结果结束。

## 7. 上下文构建与压缩

```text
Transcript（完整、追加写）
        ↓
Working Context（本次运行已提交消息）
        ↓
ContextBuilder（选择、裁剪、压缩、注入）
        ↓
Model Context（本次模型请求）
```

### 7.1 不变量

- transcript 不因上下文裁剪或压缩而删除或改写；
- 工具结果裁剪只影响 Model Context；
- 原始工具结果留在 transcript 或由 transcript 引用的外部存储中；
- ContextSummary 是不可变检查点，新摘要替代旧摘要的使用，不修改旧摘要。

### 7.2 ContextSummary

摘要至少记录：

```python
ContextSummary
  summary_id: UUID
  covers_through_message_id: UUID
  resume_from_message_id: UUID | None
  summary: str
```

Model Context 的拼接顺序为：

```text
system prompt
+ ContextSummary
+ 从 resume_from_message_id 开始的原始消息
+ 当前运行的动态内容
```

若创建摘要时覆盖范围之后还没有消息，`resume_from_message_id` 可以为 `None`；后续通过 `covers_through_message_id` 的后继消息确定恢复起点，无需修改摘要。

### 7.3 Prompt Too Long

每次模型调用前，`ContextBuilder` 应按已知上限主动控制输入大小。如果服务端仍返回 `prompt_too_long`：

1. 允许一次强制压缩；
2. 使用压缩后的上下文重新发起 ModelCall；
3. 若压缩未减少 token，或重试后仍然过长，则停止；
4. 返回 `reason=PROMPT_TOO_LONG`。

每个实际发出的请求都消耗一个 turn，包括被服务端以输入过长拒绝的请求。

## 8. 主循环控制流程

```text
初始化 Working Context 和 turn_count
                │
                ▼
       检查取消与 max_turns
                │
                ▼
        ContextBuilder 构建上下文
                │
                ▼
    发起 ModelCall，turn_count += 1
                │
                ▼
       消费并提交规范化流事件
                │
       ┌────────┼───────────┐
       │        │           │
       ▼        ▼           ▼
    流中断   输出达上限    正常完成
       │        │           │
 discard草稿   提交消息     提交消息
 新ID重新生成  新消息续写    │
                            ▼
                     是否包含 ToolUse
                       │          │
                      否          是
                       │          │
                 返回 COMPLETED   提交工具批次
                                  │
                                  ▼
                         等待全部终态结果
                                  │
                                  ▼
                         提交结果并继续循环
```

### 8.1 正常停止

模型响应正常完成且不包含工具调用时，`AgentLoop` 返回 `COMPLETED`。

### 8.2 工具批次

AssistantMessage 完成并被 Session 接受后，其中的工具调用进入执行队列。同一消息中的调用形成一个逻辑批次：

```python
ToolExecutionBatch
  run_id: UUID
  assistant_message_id: UUID
  tool_use_ids: list[UUID]
```

`AgentLoop` 等待批次中每个 `tool_use_id` 都产生终态结果，再准备下一次 ModelCall。工具执行方式不属于主循环设计。

工具失败通常是模型可见的数据，而不是 AgentRun 的致命错误。外部工具层返回的失败结果仍会被提交并加入下一轮上下文。

### 8.3 达到 max_turns

准备发起新 ModelCall 前，如果 `turn_count >= max_turns`，停止并返回 `MAX_TURNS`。

如果最后一次允许的 ModelCall 已经产生完整工具调用，这些工具仍然执行并记录结果；完成后不再调用模型，直接返回 `MAX_TURNS`。因此最后一次运行可能没有模型对工具结果的总结。

## 9. 流式中断、重试与输出续写

### 9.1 流式草稿

流式 delta 会持久化为追加事件，同时投影为 Draft AssistantMessage。草稿不是有效的已完成消息。

### 9.2 流中断后的重试

如果模型流在 AssistantMessage 完成前中断：

1. 整条草稿作废，包括已经收到的文本、reasoning 和未闭合工具调用；
2. 追加 `AssistantMessageDiscarded` 事件，不物理删除 delta；
3. UI 移除或标记该草稿；
4. `ContextBuilder` 排除该消息；
5. 从中断前最后一个完整上下文重新发起请求；
6. 新请求使用新的 `message_id`，且消耗新的 turn。

底层 delta 仅用于 trace 和恢复，不进入有效消息历史。

### 9.3 输出达到长度上限

模型以 `finish_reason=length` 正常结束时：

- 保留并提交当前 AssistantMessage；
- 创建新的 AssistantMessage 发起续写；
- 使用 `continuation_of_message_id` 关联上一条消息；
- 新的续写请求消耗一个 turn；
- 若已达到 `max_turns`，保留已有内容并返回 `MAX_TURNS`。

这与网络中断不同：前者保留完整响应帧并续写，后者作废草稿并重新生成。

## 10. 取消与恢复

### 10.1 协作式取消

`SessionEngine` 通过独立取消信号通知 `AgentLoop`：

- 正在进行的模型流请求取消，未完成草稿按 discard 规则处理；
- 尚未提交的工具调用不再提交；
- 已开始的工具由外部执行函数决定是否支持取消；
- 不支持取消的工具可以执行完毕，其真实结果和副作用仍记录；
- 取消后不再触发新的 ModelCall；
- `AgentLoop` 返回 `reason=CANCELLED`。

取消 AgentRun 不承诺撤销已经发生的工具副作用。

### 10.2 进程恢复

进程重启后只恢复 Session 状态，不自动续跑未完成的 AgentRun：

- 重放 Session 事件；
- 作废未完成 Assistant 草稿；
- 没有终态结果的已提交工具调用视为结果未知；
- 不自动重新执行工具，避免重复副作用；
- 未完成运行以 `PROCESS_INTERRUPTED` 结束；
- 后续继续操作创建新的 AgentRun。

## 11. AgentLoop 接口

建议使用“异步协程 + event sink + 结构化返回值”，而不是依赖异步生成器返回终态：

```python
result = await agent_loop.run(
    initial_messages=tuple(messages),
    user_message=user_message,
    system_prompt=system_prompt,
    max_turns=max_turns,
    event_sink=session_engine,
    cancellation=cancellation,
)
```

`initial_messages` 是只读快照。`AgentLoop` 不在运行中查询 Session 历史。

返回值的最小形态：

```python
@dataclass(frozen=True)
class AgentRunResult:
    reason: StopReason
    turn_count: int
    final_message_id: UUID | None
    error: ErrorInfo | None = None
```

建议的停止原因至少包括：

```text
COMPLETED
MAX_TURNS
PROMPT_TOO_LONG
CANCELLED
PROCESS_INTERRUPTED
MODEL_UNAVAILABLE
EVENT_COMMIT_FAILED
```

## 12. 错误边界

`AgentLoop.run()` 将可预期的运行时失败转换为 `AgentRunResult`，例如输入过长、达到最大轮数、取消、模型不可用或事件提交失败。

程序错误和内部不变量破坏不应被包装成普通停止原因，例如：

- 未知的规范化模型事件；
- ID 关联冲突；
- 不可能出现的消息顺序；
- 断言失败和代码缺陷。

这些错误应直接抛出，以便测试和监控能够发现实现问题。

## 13. 核心不变量

实现和测试必须保护以下不变量：

1. Session 历史只能由 `SessionEngine` 接受和提交。
2. Session 事件的 `sequence` 严格递增且可确定性重放。
3. 每个 ModelCall 使用新的 Assistant `message_id`。
4. 每个工具调用具有全局唯一 `tool_use_id`，每个终态结果显式引用它。
5. 未被 Session 接受的消息不得进入下一轮 Model Context。
6. Discard 的草稿不得进入有效消息历史或 Model Context。
7. ContextBuilder 不修改 transcript。
8. AgentRun 不消费启动后到达的新用户输入。
9. `turn_count` 只随实际发出的 ModelCall 增加。
10. 达到 `max_turns` 后不得再发起 ModelCall。
11. UI 投递失败不得改变 AgentRun 的控制流。
12. 进程恢复不得自动重放可能产生副作用的工具调用。

## 14. 建议测试场景

- 无工具调用的单轮正常完成；
- 一个 AssistantMessage 包含多个工具调用，结果乱序完成但上下文按调用顺序拼接；
- reasoning、文本和工具调用交错流式到达；
- 结构化 `reasoning_content` 与普通文本增量保持来源边界；
- 模型流中断后草稿被 discard，新请求使用新消息 ID；
- 输出达到长度上限后创建 continuation 消息；
- 服务端报告 prompt too long，强制压缩一次后成功或失败；
- 最后一个 turn 返回工具调用，工具执行后以 `MAX_TURNS` 停止；
- 工具返回失败结果后模型继续处理；
- UI 断线但 AgentRun 继续，重连后按 sequence 补发；
- 用户在模型流期间取消；
- 用户在不可取消工具执行期间取消；
- 进程在工具已提交但结果未知时崩溃，恢复后不重复执行；
- event sink 拒绝关键事件后运行以结构化失败结束；
- 新用户输入在当前 AgentRun 期间到达，只进入 Session 队列。
