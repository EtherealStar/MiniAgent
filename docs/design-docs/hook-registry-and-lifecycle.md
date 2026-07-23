# Hook 注册表与生命周期插入点设计

## 1. 文档目的

本文定义 MiniAgent 的 Hook 领域模型、注册边界、生命周期时机和异常语义。Hook 是流程中的可扩展插入点，不是按业务功能划分的一组回调。本文只规定“在什么时候触发、能看到什么、能提出什么结果”，不规定未来扩展实现具体完成审计、权限、统计或其他业务。

本文与以下设计保持一致：

- `docs/design-docs/overall-architecture.md` 的状态所有权和事件提交边界；
- `docs/design-docs/main-loop.md` 的 AgentLoop、ModelCall 和 ToolUse 生命周期；
- `docs/design-docs/context-management.md` 的预算检查、压缩和 `ContextManager` 责任；
- `docs/design-docs/tool-registry-and-execution.md` 的快速检查、结构化 `ToolResult` 和执行器校验边界。

## 2. 设计原则

Hook 只按生命周期时机命名。`PreModelCall`、`PreToolUse` 等名称表达流程位置；压缩判断、审计或统计只是注册在这些时机上的扩展实现，不得成为新的 Hook 类型名称。文件目标的 Workspace Root 判断和交互式 permission 是 ToolExecutor 的强制 Target Authorization 阶段，不是可选 Hook。

Hook 接收不可变的强类型上下文。Hook 不直接持有或修改 `SessionEngine`、Transcript、Working Context、ToolExecutor 等有状态对象。需要改变正式会话事实时，必须由主流程按既有事件提交边界完成。

每个 Hook 时机拥有独立的上下文和结果协议。不能为了统一调度而引入一个包含所有字段和所有决策的万能上下文或万能结果类型。

Hook 实现全部使用异步调用协议。同步实现可以在自身内部完成同步判断，但调度边界保持异步，以适配模型、上下文和工具执行流程。

## 3. 领域对象与职责

### 3.1 Hook

Hook 是绑定到一个明确生命周期时机的扩展入口。它是流程的参与者，不拥有 AgentRun、Session 或工具状态。

### 3.2 HookRegistry

`HookRegistry` 只负责注册和冻结：

- 提供按生命周期点拆分的强类型注册方法；
- 保留 composition root 中的显式注册顺序；
- `freeze()` 后禁止新增或删除注册项；
- 向 `HookDispatcher` 提供只读视图；
- 允许空注册表，空注册表不能改变现有主流程行为。

Registry 不执行 Hook，不解释 Hook 结果，也不处理 Hook 异常。冻结前的 Registry 不能交给 Dispatcher 执行。

### 3.3 HookDispatcher

`HookDispatcher` 消费冻结的 Registry，在具体生命周期点：

1. 构造该时机专属的不可变上下文；
2. 按该时机规定的调用规则执行异步 Hook；
3. 归并该时机允许的结果，或按该时机规定处理异常。

`AgentLoop` 只依赖 Dispatcher，不直接维护 Hook 列表，也不理解扩展实现的业务含义。`SessionEngine` 不依赖 Hook 机制。

## 4. Hook 注册生命周期

composition root 在应用启动阶段创建 Registry 并注册实现。注册 API 按时机拆分，避免把不同上下文误注册到错误位置：

```text
register_pre_model_call(hook)
register_assistant_message_completed(hook)
register_pre_tool_use(hook)
register_post_tool_use(hook)
```

完成注册后显式调用 `freeze()`。冻结后的注册顺序是稳定的，第一版不引入优先级、依赖图或运行期增删。Hook 实例是否被重复注册由 composition root 负责，Registry 不擅自去重或改变顺序。

有效 Hook 的启用状态可以由上层运行环境按 Session 或 AgentRun 解析，但一次已经开始的调用使用解析时得到的不可变 Hook 视图；在途调用不因注册配置变化而改变。

## 5. 生命周期插入点

### 5.1 PreModelCall

触发时机是首次 Model Context 组装完成、Model Provider 尚未发起请求时。它是 `ContextManager.before_model_call()` 检查点上的扩展入口，而不是另一套上下文管理流程。

建议的结果只有：

- `Continue`：使用当前 Model Context 发起 ModelCall；
- `RequestCompression`：请求主流程调用 ContextManager 执行一次压缩并重新组装 Model Context；
- `AbortRun`：不发起 ModelCall，由主流程产生明确的 AgentRun 终止结果。

Hook 不直接修改 Model Context、Working Context、ContextSummary 或模型参数。压缩完成后可以重新进入一次 `PreModelCall`，但必须有次数保护，防止扩展形成循环。上下文压缩仍遵守 `context-management.md` 的 80% 触发、50% 目标和 `CompressionStarted` / `CompressionCompleted` / `CompressionFailed` 事件语义。

### 5.2 AssistantMessageCompleted

当模型流被 AgentLoop 组装为完整 AssistantMessage，且 `AssistantMessageCompleted` 已被 SessionEngine 接受后触发。被取消或失败而作废的 Draft AssistantMessage 不触发该插入点。

