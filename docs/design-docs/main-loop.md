# 主循环模块设计

## 1. 背景与目标

MiniAgent 的主循环分为两层：外层 `SessionEngine` 管理 Current Session，内层 `AgentLoop` 执行一条已持久化用户消息对应的 AgentRun。

本文目标是：

- Session 历史只有一个提交边界；
- 用户可以在运行期间继续排队输入，但排队项不成为恢复事实；
- 同一 Session 的 AgentRun 严格串行；
- 流式草稿可以展示，但只有完整消息进入 Journal 和后续上下文；
- 模型、工具、取消、重试和停止都产生明确结果。

Textual 的生命周期编排、SessionRepository 的文件格式和具体 Provider 协议由各自文档定义。

## 2. 核心生命周期

```text
Current Session
  ├── QueuedInput*                 仅内存 FIFO
  └── SessionEngine worker        最多一个
        └── AgentRun               严格串行
              ├── ModelCall
              ├── ToolExecutionBatch
              ├── ModelCall
              └── AgentRunResult
```

### 2.1 QueuedInput

`SessionEngine.submit(text)` 校验输入并立即分配 `message_id`、`run_id`，随后创建 QueuedInput：

```python
QueuedInput
  message_id: UUID
  run_id: UUID
  text: str
```

QueuedInput 只存在内存，可以按 message_id 撤回，但不能编辑或重排。它不属于 Transcript，切换、清空、关闭或崩溃时允许丢失。

### 2.2 AgentRun

worker 从队首取出输入后，先用已有身份将 UserMessage 写入并 fsync Message Journal。成功后才收集 AgentRunEnvironment 并启动 AgentLoop。

AgentRun 绑定一条不可变用户消息，可包含多次 ModelCall 和工具调用。运行期间到达的新输入只进入队列，不进入当前 Working Context。

### 2.3 ModelCall 与 turn_count

ModelCall 是一次实际发送给模型的请求及响应流。每次调用使用新的 AssistantMessage identity；失败或作废的调用不会产生有效 AssistantMessage。

`turn_count` 只统计实际发出的模型请求。工具执行、Hook、上下文压缩和请求前配置错误不增加计数。

## 3. SessionEngine

SessionEngine 是单个 Session 的 Transcript 所有者和唯一提交边界。它负责：

- `start(first_text)`：创建首个用户身份、建立持久化 Session 并启动第一次运行；
- `submit(text)`：分配身份并加入内存 FIFO；
- `withdraw(message_id)`：撤回尚未出队的 QueuedInput；
- `serve()`：串行出队、提交用户消息并运行 AgentLoop；
- `cancel_active()`：协作取消当前 AgentRun；
- `stop(reason)`：停止接收输入、取消活动 Run、丢弃队列并关闭 worker；
- `snapshot()`：返回当前完整消息投影和必要的运行展示状态；
- 发布单一 SessionUpdate 流。

首条输入使用 `start(first_text)`，因为应用此时没有 Current Session。它在返回成功前完成目录创建、writer lock 和首条 UserMessage 持久化；失败时不得留下可用 Session 或启动模型调用。

后续 `submit()` 不写 Journal。worker 出队持久化失败时，不启动 AgentLoop，并发布可读失败更新；Session 必须停止后重新打开验证持久化状态，不能自动重复追加。

### 3.1 唯一 worker

一个 SessionEngine 最多有一个 `serve()` task和一个活动 AgentRun。Textual App 保证全进程最多启动一个 SessionEngine worker；Engine 自身仍校验重复启动和并行 Run 是程序错误。

### 3.2 提交接口

AgentLoop 不发送通用事件信封，而通过窄接口报告恢复事实：

```python
await session.commit_assistant(message, finish_reason)
await session.commit_tool_result(message)
await session.commit_context_summary(summary)
await session.finish_run(result)
session.publish_live(update)
```

每个 `commit_*` 方法必须：

1. 校验自然身份和关联不变量；
2. 追加一条完整 Journal record 并完成落盘确认；
3. 更新 Transcript / Working Context；
4. 发布相应 SessionUpdate。

Journal 写入失败时，步骤 3 和 4 不得执行。

## 4. AgentLoop

AgentLoop 只负责一次 AgentRun：

- 接收固定 AgentRunEnvironment、只读历史和当前已提交 UserMessage；
- 每次 ModelCall 前，由工具注册表视图结合当前 AgentRun 已提交的 ToolUse/ToolResult 重新投影最新 ToolView；
- 请求 ContextManager 构建 Model Context；
- 将同一个 ToolView 快照交给 ContextManager 和 ModelAdapter；
- 消费规范化 ModelEvent 并组装内存 Assistant 草稿；
- 完整响应到达后一次提交 AssistantMessage；
- 执行已提交 AssistantMessage 中的 ToolUse；
- 提交 ToolResult 并准备下一次 ModelCall；
- 返回结构化 AgentRunResult。

AgentLoop 不提交 UserMessage，不读取 SessionEngine 的可变队列，不直接操作 Repository 或 UI。

