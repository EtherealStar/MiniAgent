# 工具注册表与执行器设计

## 1. 目标与边界

本文定义 MiniAgent 的工具注册、OpenAI-compatible function schema 生成、参数校验、目标解析、执行重试、并发调度和工具结果持久化协议。

本文不负责 Prompt 的上下文组装。工具 Prompt 以独立文件或引用存在，由后续 ContextBuilder 在构建 Model Context 时加载。本文也不改变 `SessionEngine` 与 `AgentLoop` 的主循环职责；`AgentLoop` 提交一个 AssistantMessage 的工具批次，`ToolExecutor` 返回按调用关联的终态 `ToolResult`。

## 2. 设计原则

- 注册表在启动阶段显式构建，冻结后只读；会话只使用不可变的启用工具视图。
- 工具名是全局唯一短名。注册冲突、重复名称和 schema 不兼容在冻结阶段拒绝启动。
- 参数模型是 Pydantic 模型，`extra="forbid"`；schema 中的 alias 是唯一的外部字段名。
- 快速检查是优化，不是校验真相源；严格 Pydantic 校验是唯一权威校验。
- 预期失败变成模型可见的结构化 `ToolResult`；内部不变量破坏直接抛出。
- 只有确定无副作用的只读工具可并发；写入和其他副作用工具严格串行。
- 工具实现统一为异步 handler，运行时能力通过 `ExecutionContext` 显式传入。
- 大结果由受控 ArtifactStore 外置，模型通过普通 `read_file(offset, limit)` 分页读取。

## 3. 核心对象

### 3.1 ToolSpec

`ToolSpec` 是不可变注册描述，至少包含：

```python
ToolSpec(
    name: str,                         # 全局唯一短名
    input_model: type[BaseModel],
    handler: AsyncToolHandler,
    prompt_ref: PromptRef | None,      # 仅供上下文模块使用
    resolve_targets: TargetResolver,
    classify: ExecutionClassifier,
    retry_policy: RetryPolicy,
    timeout_seconds: float | None,
    result_policy: ResultPolicy,
    function_schema: OpenAIFunctionSchema,  # 冻结时生成
)
```

`handler` 的形态为：

```python
async def handler(args: ToolInput, ctx: ExecutionContext) -> ToolContent:
    ...
```

handler 只接收已验证模型，不接收原始 JSON。同步底层 API 必须由工具内部显式放入 `asyncio.to_thread()`，Registry 不隐式包装同步函数。

### 3.2 ToolRegistry

composition root 显式构建 Registry：

```python
registry = ToolRegistry([read_file_spec, grep_spec, ...])
registry.freeze()
```

冻结过程：

1. 校验名称非空且全局唯一。
2. 校验 input model 的 `extra="forbid"`，并生成 JSON Schema。
3. 为每个工具生成包装参数 schema，加入框架字段 `correction_of_tool_use_id: str | None`。
4. 将本地 `$defs/$ref` 解析为 OpenAI-compatible schema，保证 object 节点使用 `additionalProperties: false`，并设置 `strict: true`。
5. 对无法无损表达的结构、递归模型或不支持关键字报告工具名和 schema 路径并拒绝冻结。
6. 缓存不可变 function schema。

Prompt 不写入 input model，也不由 Registry 负责拼装；Registry 仅保存 `prompt_ref`。

### 3.3 ToolTarget

工具参数校验成功后，`resolve_targets(validated_input)` 派生零个或多个内部 `ToolTarget`：

```python
ToolTarget(
    kind="file",
    operation="read",
    value="src/app.py",  # 规范化后的受控值
)
```

文件目标以会话 workspace root 为基准解析。默认拒绝越界 `..`、绝对路径和解析符号链接后逃出 workspace root 的路径。读写能力、文件/目录限制和其他策略由工具自己的目标策略检查。

### 3.4 ExecutionTraits

目标解析后由无副作用、确定性的分类器计算：

```python
ExecutionTraits(concurrency_safe: bool)
```

只有明确无副作用的只读调用才能为 `True`。分类器异常时保守判为 `False`。不引入资源锁或读写集合；副作用工具一律串行。

