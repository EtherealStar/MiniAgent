# 实现 Hook 注册表、调度器与四个生命周期插入点

本 ExecPlan 是一份持续维护的实现计划，必须遵守仓库根目录 `PLANS.md`。本文从当前工作树出发，落实 `docs/design-docs/hook-registry-and-lifecycle.md` 已确定的设计：提供启动期注册并冻结的 `HookRegistry`、按时机分发的 `HookDispatcher`，以及 `PreModelCall`、`AssistantMessageCompleted`、`PreToolUse`、`PostToolUse` 四个强类型异步插入点。实现完成后，扩展代码可以在不持有 `SessionEngine`、Transcript、Working Context 或 ToolExecutor 可变状态的前提下参与 AgentRun；空注册表仍保持当前行为。

## Purpose / Big Picture

用户和后续开发者将能把审计、策略、统计或模型调用前检查接入稳定的生命周期位置，而不必修改 `AgentLoop` 的控制流。调用模型前的 Hook 可以继续、请求一次上下文压缩或明确终止 AgentRun；工具调用前的 Hook 可以执行快速 JSON/schema 检查并阻止 handler；消息和工具结果提交后的通知 Hook 即使自身失败，也不会回滚已写入的会话事实。

可观察的成功标准是：测试能够记录四个 Hook 的真实调用顺序和所见不可变上下文；模型调用前请求压缩时只压缩一次并重新检查，反复请求不会形成循环；快速检查失败会生成结构化 ToolResult 且 handler 调用次数为零；只有 `AssistantMessageCompleted` 或 `ToolResultRecorded` 被 `SessionEngine` 接受后，相应通知才发生；空 Registry 下现有主循环、工具执行和 69 项基线测试行为不变。

## Progress

- [x] （2026-07-23）阅读 `AGENTS.md`、`PLANS.md`、Hook 设计、相关架构文档、当前循环/工具代码和测试；运行基线测试。
- [x] （2026-07-23）定义四组不可变上下文、结果协议、Hook Protocol、错误类型和 Trace 字段。
- [x] （2026-07-23）实现 `HookRegistry` 的分时机注册、显式冻结、有序只读视图和空注册表。
- [x] （2026-07-23）实现 `HookDispatcher` 的控制型短路、通知型隔离、异常分类和顺序保证。
- [x] （2026-07-23）抽取工具快筛为可复用纯逻辑，并让 ToolExecutor 复用同一规则。
- [x] （2026-07-23）在 `AgentLoop` 接入四个生命周期点，并保持提交边界、结果顺序、取消和 `turn_count` 语义。
- [x] （2026-07-23）在 Textual composition root 注册默认快筛 Hook，增加无网络生命周期测试和全量回归。
- [x] （2026-07-23）完成 README、稳定接口导出、设计接口同步、静态语法检查与最终验收。

## Surprises & Discoveries

- Observation：当前 `miniagent/loop.py` 没有 Hook 端口；模型上下文组装后会立即创建 `AssistantMessageStarted` 并递增 `turn_count`。
  Evidence：`AgentLoop.run()` 在 `ContextBuilder.build()`/`force_compress()` 之后直接发出 started 事件，当前没有模型请求前的异步接缝。
- Observation：快速 JSON、顶层字段和修正字段检查目前与严格 Pydantic 校验、修正额度记录一起位于 `ToolExecutor._prepare()`。
  Evidence：`miniagent/tools/executor.py` 的 `_prepare()` 依次执行 `json.loads()`、required/property 集合检查、`model_validate(strict=True)` 和目标解析。实现 Hook 时不能复制出两套会漂移的规则，也不能让被 Hook 拒绝的调用绕开修正额度记录。
- Observation：当前代码仍使用按字符数工作的 `ContextBuilder`，完整 `ContextManager.before_model_call()` 尚未落地。
  Evidence：`docs/design-docs/exec-plans/context-management-implementation.zh-CN.md` 的里程碑均未完成；`miniagent/loop.py` 仍调用 `build()` 和 `force_compress()`。
- Observation：直接运行 `uv run pytest` 在当前 Windows 环境中无法导入本地 `miniagent`，但模块方式运行正常。
  Evidence：`uv run python -m pytest -q` 于 2026-07-23 得到 `69 passed in 0.95s`；本计划统一使用后一命令。
- Observation：`docs/reference/query.ts` 展示了 Hook 阻止继续执行后再次进入循环可能与 prompt-too-long 互相放大的实际风险。
  Evidence：该文件 1169-1172 和 1293-1297 附近的注释明确描述“错误—Hook 阻止—重试—再次错误”的死循环，因此本实现对 Hook 请求压缩设置硬性重入上限。
