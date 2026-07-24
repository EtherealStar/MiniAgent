# MiniAgent 持久化与可观测性设计

## 1. 文档目的

本文定义本地 Session 目录发现、Message Journal、独占写入、崩溃恢复和 AgentRun Trace。Message Journal 是恢复事实源；Trace 只用于诊断。

本文不引入数据库、OpenTelemetry、OTLP 或远程 telemetry 系统。

## 2. 设计目标与非目标

### 2.1 目标

- 已提交的用户消息、完整 AssistantMessage、ToolResult、ContextSummary 和运行终态可确定性恢复；
- 任何模型或工具动作前，其 UserMessage 已完成 fsync；
- 排队输入和 Assistant 流式草稿不会被误认为恢复事实；
- 历史 Session 按需发现，单个损坏目录不阻止列出其他 Session；
- 同一个 Session 同时只有一个进程持有写入权；
- Trace 可以定位 AgentRun、ModelCall 和 ToolCall，但其失败不改变业务结果。

### 2.2 非目标

- 不持久化内存 QueuedInput；
- 不支持多个进程同时写同一个 Session；
- 不提供 `session.json`、目录缓存或 Session 重命名；
- 不提供历史分页、Journal cursor 或增量恢复；
- 不保存 Assistant started、delta 或 discarded 到 Message Journal；
- 不持久化 Permission Request、Permission Decision、AgentRun 拒绝缓存或 Session Permission Grant；
- 不把 TodoStore 或 TodoReminder 作为恢复事实；
- 不在 Journal 写入失败后自动猜测并重试追加；
- 不以 Trace 作为恢复或业务审计事实源。

## 3. 权威性边界