## 5. 消息和工具关联

每条 Message 使用全局唯一 `message_id`，并由保持原始顺序的 Parts 构成。AssistantMessage 可以包含 TextPart、ReasoningPart 和 ToolUsePart。

每次 ModelCall 创建新的 Assistant `message_id`：

- 输出达到长度上限后的续写使用 `continuation_of_message_id`；
- 草稿作废后的重试使用 `retry_of_message_id`；
- UI 可以合并展示，但 Transcript 保留调用边界。

每个 ToolUse 使用全局唯一 `tool_use_id`。ToolResult 同时引用 tool_use_id 和来源 AssistantMessage identity，关联不得依赖数组位置。并发完成的工具结果在进入下一轮 Model Context 和 Journal 时按原 ToolUse 顺序提交。

## 6. SessionUpdate 与草稿

SessionEngine 对 UI 只暴露一条 SessionUpdate 流。最小更新类型包括：

```text
InputQueued
InputWithdrawn
InputCommitted
AssistantStarted
AssistantDelta
AssistantDiscarded
AssistantCompleted
ToolResultCompleted
RunTerminated
QueueDiscarded
SessionStopped
```

`InputQueued`、Assistant started/delta/discard 和 QueueDiscarded 只是当前进程状态。它们可以丢失，也不进入 Message Journal。

模型响应完整后才提交 AssistantMessage。未完成草稿在取消、流中断或进程崩溃时直接退出有效 UI Projection，不需要 Journal 中的 started/discarded 配对记录。

Textual 可以在需要时用 `snapshot()` 重建当前完整聊天，但 Runtime 不维护 revision、cursor、事件补放或自动缺口恢复协议。

## 7. Working Context 与 ContextManager

```text
Message Journal
      ↓ 恢复
Transcript
      ↓ AgentRun 启动时快照
Working Context
      ↓ ContextManager + ToolView
Model Context
```

由于后续 QueuedInput 不写 Journal，Transcript 不会出现两个 AgentRun 的事实交错。Working Context 直接按 Transcript 顺序选择消息，无需 Run Segment 重组。

ContextManager 可以裁剪 ToolResult、注入 memory 或用 ContextSummary 替代旧消息，但不能修改 Transcript。新摘要只有经 SessionEngine 提交为 ContextSummary Journal Record 后，才能进入后续 Working Context；覆盖边界由上下文设计文档定义。

ToolView 不由 ContextManager 计算。AgentLoop 在每次 ModelCall 准备阶段，把冻结 Registry view 和当前 AgentRun 的已接受消息交给工具模块进行纯投影。投影只使用已经由 SessionEngine 提交的 ToolResultPart；尚未提交、提交失败或仅存在于 Trace 的结果不能改变工具可用性。

## 8. 主循环控制流程

```text
worker 取 QueuedInput
        │
        ▼
持久化 UserMessage 并 fsync
        │ 失败 → 停止 Session，不调用模型
        ▼
冻结 AgentRunEnvironment
        │
        ▼
检查取消与 max_turns
        │
        ▼
取得 ToolView，构建 Model Context
        │
        ▼
发起 ModelCall，turn_count += 1
        │
        ▼
在内存组装并流式展示 Assistant 草稿
        │
   ┌────┴──────────────┐
   ▼                   ▼
失败/中断             完整响应
   │                   │
作废草稿              提交完整 AssistantMessage
重试或终止             │
                       ▼
                 是否含 ToolUse
                   │       │
                  否       是
                   │       │
                 完成      执行并提交 ToolResult
                           │
                           └── 下一次 ModelCall
```

### 8.1 工具批次

只有完整 AssistantMessage 成功提交后，其中的 ToolUse 才能执行。若模型产生多个 ToolUse，它们形成一个 ToolExecutionBatch；AgentLoop 等待全部终态结果，再按模型调用顺序提交并继续。

工具失败通常是模型可见 ToolResult，不是 AgentRun 致命错误。最后一个允许的 ModelCall 即使产生工具调用，也要完成并记录工具结果，然后以 `MAX_TURNS` 停止，不再调用模型总结。

批次全部结果按原始 ToolUse 顺序提交后，下一次 ModelCall 才刷新 ToolView。同一工具在当前 AgentRun 中连续产生三次可计数的最终失败后，从下一次 ToolView 完全移除；成功结果清零该工具的连续失败。第一次或第二次失败只在紧接的一次 ModelCall 中产生不含失败次数的 Tool Recovery。该状态从当前 AgentRun 的已提交消息投影，不写入 AgentRunEnvironment，也不修改冻结 Registry。

### 8.2 流中断与重试

模型流未完成时，整条 Assistant 草稿作废。草稿不进入 Transcript，新请求使用新的 message_id 并消耗新的 turn。已接收的 delta 可以进入 Trace，但不写 Message Journal。

服务端返回 prompt too long 时最多强制压缩并重试一次；若压缩无效或再次过长，则以 `PROMPT_TOO_LONG` 终止。