- Observation：Hook 计划开始时依赖的 ContextManager 前置计划随后已完成并移入 `docs/design-docs/exec-plans/completed/`。
  Evidence：当前 `ContextManager.before_model_call()`、多摘要提交和 tiktoken 预算已落地；本实现增加窄 `request_compression()` 入口后可在正式边界完成 PreModelCall。
- Observation：当前生产 composition root 位于 `miniagent/ui/app.py` 的 `_ConfiguredLoop`，而 `main.py` 只启动交互式 Textual App。
  Evidence：默认 Registry/Dispatcher 在 `_ConfiguredLoop.run()` 组装；`uv run python main.py` 不再是可自动退出的 demo，因此无网络验收使用 Textual 测试设施和全量测试。

## Decision Log

- Decision：把 Hook 实现放在新的 `miniagent/hooks/` 包中，包的公开 interface 只暴露强类型上下文、结果、Registry、只读 View 和 Dispatcher；具体列表与归并逻辑留在 implementation 内。
  Rationale：这是一个小 interface、深 implementation 的模块。调用方只学习四个生命周期方法，AgentLoop 不持有注册项，也不解释扩展实现的业务用途。
  Date/Author：2026-07-23 / Codex
- Decision：Registry 按四个时机分别注册，保留 composition root 的显式顺序；`freeze()` 幂等并返回不可变 View，冻结后注册抛出 `HookRegistryError`，不增加优先级、去重和运行期增删。
  Rationale：顺序就是第一版唯一的组合规则，避免万能 Hook 类型和隐式排序；同一实例重复注册可能是调用方的有意行为，Registry 不擅自改写。
  Date/Author：2026-07-23 / Codex
- Decision：`PreModelCall` 和 `PreToolUse` 是控制型 Hook，遇到第一个非 Continue 结果即按注册顺序短路；两个通知型 Hook 始终调用全部注册项，逐个记录异常后继续。
  Rationale：短路让控制结果确定且避免已决定终止后继续产生副作用；通知发生在正式事实提交后，失败不应回滚事实，也不应阻止其他观察者。
  Date/Author：2026-07-23 / Codex
- Decision：一次目标 ModelCall 的 Hook 额外压缩最多执行一次；压缩后从第一个 `PreModelCall` Hook 重新检查，第二次 `RequestCompression` 以 `HOOK_FAILED` 和 `hook_compression_loop` 明确终止，不发目标模型请求。
  Rationale：重新检查让 Hook 看到压缩后的不可变 ModelContext；硬上限消除扩展配置造成的无限压缩循环。压缩本身仍由 ContextManager 完成，不增加 `turn_count`。
  Date/Author：2026-07-23 / Codex
- Decision：工具快筛抽为 `miniagent/tools/validation.py` 中的纯函数；默认 `FastToolValidationHook` 使用它，ToolExecutor 对 Continue 路径仍执行同源防御性检查和严格 Pydantic 校验。AgentLoop 将逐调用的快筛结果与原批次一起交给 ToolExecutor，由 Executor 统一生成 ToolResult、维护修正额度并按原始顺序归并。
  Rationale：Hook 的拒绝可以避免 handler 和昂贵后续准备，同时 ToolExecutor 仍是真相边界；由 Executor 生成失败能保留 `correction_of_tool_use_id` 的一次修正规则，避免 AgentLoop 了解 ToolFailure 信封。
  Date/Author：2026-07-23 / Codex
- Decision：增加 `HOOK_ABORTED` 与 `HOOK_FAILED` 两个明确 StopReason；用户或策略主动返回 Abort 使用前者，控制型 Hook 抛异常、返回非法结果或触发压缩重入保护使用后者。通知 Hook 异常不改变 StopReason。
  Rationale：主动控制和扩展故障具有不同运维含义，不能伪装为模型、上下文或工具失败。
  Date/Author：2026-07-23 / Codex
- Decision：`PreModelCall` 的完整接入以 checked-in 的上下文管理 ExecPlan 完成 `ContextManager.before_model_call()` 和显式一次压缩入口为前置条件；其余 Registry、Dispatcher 和三个插入点可以先独立实现并验证。
  Rationale：继续扩展旧 `ContextBuilder.force_compress()` 会固化将被删除的字符预算和单摘要行为。计划通过里程碑顺序保持当前工作树可增量验证，又不制造一次性兼容机制。
  Date/Author：2026-07-23 / Codex