```text
SessionRepository
  ├─ Message Journal (`message.jsonl`) -> 唯一恢复事实源
  ├─ writer lock                     -> 单进程独占写入
  └─ TraceSink (`trace/*.jsonl`)      -> 可丢失诊断记录

SessionEngine memory
  ├─ QueuedInput                     -> 未持久化
  ├─ Assistant draft                 -> 未持久化
  └─ Transcript                      -> 从 Journal 恢复
```

SessionUpdate 是当前进程通知，不写入上述任何恢复文件。UI Projection、Trace 和内存队列都不能补充或改写 Message Journal 的业务事实。

Pending Permission Request、当前 AgentRun 拒绝缓存和 Session Permission Grant 同样只属于 SessionEngine 内存。它们既不是恢复事实，也不是诊断记录；重新打开 Session 时始终为空。

## 4. SessionRepository

### 4.1 文件布局

```text
sessions/
  <session_id>/
    message.jsonl
    writer.lock
    trace/
      000001.jsonl
      000002.jsonl
      ...
    tool_result/
      <tool_use_id>/
        result.json
        metadata.json
    document_cache/
      <source_sha256>/
        content.md
        manifest.json
```

- `message.jsonl` 一个 Session 一个文件，不在线轮转；
- `writer.lock` 使用 OS 级独占锁，不能只检查文件是否存在；
- Trace 可以按大小轮转和单独清理；
- `tool_result` 只保存由 ArtifactStore 外置的最终结构化 ToolOutput，并由 Message Journal 中的 ArtifactRef 关联；
- `document_cache` 保存 MinerU 已完成的 Session 派生 Markdown；它是可重建缓存，不是 Message Journal 事实；
- 不创建 `session.json`；
- 没有首条已持久化用户消息，就没有可列出的 Session。

### 4.2 接口

Repository 对外提供：

```python
list_sessions() -> tuple[SessionSummary, ...]
create_session(session_id, first_user_record) -> OpenSession
open_session(session_ref) -> OpenSession
```

`OpenSession` 持有已验证 Journal 的读写能力和 writer lock。关闭 handle 必须释放锁。

`create_session` 原子完成目录准备、锁获取和首条 UserMessage 追加。失败时不得返回可用 handle；实现应尽力清理没有有效 Journal record 的空目录。

### 4.3 按需列表

应用启动不扫描历史目录。用户打开 Session picker 时，Textual App 才调用 `list_sessions()`。

Repository 从每个 Journal 派生：

- session_id；
- 由首条用户消息截断生成的不可编辑名称；
- 创建时间；
- 最后用户输入时间；
- 是否可打开及简短错误分类。

列表按最后用户输入时间倒序。一个 Journal 损坏时，Repository 继续扫描其他目录，并以目录 ID 作为损坏条目的回退名称；损坏条目可见但不可打开。

列表扫描不得获取 writer lock，也不修复或改写 Journal。只有用户选择 Session 时才调用 `open_session()` 完整验证并获取独占锁。

## 5. Message Journal

### 5.1 记录类型

`message.jsonl` 只允许：

- `user_message`
- `assistant_message`
- `tool_result`
- `context_summary`
- `run_terminated`

不写入：QueuedInput、AssistantStarted、AssistantDelta、AssistantDiscarded、ToolUseDetected、ModelEvent、UI 状态和 Trace 细节。

Reasoning 和 ToolUse 是完整 AssistantMessage 的 Part，随 `assistant_message` 一次保存。工具执行只能发生在包含该 ToolUse 的 AssistantMessage 成功提交之后。

用户拒绝越界目标后产生的 `permission_denied` 是该 ToolUse 的终态 ToolResult，因此按普通 `tool_result` 保存。记录只表达调用未执行，不保存 Permission Decision、授权范围、许可缓存或未经安全处理的路径。获准执行后的成功或失败结果也按既有工具结果协议保存；“曾经批准”本身不进入 Journal。

`tool_result` payload 必须保存 `tool_use_id`、来源 AssistantMessage identity、`tool_name`、模型可见 `content`、`is_error` 和 `outcome_unknown`。成功的小结果内联经过 output model 校验的结构化 `output`；成功的大结果保存 ArtifactRef 且不重复内联完整 output；预期失败保存结构化 ToolFailure。成功 output、成功 artifact 和 failure 按工具设计文档规定互斥。Journal 不保存 output schema 版本，也不在恢复时用当前工具定义重新解释历史结果。

成功 `read_docs` 的内联 output 可以携带 DocumentRef。Journal 保存该结构化历史结果，但 `document_cache` 内容仍是可丢失派生缓存：恢复时只重建 Current Session 的只读受控引用索引，并校验路径、字节数和哈希；缓存缺失或损坏不会使 Journal 损坏，只使 DocumentRef 在再次使用时失效。

成功 `todo_write` 的内联 output 可以保存当时列表，供历史 Tool Presentation 或诊断使用，但它不是 TodoStore 的恢复源。进程重启后不得扫描历史 ToolResult 回填 TodoList。

### 5.2 JSONL record

每行是一个完整 JSON 对象：

```json
{
  "schema_version": 1,
  "record_type": "assistant_message",
  "session_id": "uuid",
  "run_id": "uuid",
  "occurred_at": "2026-07-23T00:00:00Z",
  "payload": {
    "message": {}
  }
}
```

约束：

- 文件物理行顺序就是 Session 事实顺序；
- 不保存 `journal_sequence`；
- 不保存通用 `event_id`；
- user/assistant/tool message 使用 message_id，context_summary 使用 summary_id，run_terminated 使用 run_id 作为自然身份；
- 重复自然身份、未知 record_type、身份关联错误或不支持的 schema_version 都使恢复失败；
- 中间完整但结构非法的记录视为 Journal 损坏，不能静默跳过。

### 5.3 提交协议

SessionEngine 的每个明确提交方法按以下顺序执行：

1. 在内存中校验消息身份、role、run 和 ToolUse 关联；
2. 编码一个完整 JSONL record；
3. 追加、flush 并 fsync；
4. 成功后更新 Transcript；
5. 最后发布 SessionUpdate，并异步写 Trace。

Journal 追加失败时不更新 Transcript，不发布“已完成”更新，也不自动重试。SessionEngine 终止当前运行并关闭 handle；重新打开时由完整扫描判断该 record 是否实际落盘。

用户消息有两条路径：

- 首条消息由 `create_session(..., first_user_record)` 在 Session 创建过程中提交；
- 后续消息由 worker 从内存队列出队后提交。

两者都必须在 AgentRunEnvironment 组装、ModelCall 或 ToolUse 之前完成 fsync。

### 5.4 完整恢复

`open_session()` 从头扫描完整 Journal，不提供分页或 cursor：

- 文件尾部唯一一条不完整 JSON 行视为写入中断，可以截断到最后一个完整换行；
- 中间损坏、重复自然身份或非法关联使 Session 不可打开；
- 只有完整记录进入 Transcript；内存 Assistant 草稿不存在于恢复输入；
- ToolResult 必须引用此前已提交 AssistantMessage 中的 ToolUse；
- ToolResult 的 `tool_name` 必须与所引用 ToolUse 一致，成功 output/artifact 与 failure 的互斥关系必须成立；
- 恢复保留已持久化的结构化 output、failure 和 ArtifactRef；Provider 投影仍只使用 content；
- ContextSummary 按物理创建顺序恢复，并且覆盖边界必须引用此前存在的消息；
- run_terminated 恢复停止原因，但 Trace 不能充当终态事实；
- 发现已持久化 UserMessage 对应的 Run 没有终态时，不重放模型或工具，追加 PROCESS_INTERRUPTED；
- 恢复后的输入队列始终为空。
- 恢复后不存在 pending Permission Request、AgentRun 拒绝缓存或 Session Permission Grant，也不因历史 ToolUse 重新询问用户。
- 恢复后的 TodoStore 为空；同一进程内的 Session 切换不走进程恢复，因此仍可读取既有内存 TodoList。
- 恢复时可以从已提交 ArtifactRef 和 DocumentRef 重建 exact-read 受控引用目录，但不能恢复外部上传 permission 或未完成 MinerU batch。

## 6. Trace 模型

### 6.1 Span 层级

```text
agent.run
  └─ agent.turn
       ├─ model.call
       │    └─ stream_summary event
       └─ tool.call
            ├─ attempt_started event
            ├─ retry_scheduled event
            └─ tool_finished event
```

最小 Span 类型为 `agent.run`、`agent.turn`、`model.call` 和 `tool.call`。每个 Span 有开始时间、结束时间、状态和父子关系。delta 不创建独立 Span。

### 6.2 Trace Envelope

```json
{
  "trace_schema_version": 1,
  "trace_record_id": "uuid",
  "trace_sequence": 187,
  "occurred_at": "2026-07-23T00:00:00Z",
  "event_type": "tool_finished",
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "uuid",
  "session_id": "uuid",
  "run_id": "uuid",
  "message_id": "uuid",
  "payload": {}
}
```

`trace_sequence` 只表示 Trace writer 的追加顺序，不是 Journal sequence。Trace 可以通过 session_id、run_id、message_id 和 tool_use_id 反查业务对象；不得依赖已删除的通用 event_id。

### 6.3 Span 内容

`agent.run` 记录输入 message_id、turn count、stop reason、final message ID、耗时和安全错误分类。

`agent.turn` 记录 turn 序号、Assistant message ID、continuation/retry 关联、上下文规模、压缩标记、工具数量和模型终态。

`model.call` 记录 provider、model、request ID、输入规模、生成选项、finish reason、usage、重试关系和安全错误。默认不记录完整 prompt、reasoning 或工具定义原文。

`tool.call` 记录 tool name、tool_use_id、Assistant message ID、批次位置、attempt、耗时、outcome_unknown、is_error 和结果大小。默认不记录参数原文或完整结果。

Trace 不记录 Permission Request、Permission Decision、授权缓存或外部目标明文。最终 `permission_denied` 可以像其他 ToolFailure 一样只记录安全 code、stage、attempts=0 和耗时元数据。

### 6.4 Stream Summary

模型流默认只记录 text/reasoning/tool delta 的数量和字节数、首尾时间、间隔统计、是否收到终态、是否取消和总耗时。只有显式启用 content trace 时，才能以受大小限制和脱敏的批次保存原文。

### 6.5 错误对象

Trace 错误记录包括 category、type、retryable、provider_code、status_code、request_id、安全 message 和 cancelled。message 必须限长并脱敏；Python stack trace 只进入独立 debug sink。

## 7. Trace 写入语义

- TraceSink 是核心流程外的异步适配边界；
- writer 可以使用有界队列、批量写入和文件轮转；
- 队列满或 sink 失败时允许丢弃 Trace；
- Trace 失败不回滚 Journal，也不改变 AgentRunResult；
- 退出时可以短暂 drain，超时后允许丢弃尾部；
- 默认 metadata-only，内容记录必须显式开启并受脱敏、大小和保留期限制。

## 8. 不变量与验证场景

1. UserMessage fsync 成功前不发起模型或工具动作。
2. QueuedInput、Assistant 草稿和 SessionUpdate 都不进入 Message Journal。
3. Journal 写入失败时 Transcript 不变，且不自动重试追加。
4. Journal 物理顺序可以唯一恢复 Transcript。
5. 尾部不完整行可截断，中间损坏或重复自然身份会停止恢复。
6. 一个 Session 同时只有一个 writer lock 持有者。
7. `list_sessions()` 隔离损坏目录且不改写文件。
8. 不完整 Run 恢复为 PROCESS_INTERRUPTED，不重放模型或工具。
9. Trace sink 失败、变慢或队列满不改变业务结果。
10. 默认 Trace 不包含 prompt、reasoning、工具参数或完整结果原文。
11. Permission 内存状态不写入 Journal、Trace 或配置；只有安全的终态 `permission_denied` ToolResult 可以恢复。

## 9. 当前实现差距

- 当前 JsonlTranscriptStore 写入通用 SessionEvent，尚未实现本文的窄 record schema；
- 当前存储没有读取恢复、尾行处理、完整验证、fsync 或 writer lock；
- 当前代码仍持久化 event_id、sequence、Assistant started/delta/discard；
- SessionRepository 的目录扫描、SessionSummary、损坏隔离和完整 open 尚未实现；
- TraceSink 与本文的 Span 模型尚未实现。
- 当前 ToolResultPart 尚未持久化 tool_name、结构化 output、failure 和 ArtifactRef，恢复也未校验这些互斥关系。