### 8.3 输出续写

模型以 `finish_reason=length` 正常结束时，先提交当前完整 AssistantMessage，再用新的 message_id 续写。续写消耗新的 turn；达到上限时保留已有内容并返回 `MAX_TURNS`。

## 9. 取消、切换与恢复

### 9.1 取消

取消信号停止新的 ModelCall，并请求取消当前模型流和工具执行。已经发生的工具副作用不能撤销；无法取消的工具完成后，其真实结果仍应尽力提交。

普通取消只影响活动 AgentRun，不自动丢弃后续队列。`stop(reason)` 用于 Session 切换、清空和应用关闭，它会同时丢弃所有未持久化 QueuedInput。

### 9.2 切换和关闭

Textual App 调用：

```python
await session.stop(reason=SESSION_SWITCHED | APPLICATION_SHUTDOWN)
```

Engine 停止接收输入、发布 QueueDiscarded、取消活动 Run、提交明确终态、停止 worker 并关闭 Repository handle。硬超时导致未能提交终态时，交给下次恢复补记 PROCESS_INTERRUPTED。

### 9.3 进程恢复

恢复只重放 Message Journal。若最后一个已持久化 UserMessage 没有 RunTerminated：

- 不自动请求模型；
- 不自动重放 ToolUse；
- 不恢复任何 QueuedInput；
- 追加 PROCESS_INTERRUPTED；
- 允许用户提交新的输入。

## 10. AgentLoop 接口

建议接口为异步协程和结构化返回值：

```python
result = await agent_loop.run(
    environment=agent_run_environment,
    working=working_context,
    event_sink=session_commit_port,
    cancellation=cancellation,
)
```

`SessionCommitPort` 只有完整消息、工具结果、运行终态和 live update 的明确方法，不暴露 Repository。

AgentRunResult 至少包含：

```python
AgentRunResult
  reason: StopReason
  turn_count: int
  final_message_id: UUID | None
  error: ErrorInfo | None
```

停止原因至少包括 COMPLETED、MAX_TURNS、PROMPT_TOO_LONG、CANCELLED、SESSION_SWITCHED、APPLICATION_SHUTDOWN、PROCESS_INTERRUPTED、MODEL_UNAVAILABLE 和 EVENT_COMMIT_FAILED。

## 11. 错误边界

- 预期 Provider、上下文、取消和限制错误转换为 AgentRunResult；
- 工具预期失败转换为 ToolResult；
- Journal 提交失败转换为 EVENT_COMMIT_FAILED，并使 Session 进入必须停止和重新验证的状态；
- 未知 ModelEvent、ID 冲突、不可能的消息顺序和重复 worker 是程序错误，直接抛出；
- SessionUpdate 和 Trace 写入失败不改变 AgentRun 控制流。

## 12. 核心不变量

1. SessionEngine 是 Transcript 的唯一提交边界。
2. QueuedInput 入队时获得 message_id 和 run_id，但不进入 Journal。
3. UserMessage 完成持久化之前不得开始其 AgentRun。
4. 同一 Session 同时最多一个 AgentRun。
5. AgentRun 不消费启动后到达的新输入。
6. 每个 ModelCall 使用新的 Assistant message_id。
7. 未完成或作废草稿不进入 Transcript 或 Model Context。
8. ToolUse 所属 AssistantMessage 必须先提交，工具才能执行。
9. turn_count 只随实际发出的 ModelCall 增加。
10. AgentRunEnvironment 在整个 Run 内不变；每个 ModelCall 重新投影最新 ToolView，ContextManager 与 ModelAdapter 使用同一个快照。
11. stop 会丢弃所有未持久化 QueuedInput。
12. 恢复不自动重放模型或工具动作。
13. 只有已经提交的 ToolResult 才能影响后续 Tool Recovery 和工具可用性。

## 13. 建议测试场景

- 首条消息创建失败时不调用模型；
- 连续 submit 分配稳定身份并按 FIFO 执行；
- 排队项可撤回、不可重排且不写 Journal；
- 出队持久化失败时当前和后续 Run 都不启动；
- 流式草稿可见但恢复后不存在；
- 一个 AssistantMessage 含多个 ToolUse，结果乱序完成但顺序提交；
- 同一批次的同名工具结果按原始 ToolUse 顺序影响下一轮 ToolView，不按完成顺序；
- 第一次或第二次最终失败只影响紧接的一轮 Recovery，第三次连续失败从下一轮移除工具，成功清零；
- ContextManager 与 ModelAdapter 接收同一个刷新后的 ToolView，schema 与 Prompt 不漂移；
- 流中断后使用新 message_id 重试；
- length 终态产生 continuation；
- 取消活动 Run 不删除普通队列，stop 则丢弃全部队列；
- 进程中断后只恢复已提交消息并补记 PROCESS_INTERRUPTED；
- 连续 ModelCall 固定 AgentRunEnvironment，但使用新的 ToolView。