- Decision：在工具模块定义不可变 `PreToolUseOutcome`，AgentLoop 只把 Hook 决定映射为该计划；ToolExecutor 验证批次对齐并独占 ToolFailure 封装与修正台账。
  Rationale：这让 Hook 模块不依赖 Executor 内部失败信封，同时保证快筛拒绝与直接执行路径使用同一台账和结果顺序。
  Date/Author：2026-07-23 / Codex
- Decision：生产默认 Hook 在 `miniagent/ui/app.py` 注册，不在 `main.py` 注册。
  Rationale：当前 `main.py` 只是 Textual 启动器，模型、工具、ContextManager 和 AgentLoop 的真实 composition root 是 `_ConfiguredLoop.run()`。
  Date/Author：2026-07-23 / Codex

## Outcomes & Retrospective

四个生命周期点现已全部接入 AgentLoop。PreModelCall 在 Assistant identity、started 更新、turn_count 和模型请求之前运行，可中止或显式压缩一次；重复压缩以 `HOOK_FAILED/hook_compression_loop` 终止。PreToolUse 在整个批次任何 handler 启动前完成，拒绝由 ToolExecutor 生成 `attempts=0` 的结构化失败，并保留一次修正语义。两个通知只在对应 Journal 事实提交后发生，异常与 Trace 写入失败均不回滚事实。Textual composition root 默认注册 `FastToolValidationHook`，空 Registry 仍保持旧行为。

新增模型和工具生命周期、提交边界、预检对齐、修正额度及 Trace 隔离测试；最终全量结果为 `145 passed in 2.47s`，静态语法检查通过。没有加入优先级、热更新、参数替换、流式 delta 或 RunTerminated Hook；保留的兼容行为只有 AgentLoop 未显式注入 Dispatcher 时自动使用冻结的空 Registry。

## Context and Orientation

`miniagent/loop.py` 的 `AgentLoop` 执行一条用户输入对应的 AgentRun，循环构建模型输入、消费模型流、提交完整 AssistantMessage、执行工具并提交 ToolResult。`miniagent/session.py` 的 `SessionEngine` 是正式会话事实的接受者；在本计划中，调用 `event_sink.emit()` 成功返回表示该事实已被接受，抛出 `EventCommitError` 表示未接受。通知 Hook 必须位于成功返回之后。

`miniagent/context.py` 已实现 `ContextManager.before_model_call()` 与显式 `request_compression()`。Model Context 是一次模型请求真正使用的不可变输入投影。`PreModelCall` 位于首次组装完成后、任何 `AssistantMessageStarted`、`turn_count` 增加或 `ModelAdapter.stream()` 之前。Hook 请求压缩只表达控制决定；ContextManager 才选择压缩范围、调用压缩模型并通过 SessionEngine 提交 ContextSummary。

`miniagent/tools/registry.py` 在启动阶段冻结 ToolSpec，`miniagent/tools/executor.py` 把 ToolUse 批次变成终态 ToolResult。快速检查只验证 arguments 是 JSON object、外部 alias 命名的顶层 required/property 集合和框架字段；严格 Pydantic 校验、目标策略、重试、取消和 handler 调用仍属于 ToolExecutor。快速检查失败是模型可见的预期 ToolFailure；缺失或重复 `tool_use_id`、结果关联错误属于内部协议错误，Hook 不能把它们降级为普通失败。

Hook Context 是传给某个生命周期实现的冻结数据快照。Hook Result 是控制型 Hook 返回的明确决定。通知型 Hook 返回 `None`。Trace 是非权威诊断记录，通知 Hook 的异常写入 Trace，但不进入 Message Journal，也不改变 Working Context。

## 参考文档与阅读顺序

实现者在开始工作和进入相关里程碑前必须按以下顺序阅读；不能只阅读本计划。读到与当前工作树不一致的事实时，先写入 `Surprises & Discoveries`，再更新相应决定和步骤。