当前该 Hook 只作为稳定的生命周期通知存在：不修改 AssistantMessage，不改变 ToolUse，不改变 AgentLoop 的下一步决策，也不回滚已经提交的 Journal Record。

### 5.3 PreToolUse

触发时机是模型产生 ToolUse、ToolExecutor 执行 handler 之前。当前该插入点只接入工具设计中的快速 JSON/schema 检查：

```text
ToolUse
  -> PreToolUse 快速检查
       -> 失败：生成结构化 ToolFailure / ToolResult，不进入 handler
       -> 通过：交给 ToolExecutor
```

快速检查覆盖合法 JSON object、顶层 required/property 集合和框架字段的快速约束。它不支持参数替换，也不负责严格 Pydantic 校验、目标策略、执行重试、取消或 handler 调用。

快速检查是优化而不是校验真相源。ToolExecutor 仍保留最终的严格校验作为防御性边界；任何路径都不能让未经严格验证的输入进入 handler。合法 `tool_use_id` 对应的预期参数错误必须产生工具设计规定的结构化失败；调用 ID 缺失、重复或消息关联冲突仍属于内部协议错误，不能伪装成 ToolFailure。

PreToolUse 不读取或批准 ToolTarget，不发起 Permission Request，也不持有 AgentRun 拒绝缓存或 Session Permission Grant。快速检查通过后，ToolExecutor 依次完成严格 Pydantic 校验、目标解析和 Target Authorization；漏注册、跳过或更换 Hook 都不能绕过授权。

### 5.4 PostToolUse

触发时机是 ToolExecutor 返回终态 ToolResult，且该结果已被 SessionEngine 接受之后。Hook 看到的是正式 Journal Record 对应的结果，而不是可能提交失败的临时结果。

当前该 Hook 只作为生命周期插入点存在：不改写 ToolResult，不改变 `tool_use_id`、工具错误信封或 `outcome_unknown` 语义，也不改变后续 AgentLoop 决策。

## 6. 调用与异常语义

每个时机的多个 Hook 按 Registry 的显式注册顺序调用；具体是否短路或归并由该时机的协议决定，而不是由 Registry 统一推断。第一版四个时机的行为如下：

- `PreModelCall`：控制结果由主流程按 `Continue`、`RequestCompression` 或 `AbortRun` 处理；控制决策不能修改上下文对象。
- `AssistantMessageCompleted`：所有注册项作为通知调用；不影响已提交消息和循环。
- `PreToolUse`：快速检查失败直接转为结构化工具失败；不提供参数替换。
- `PostToolUse`：所有注册项作为通知调用；不影响已提交结果和循环。

异常按插入点职责处理，不能统一吞掉：

- `PreModelCall` 的非预期异常属于调用前失败，不发模型请求，AgentRun 明确终止；
- `PreToolUse` 的预期参数错误进入结构化 ToolResult，非预期异常属于内部错误；
- `AssistantMessageCompleted` 和 `PostToolUse` 当前是通知 Hook，异常只记录 Trace，不回滚已接受的 Journal Record，也不改变主流程。

## 7. 关键时序

### 7.1 模型调用前

```text
ToolCapabilities.snapshot()
  -> ContextManager.before_model_call(AgentRunEnvironment, ToolView)
  -> PreModelCall
       -> Continue：ModelAdapter.stream()
       -> RequestCompression：ContextManager 压缩并重新组装
       -> AbortRun：AgentRun 明确终止
```

### 7.2 模型返回工具调用

```text
ModelAdapter.stream()
  -> AssistantMessage 组装
  -> SessionEngine 接受 AssistantMessageCompleted
  -> AssistantMessageCompleted Hook
  -> PreToolUse
  -> ToolExecutor（最终校验与执行）
  -> SessionEngine 接受 ToolResult
  -> PostToolUse Hook
```

## 8. 核心不变量

1. Hook 名称只表达生命周期时机，不按扩展业务功能命名。
2. Hook 不绕过 SessionEngine 修改正式 Session 状态。
3. 未被 SessionEngine 接受的消息、摘要或工具结果不能被后续流程当作正式 Working Context 使用。
4. `PreModelCall` 不取代 ContextManager；上下文压缩仍由 ContextManager 执行。
5. `PreToolUse` 的快速检查不取代 ToolExecutor 的严格校验。
6. Target Authorization 不是 Hook；Workspace Root 判断和 Permission Decision 不能因 Hook 配置而启用、关闭或改变顺序。
7. HookRegistry 冻结后不可修改，空 Registry 保持现有行为。
8. HookDispatcher 是执行边界，AgentLoop 不直接管理注册列表。
9. 通知型 Hook 的异常不能回滚已经提交的 Journal Record。

## 9. 当前范围与后续实现边界

本文只确定领域模型和契约，不要求立即实现所有 Hook 行为。后续实现应先提供 `HookRegistry` 的注册、冻结和只读视图，再提供 `HookDispatcher` 的时机分发与强类型上下文；最后把 AgentLoop 的关键位置接入 Dispatcher。

第一版不包含：

- 每个模型流式增量的高频 Hook；
- `RunTerminated` Hook；
- 参数替换 Hook；
- Hook 优先级或依赖图；
- 运行期动态注册和删除；
- 由 Hook 直接写 Transcript、Repository 或 Working Context。