### 3.5 ExecutionContext

`ExecutionContext` 显式承载执行期能力，例如：

- session/run/tool_use 标识；
- workspace root；
- cancellation signal；
- trace sink；
- ArtifactStore；
- 当前工具的超时和权限视图。

不得通过全局变量读取会话状态或取消信号。

## 4. Schema 与调用包装

模型看到的是 OpenAI-compatible function：

```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "由独立 Prompt 文件提供的工具说明",
    "strict": true,
    "parameters": { "type": "object", "...": "..." }
  }
}
```

Registry 生成的参数包装层始终有 `correction_of_tool_use_id`。普通调用显式传 `null`，修正调用传原始失败调用 ID。Executor 在业务校验前剥离该字段；它永远不会传给工具的 Pydantic input model。

模型只能使用 schema 暴露的 alias。快速检查、Pydantic 校验和字段错误路径都使用外部 alias，不同时接受 Python 字段名。

## 5. 执行流水线

对每个模型工具调用执行以下步骤：

1. **解析调用**：确认工具调用 ID、工具名和 arguments 可用；顶层必须是 JSON object。
2. **解析工具**：未知工具返回 `unknown_tool`，不执行、不消耗执行重试预算。
3. **框架字段检查**：读取并验证 `correction_of_tool_use_id`，确认它指向当前 Session 中同一工具、上一轮产生的可修正失败调用，且没有已有修正。禁止链式修正。
4. **快速检查**：依据冻结 schema 的顶层 required/property 集合检查缺失字段和多余字段。
5. **Pydantic 校验**：以严格模式验证 alias 命名的 arguments；嵌套模型同样禁止额外字段。
6. **目标解析**：从已验证 input 派生 `ToolTarget` 并执行 workspace、权限和能力策略检查。
7. **执行分类**：计算 `ExecutionTraits`，异常时标记为不安全。
8. **handler 执行**：在工具超时和取消边界内执行异步 handler。
9. **结果封装**：生成与 `tool_use_id`、工具名绑定的终态 `ToolResult`，必要时外置大结果。
10. **事件提交**：结果提交成功后，按 assistant 原始调用顺序加入 `messages.jsonl`/Working Context。

快速检查失败仍必须生成结构化结果；它不能替代 Pydantic 校验，也不能执行 handler。

## 6. 失败、修正与重试

### 6.1 统一失败信封

预期失败使用统一结构：

```python
ToolFailure(
    code: str,
    stage: str,
    message: str,
    field_errors: list[FieldError],
    correctable: bool,
    retryable: bool,
)
```

典型阶段包括 `fast_validation`、`pydantic_validation`、`target_policy`、`execution`。参数错误、权限拒绝、目标不存在和业务拒绝不会进入执行重试。

JSON 无法解析或顶层类型错误，只要有合法 `tool_use_id`，返回 `malformed_arguments` 并允许一次修正。重复 `tool_use_id`、缺少调用 ID、消息关联冲突等属于内部协议错误，直接抛出并终止批次。

### 6.2 参数修正

每个原始 `tool_use_id` 最多允许一次参数修正。校验失败结果中包含原调用 ID；下一次调用用 `correction_of_tool_use_id` 显式关联，并获得新的 `tool_use_id`。修正调用必须使用同一工具，且直接指向原始失败调用，不能形成 `A <- B <- C` 链。第二次失败返回 `correction_not_allowed` 终态失败，不执行工具。

### 6.3 执行重试

执行尝试总数最多为 3 次，即首次执行加最多 2 次重试。只有工具或执行器分类为 `transient` 且重试策略允许时才重试。超时或取消若无法确认没有副作用，返回 `outcome_unknown`，不自动重放。每次尝试记录 `attempt=1..3`。

## 7. 批次调度与取消

同一 AssistantMessage 的 ToolUse 构成一个逻辑批次。Executor 将调用分成最大连续安全段：

```text
safe A, safe B  -> 并发等待
unsafe C         -> 等待前段后串行执行
safe D           -> 开始下一安全段
```