1. `AGENTS.md`：仓库级工作约束；当前为空，但每次执行计划前仍需确认它没有新增内容。
2. `PLANS.md`：ExecPlan 的自包含、持续维护、里程碑、验证和变更说明要求。
3. `CONTEXT.md`：Hook、四个生命周期点、Registry、Dispatcher、AgentRun、ModelCall、Turn、Journal Record 和 ToolFailure 的规范术语。
4. `docs/design-docs/hook-registry-and-lifecycle.md`：本计划的权威局部契约，尤其是注册冻结、四个触发时机、控制结果和异常语义。
5. `docs/design-docs/overall-architecture.md`：SessionEngine、AgentLoop、ContextManager、ToolView 的状态所有权，以及不可替代的提交边界。
6. `docs/design-docs/main-loop.md`：AssistantMessage、工具批次、取消、恢复、`turn_count` 和 AgentRunResult 语义。
7. `docs/design-docs/context-management.md` 与 `docs/design-docs/exec-plans/context-management-implementation.zh-CN.md`：`before_model_call()`、压缩 80%/50% 规则、ContextSummary 提交，以及本计划的前置接口。
8. `docs/design-docs/tool-registry-and-execution.md`：快筛、严格 Pydantic 校验、ToolFailure、一次修正、批次顺序、取消和 `outcome_unknown` 约束。
9. `docs/design-docs/persistence-and-observability.md`：Journal Record、SessionUpdate 与 Trace 的权威性差异，确认通知失败只能记录 Trace。
10. `docs/reference/query.ts` 的 980-1055、1150-1310、1360-1530 行和 `docs/reference/QueryEngine.ts` 的 310-345、1040-1065 行：只作为成熟 Agent 循环中 post-sampling/stop Hook 顺序、异步通知和重入风险的比较材料，不覆盖 MiniAgent 设计。
11. `miniagent/loop.py`、`miniagent/ports.py`、`miniagent/domain.py`、`miniagent/events.py`、`miniagent/session.py`、`miniagent/context.py`：确认主流程和领域类型现状。
12. `miniagent/tools/registry.py`、`miniagent/tools/executor.py`、`miniagent/tools/schema.py`、`miniagent/tools/models.py`：确认冻结 ToolSpec、快筛、严格校验和失败信封的当前实现。
13. `tests/test_loop.py`、`tests/test_session.py`、`tests/tools/test_executor.py`、`tests/tools/test_integration.py`：沿用已有 ScriptedModel、EventSink、假 handler 和批次测试方式，不访问网络。

## Plan of Work

### Milestone 1：建立强类型 Hook 模型与冻结 Registry

创建 `miniagent/hooks/` 包，并在 `miniagent/hooks/models.py` 定义四个互不混用的 frozen dataclass Context。`PreModelCallContext` 至少携带 `run_id`、即将发出的 `turn_number`、不可变 ModelContext 和本次冻结 ToolView 标识；`AssistantMessageCompletedContext` 携带 run、已接受 Message 和 finish reason；`PreToolUseContext` 携带 run、所属 assistant message ID、ToolUse、匹配到的只读 ToolSpec/schema；`PostToolUseContext` 携带 run、已接受的 ToolResult/Tool Message。Context 不包含 SessionEngine、Repository、可变 Registry、ToolExecutor 或可调用的提交函数。

为每个时机定义独立 Protocol。`PreModelCallHook.__call__()` 返回 `ContinueModelCall`、`RequestCompression` 或 `AbortRun`；`PreToolUseHook.__call__()` 返回 `ContinueToolUse` 或 `RejectToolUse`；两个通知 Hook 返回 `None`。结果使用明确 frozen 类型，不使用字符串、布尔值、任意 dict 或一个包含所有字段的通用结果。`AbortRun` 包含稳定 code 和面向日志的非敏感 message；`RejectToolUse` 包含快筛失败所需的 code、message 和字段错误，但不能替换 arguments。

在 `miniagent/hooks/registry.py` 实现 `HookRegistry` 与 `HookRegistryView`。提供 `register_pre_model_call()`、`register_assistant_message_completed()`、`register_pre_tool_use()`、`register_post_tool_use()`；逐项校验对象可异步调用，按注册顺序保存。`freeze()` 生成只读 tuple View，并使后续注册失败；重复 freeze 返回同一语义的 View。空 Registry 可冻结且四组 tuple 都为空。测试文件 `tests/hooks/test_registry.py` 覆盖类型误注册、顺序、重复实例、冻结后注册、重复 freeze、空 Registry 和调用方无法修改 View。

里程碑验收命令为：

    uv run python -m pytest tests/hooks/test_registry.py -q
    uv run python -m compileall miniagent/hooks

预期所有 Registry 测试通过；此时尚未修改 AgentLoop，现有 69 项测试也必须继续通过。

### Milestone 2：实现深模块 HookDispatcher 与异常/Trace 语义

在 `miniagent/hooks/dispatcher.py` 实现 `HookDispatcher`，构造时只接受冻结的 `HookRegistryView` 和可选窄 `TraceSink`。公开 interface 只有四个按时机命名的 async 方法，不暴露列表和通用 `dispatch(name, payload)`。Dispatcher 自己构造或接收已经冻结的专属 Context，并验证 Hook 返回类型。

`before_model_call()` 按序调用，Continue 才进入下一项；RequestCompression 或 AbortRun 立即返回。`before_tool_use()` 同理，RejectToolUse 立即返回。控制 Hook 抛出的非取消异常包装为带 phase、注册序号和实现名称的 `HookExecutionError`；`asyncio.CancelledError` 原样传播。通知方法逐个调用所有 Hook；每个非取消异常写 `hook_notification_failed` Trace，字段包含 phase、hook name/index、run_id、异常类别，不包含 prompt、arguments、工具正文或秘密；然后继续下一个。非法返回值按控制 Hook 故障处理，不能默认为 Continue。

在 `tests/hooks/test_dispatcher.py` 使用记录型异步 Hook 覆盖注册顺序、首个决定短路、非法返回、异常包装、取消传播、通知失败继续、多个通知失败分别记录和空 View 的 identity 行为。删除 Registry 后，Dispatcher 已持有的 View 仍能工作，证明执行时不依赖可变 Registry。

### Milestone 3：抽取工具快筛并接入 PreToolUse 批次计划

创建 `miniagent/tools/validation.py`，把 `ToolExecutor._prepare()` 中 JSON object、`correction_of_tool_use_id` 存在性以及 frozen schema 顶层 required/property 的检查抽成无副作用纯函数。输入是 ToolUse 与匹配 ToolSpec/schema，输出为结构化 `FastValidationResult`；字段名统一使用模型看到的 alias。未知工具不由快筛伪装处理，继续交给 ToolExecutor 的 resolve_tool 阶段。缺失/重复 `tool_use_id` 仍在任何 Hook 之前由批次协议校验抛出 `ToolProtocolError`。

在 `miniagent/hooks/builtin.py` 实现 `FastToolValidationHook`，只调用上述纯函数并映射成 ContinueToolUse 或 RejectToolUse。它不持有修正额度、Registry 或 Executor 的可变状态。修改 `ToolExecutor.submit_batch()` 的端口，额外接收与 `batch.tool_uses` 一一对应、携带 tool_use_id 的不可变 `PreToolUseOutcome` tuple；Executor 首先验证数量、顺序和 ID 关联。Reject 由 Executor 转成既有 `ToolFailure(stage="fast_validation", correctable=True)` 和 ToolResult，并更新现有 `_correctable` 台账；Continue 进入 `_prepare()`，且 `_prepare()` 仍调用同一个纯函数做防御性快筛，再执行严格 Pydantic 校验。这样自定义 Dispatcher、漏注册默认 Hook 或直接调用 Executor 都不能让未严格校验的输入进入 handler。

修改 `AgentLoop` 的工具批次路径：先验证批次 ID 协议，为每个 ToolUse 构造独立 Context 并按模型顺序调用 Dispatcher，再一次性把原始 batch 和 outcomes 交给 ToolExecutor。任一 PreToolUse Hook 非预期失败时，整个批次尚未启动任何 handler，AgentRun 以 `HOOK_FAILED` 结束。Executor 返回结果后仍使用 `_order_results()` 校验并按模型顺序提交。

更新 `tests/tools/test_executor.py`，证明直接调用 Executor 仍严格校验；增加 `tests/hooks/test_tool_lifecycle.py`，覆盖 malformed JSON、非 object、required/extra、框架字段、嵌套 Pydantic 失败、一次修正、同批次修正拒绝、混合通过/拒绝批次、outcome 对齐错误和 handler 调用次数。聚焦验收预期快筛失败 `attempts=0`、`stage=fast_validation`、handler 未运行；通过快筛但类型错误仍由 `pydantic_validation` 拒绝。

### Milestone 4：接入提交后的两个通知时机

修改 `miniagent/loop.py`，在 `await event_sink.emit(AssistantMessageCompleted(...))` 成功返回后立即调用 `dispatcher.assistant_message_completed()`，然后才把该消息用于工具批次或下一步循环。若 emit 抛出 `EventCommitError`，Hook 不调用；通知 Hook 失败只留下 Trace，消息仍加入已接受 Working Context，后续控制流不变。草稿取消、Provider 失败和 `AssistantMessageDiscarded` 均不触发该通知。

每个 ToolResult 继续按原 ToolUse 顺序创建 Tool Message 并提交 `ToolResultRecorded`；每次 emit 成功后立即调用 `dispatcher.after_tool_use()`。提交失败时该结果的 PostToolUse 不调用，后续结果也不假定已提交。通知只能看到已接受结果，不改写 content、failure、tool_use_id、assistant_message_id 或 `outcome_unknown`。