并发只发生在连续的 `concurrency_safe=True` 调用中，不能跨越副作用工具。结果收齐后，按原始 ToolUse 顺序返回给 AgentLoop；并发完成顺序不改变下一轮 Model Context 的逻辑顺序。

AgentRun 取消时：

- 不再启动新调用；
- 向所有运行任务发出协作式取消；
- 等待已启动任务全部进入终态；
- 只读调用记为 `cancelled`；
- 无法确认副作用结果的调用记为 `outcome_unknown`；
- 不允许后台任务脱离 AgentRun 继续运行。

每个工具使用全局默认超时和硬上限，`ToolSpec` 只能声明更短的超时。

## 8. 结果持久化

Session 目录布局为：

```text
.mini/
└── sessions/<session_id>/
    ├── messages.jsonl
    ├── trace.jsonl
    └── tool_result/<tool_use_id>/
        ├── result.txt|result.json
        └── metadata.json
```

`messages.jsonl` 是可恢复的消息聊天记录；`trace.jsonl` 按真实事件顺序完整记录调用开始、尝试、重试、结束和错误；`tool_result` 只保存最终结果，不保存每次 retry 的中间输出。

默认输出超过 50 KB 时外置；`grep` 工具超过 20 KB 时外置。阈值由 `ToolSpec` 的结果策略决定，工具只能收紧而不能突破系统硬上限。外置结果的模型可见内容必须包含：

- 原输出已被截断并外置的说明；
- 确定性预览；
- 受控结果文件路径或引用；
- 使用 `read_file` 的读取方式。

结果文件位于 workspace 内的 `.mini/sessions/...`，因此复用已有 workspace 路径策略。模型通过 `read_file(path, offset, limit)` 分页读取，不新增重复的专用结果工具。Executor/ArtifactStore 生成路径，工具不能返回任意本地路径。

并发调用的 `trace.jsonl` 按真实完成事件追加；批次结果在 `messages.jsonl` 中按原始 ToolUse 顺序提交。只有 Session 接受结果后，结果才进入 Working Context。

## 9. 核心不变量

1. Registry 冻结后不可变，工具名全局唯一。
2. 每个 function schema 与冻结时的 input model 一致，schema 不兼容阻止启动。
3. handler 永远收到严格验证后的 Pydantic 模型和显式 `ExecutionContext`。
4. 未通过目标策略的调用不会执行 handler。
5. 参数修正每个原始调用最多一次，不能链式绕过。
6. 执行尝试最多三次，非 transient 或不确定副作用不自动重试。
7. 副作用工具不并发，安全段不跨越串行屏障。
8. ToolResult 始终显式绑定 `tool_use_id` 和工具名。
9. 未被 Session 接受的结果不得进入 Working Context。
10. `messages.jsonl` 的工具结果按 assistant 调用顺序排列，`trace.jsonl` 保留真实事件顺序。
11. 结果外置路径由 ArtifactStore 生成，并位于当前 Session 的受控目录。
12. 内部 ID 冲突、消息关联冲突和 Registry 契约破坏不得伪装成普通 ToolFailure。

## 10. 建议测试场景

- 重复工具名、缺少 `extra="forbid"`、不支持 schema 关键字导致冻结失败；
- alias 字段的缺失、多余字段和严格类型错误；
- unknown tool、malformed arguments、重复 ID 和消息关联冲突；
- 一次参数修正成功、二次修正被拒绝、跨工具或链式修正被拒绝；
- transient 执行失败重试至成功、三次耗尽、非 transient 不重试；
- 超时后结果未知且不重放；
- 连续安全段并发，副作用工具形成屏障，结果按原始顺序提交；
- 取消时不启动新工具、所有已启动任务进入终态；
- 默认 50 KB 与 grep 20 KB 外置阈值、预览和 `read_file(offset, limit)` 读取；
- `messages.jsonl` 与 `trace.jsonl` 分别验证逻辑顺序和真实事件顺序；
- 进程恢复时仅恢复消息记录，不重复执行未知结果的工具。