测试用记录 EventSink 在 emit 前、emit 成功和 emit 失败三个阶段留下时间线，断言顺序严格为 `assistant commit -> assistant hook -> pre-tool hook -> executor -> tool result commit -> post-tool hook`。另覆盖无 ToolUse 的消息、多个结果的模型顺序、通知异常继续、草稿作废不通知和提交失败不通知。

### Milestone 5：在 ContextManager 之后接入 PreModelCall

先完成并验证 `docs/design-docs/exec-plans/context-management-implementation.zh-CN.md` 至少到里程碑 5，使 AgentLoop 使用 `ContextManager.before_model_call()`，并提供一个明确的“在同一 ModelCall 准备阶段请求一次额外压缩并返回新 ModelContext”的窄入口。若该前置条件未满足，停留在本里程碑之前，更新 Progress；不得把 Hook 接到旧 `force_compress()` 后宣称完成。

修改 AgentLoop 的每次模型调用准备流程：获取同一个冻结 ToolView，调用 ContextManager 得到首次 ModelContext，然后构造 PreModelCallContext 并调用 Dispatcher。Continue 才创建 Assistant message ID、发布 started 事件、增加 `turn_count` 并调用 ModelAdapter；AbortRun 发布 RunTerminated 并返回 `HOOK_ABORTED`，不发模型请求且不增加 turn；HookExecutionError 返回 `HOOK_FAILED`。RequestCompression 调用 ContextManager 的单次额外压缩入口，等待 ContextSummary 被 SessionEngine 接受、重新组装 ModelContext，再从第一个 PreModelCall Hook 重跑。第二次请求压缩立即返回 `HOOK_FAILED/hook_compression_loop`。

若 ContextManager 自己已因 80% 阈值完成常规压缩，PreModelCall 看到的是常规压缩后的首次 ModelContext；Hook 仍只有一次额外请求额度。Hook 不接收 compressor、commit port 或可变 WorkingContext，不能自己修改内容。压缩调用和 Hook 执行均不是 Turn。取消在 Hook 或压缩期间发生时沿现有 `CANCELLED` 路径结束，不包装成 Hook 故障。

在 `tests/hooks/test_model_lifecycle.py` 使用 Fake ContextManager、Fake ModelAdapter 和记录 EventSink 覆盖 Continue、Abort、一次压缩后 Continue、二次 RequestCompression、压缩失败、控制 Hook 异常、取消、多个 Hook 顺序和 ToolView/ModelContext 快照。断言未 Continue 的所有路径中 `model.stream_calls == 0`、没有 AssistantMessageStarted、`turn_count == 0`；一次压缩成功路径只有一个目标 ModelCall，且目标模型拿到重建后的 Context。

### Milestone 6：composition root、演示、回归与文档收尾

在 `main.py` 的 composition root 显式创建 HookRegistry，注册 `FastToolValidationHook`，冻结后构造 Dispatcher，并注入 AgentLoop。若要展示顺序，增加只记录 phase 名称的 demo Hook，不打印 prompt、arguments 或结果正文。默认生产配置不注册改变模型调用控制的示例 Hook。公开包导出保持窄：调用方可导入 Registry、Dispatcher、四个 Protocol/Context/Result 和默认快筛 Hook，不导出内部 list、归并器或可变状态。

更新 `README.md` 的开发验证段，说明如何在 composition root 注册、必须 freeze、Context 不可变、通知异常只进 Trace，以及空 Registry 的行为。同步检查 `CONTEXT.md` 和 `docs/design-docs/hook-registry-and-lifecycle.md`；只有实现中确认了新的稳定事实才修改设计文档，并在 Decision Log 记录原因。

运行聚焦、全量、语法和演示命令。把实际测试数量和短输出写入 Artifacts，将 Progress 全部更新为完成，并在 Outcomes 说明是否存在兼容层或未实现的后续 Hook。第一版不得顺带加入优先级、依赖图、热更新、参数替换、流式 delta Hook 或 RunTerminated Hook。

## Concrete Steps

所有命令都在 `D:\study\MiniAgent` 执行。每次开始前先查看工作树，只处理本计划涉及文件，不覆盖用户在 `PLANS.md`、现有 ExecPlan、参考文档或其他文件中的改动。

1. 记录基线和前置计划状态：

       git status --short
       uv run python -m pytest -q
       rg -n "ContextManager|before_model_call|force_compress|Hook" miniagent tests docs/design-docs/exec-plans

   当前已知基线为 `69 passed`。如果数量因其他计划实施而变化，记录新的数量和原因，不把数量变化本身视为失败。

2. 完成 Registry 和 Dispatcher 后运行：

       uv run python -m pytest tests/hooks/test_registry.py tests/hooks/test_dispatcher.py -q
       uv run python -m pytest -q

3. 完成工具接入后运行：

       uv run python -m pytest tests/hooks/test_tool_lifecycle.py tests/tools/test_executor.py tests/tools/test_integration.py -q
       uv run python -m pytest -q

4. 完成主循环四个时机后运行：

       uv run python -m pytest tests/hooks tests/test_loop.py tests/test_session.py -q
       uv run python -m pytest -q

5. 最终检查语法和无网络演示：

       uv run python -m compileall miniagent tests main.py
       uv run python -m pytest tests/ui/test_app_lifecycle.py -q

   自动验收应退出码为 0。交互式应用可另用 `uv run python -m miniagent.ui` 启动；自动测试通过 Textual test facilities 验证生命周期，不访问 Provider 或打印秘密与完整输入正文。

## Validation and Acceptance

Registry 验收要求四类实现不能注册到错误时机，冻结后不能修改，显式顺序稳定，空注册表通过 Dispatcher 后等同 Continue/无通知。测试必须证明 AgentLoop 只依赖 Dispatcher 的窄 interface，不读取 Registry View 中的 tuple，也不根据具体 Hook 类名分支。

PreModelCall 验收要求它在首次 ModelContext 组装后、AssistantMessageStarted 和真实请求前发生。Continue 发出一次模型请求并增加一次 turn；Abort 和 Hook 故障不发请求、不增加 turn；RequestCompression 最多额外压缩一次并用新 Context 重跑 Hook。反复请求、压缩失败和取消都有确定终态，不会循环。

AssistantMessageCompleted 验收要求完整消息 commit 成功后恰好通知一次；流中断、取消草稿、provider 错误和 commit 失败通知零次。通知实现抛异常时，其他通知仍运行，消息仍是正式 Working Context，循环结果与空通知时相同。

PreToolUse 验收要求发生在任何 handler 前。合法 JSON、顶层 required/property 和框架修正字段由同一纯逻辑在默认 Hook 与 Executor 防御边界使用；通过快筛仍必须经过 strict Pydantic。快筛失败生成保留 tool_use_id/assistant_message_id 的结构化 ToolResult，attempts 为 0，handler 次数为 0；一次修正规则与当前 Executor 行为一致。缺失/重复 ID 和 outcome 对齐错误仍抛内部协议错误。

PostToolUse 验收要求每个终态结果被 SessionEngine 接受后按原 ToolUse 顺序通知。通知失败不修改 ToolResult、不回滚 Journal Record、不改变 `outcome_unknown`，并生成不含正文的 Trace。ToolResult commit 失败时对应通知不发生。

最终运行 `uv run python -m pytest -q` 必须退出 0。除 Hook 专用测试外，既有 provider、context、session、loop、tool registry/executor/grep/integration 测试全部通过；用 Fake/记录对象证明时序，不访问真实模型或网络。

## Idempotence and Recovery

重复构建和冻结 Registry 不改变注册顺序；冻结后的 View 是不可变快照。Dispatcher 每次调用只读取 View。测试和 demo 可重复运行，不写 Session 之外的持久状态。默认快筛 Hook 无状态；修正额度仍只由每个 ToolExecutor 实例维护。

如果实现中途失败，保留已通过的较早里程碑和测试，从当前 Progress 中第一个未完成项继续。接口迁移采用先增加默认空 Dispatcher/兼容参数、迁移 composition root 与测试、最后删除旧分支的顺序，使每一步都能运行测试。不得用 `git reset --hard`、删除用户文件或覆盖现有未提交计划恢复。

如果 ContextManager 前置计划尚未完成，Registry、Dispatcher、工具和通知里程碑可以保持已完成，PreModelCall 留为未完成；不要用旧字符压缩机制临时模拟。若 ToolExecutor 已被其他工作重构，优先保留“同源快筛、Executor 严格真相边界、修正台账唯一所有者”三个不变量，并在 Decision Log 更新具体接口。

## Artifacts and Notes

实施时在此追加短证据，格式示例：

    Registry：4 类 Hook 按注册顺序冻结；冻结后注册得到 HookRegistryError。
    PreModelCall：首次 request_compression，重建 Context 后 continue；compress_calls=1，model_calls=1，turn_count=1。
    循环保护：第二次 request_compression；model_calls=0，StopReason=HOOK_FAILED，category=hook_compression_loop。
    PreToolUse：invalid_arguments/fast_validation；attempts=0，handler_calls=0；修正调用成功。
    通知时序：assistant_commit -> assistant_hook -> pre_tool -> handler -> result_commit -> post_tool。
    全量测试：<N> passed in <duration>。

不要记录完整 prompt、工具 arguments、工具结果、API Key、环境变量值或用户会话正文。

    Registry/Dispatcher 与生命周期：18 passed；冻结、顺序、短路、非法返回、取消、提交边界与通知隔离均有覆盖。
    语法检查：uv run python -m compileall miniagent tests main.py 通过。
    PreModelCall：单次请求压缩后重检并发出 1 次目标 ModelCall；重复请求以 hook_compression_loop 终止且 turn_count=0。
    PreToolUse：拒绝结果 attempts=0、handler_calls=0；outcome 错位在修改 Executor 状态前抛 ToolProtocolError。
    通知时序：assistant_commit -> assistant_hook -> pre_tool -> handler -> result_commit -> post_tool。
    全量回归：145 passed in 2.47s。

## Interfaces and Dependencies

最终公开 interface 至少表达以下语义；名称可为避免与已有事件类冲突作小幅调整，但四组 Context 和 Result 不得合并为万能类型：

    class PreModelCallHook(Protocol):
        async def __call__(self, context: PreModelCallContext) -> PreModelCallResult: ...

    class AssistantMessageCompletedHook(Protocol):
        async def __call__(self, context: AssistantMessageCompletedContext) -> None: ...

    class PreToolUseHook(Protocol):
        async def __call__(self, context: PreToolUseContext) -> PreToolUseResult: ...

    class PostToolUseHook(Protocol):
        async def __call__(self, context: PostToolUseContext) -> None: ...

    PreModelCallResult = ContinueModelCall | RequestCompression | AbortRun
    PreToolUseResult = ContinueToolUse | RejectToolUse

    class HookRegistry:
        def register_pre_model_call(self, hook: PreModelCallHook) -> None: ...
        def register_assistant_message_completed(self, hook: AssistantMessageCompletedHook) -> None: ...
        def register_pre_tool_use(self, hook: PreToolUseHook) -> None: ...
        def register_post_tool_use(self, hook: PostToolUseHook) -> None: ...
        def freeze(self) -> HookRegistryView: ...

    class HookDispatcher:
        async def before_model_call(self, context: PreModelCallContext) -> PreModelCallResult: ...
        async def assistant_message_completed(self, context: AssistantMessageCompletedContext) -> None: ...
        async def before_tool_use(self, context: PreToolUseContext) -> PreToolUseResult: ...
        async def after_tool_use(self, context: PostToolUseContext) -> None: ...

ToolExecutor 的具体参数可以使用单独的 `ToolPreflightPlan`，但必须是不可变且与 `ToolExecutionBatch.tool_uses` 按 tool_use_id 一一对应。AgentLoop 不构造 ToolFailure；Executor 根据 RejectToolUse 生成现有失败信封、维护 correction 台账并返回完整有序结果。Hook 模块只依赖领域快照和窄 Trace Protocol，不依赖具体 SessionEngine、Repository、UI、HTTP provider、handler、ArtifactStore 或可变 Registry。

本计划不新增第三方运行时依赖。继续使用 Python 3.11、dataclass、typing.Protocol、asyncio、Pydantic 和现有 pytest/pytest-asyncio。若为只读映射使用 `MappingProxyType`，仍需确保嵌套 schema 不可被 Hook 修改；可复用 ToolRegistry 已有深拷贝策略或在构造 Context 时生成真正的冻结投影。

变更说明（2026-07-23）：首次创建 Hook 注册表与生命周期中文 ExecPlan。根据当前 69 项测试基线、Hook 设计、旧 ContextBuilder 与 ToolExecutor 快筛现状，补充四类强类型协议、冻结/调度机制、控制与通知异常语义、工具修正台账衔接、ContextManager 前置依赖、压缩重入保护、精确参考文档、逐里程碑测试和可恢复实施顺序；当前已实现 Registry、Dispatcher、纯快筛和提交后通知，保留 ContextManager/PreToolUse 批次为后续里程碑。

变更说明（2026-07-23）：ContextManager 前置计划完成后，补齐显式压缩入口、AgentLoop 的 PreModelCall/PreToolUse、Executor 预检计划、生产默认 Hook、提交边界与重入测试；同步 README 和上下文设计接口，最终全量验证为 `145 passed in 2.47s`。根据当前架构把 composition root 从计划中的 `main.py` 更正为 `miniagent/ui/app.py`，并用 Textual 自动测试替代不会自动退出的交互式 UI 命令。
