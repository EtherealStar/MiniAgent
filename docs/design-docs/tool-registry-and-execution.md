# 工具注册表与执行器设计

## 1. 目标与边界

本文定义 MiniAgent 的工具注册、OpenAI-compatible function schema 生成、输入输出校验、目标解析与授权、执行重试、并发调度、AgentRun 内工具可用性和工具结果持久化协议。

本文不负责 Prompt 的最终排版。工具 Prompt 以同名工具包中的独立引用存在，由 Registry 在启动时解析，并由工具模块随 ToolView 提供给 ContextManager。本文不改变 `SessionEngine` 与 `AgentLoop` 的主循环职责；`AgentLoop` 提交一个 AssistantMessage 的工具批次，`ToolExecutor` 返回按调用关联的终态 `ToolResult`。

## 2. 设计原则

- composition root 显式给出可用工具名；注册表按同名工具包约定加载、校验并冻结定义，运行期不扫描目录。
- 注册表冻结后只读；每次 ModelCall 前根据冻结定义和当前 AgentRun 已提交的工具结果重新投影不可变 ToolView。
- 工具名是全局唯一短名。注册冲突、重复名称和 schema 不兼容在冻结阶段拒绝启动。
- 输入模型和输出模型都是 Pydantic 模型，`extra="forbid"`；schema 从模型派生，不维护手写副本。
- 快速检查是优化，不是校验真相源；严格 Pydantic 校验是唯一权威校验。
- 预期失败变成模型可见的结构化 `ToolResult`；内部不变量破坏直接抛出。
- 只有确定无副作用的只读工具可并发；写入和其他副作用工具严格串行。
- 文件目标在 Current Session 冻结的 Workspace Root 内自动允许；越界目标必须由统一 Target Authorization 取得用户许可，工具和 handler 不自行实现许可旁路。
- 工具实现统一为异步 handler，运行时能力通过 `ExecutionContext` 显式传入。
- handler 只返回成功的 `ToolOutput`；失败状态、执行次数和 artifact 由 Executor 封装。
- 结果预算约束完整 `ToolOutput` 的规范 JSON；大结果由受控 ArtifactStore 外置。

## 3. 核心对象

### 3.1 ToolSpec

`ToolSpec` 是不可变注册描述，至少包含：

```python
ToolSpec(
    name: str,                         # 全局唯一短名
    input_model: type[BaseModel],
    output_model: type[ToolOutput],
    handler: AsyncToolHandler,
    prompt_ref: PromptRef,
    resolve_targets: TargetResolver,
    classify: ExecutionClassifier,
    retry_policy: RetryPolicy,
    timeout_seconds: float | None,
    result_policy: ResultPolicy,
    function_schema: OpenAIFunctionSchema,  # 冻结时生成
    output_schema: JsonSchema,              # 冻结时生成
    prompt: str,                            # 冻结时解析
)
```

`handler` 的形态为：

```python
async def handler(args: ToolInput, ctx: ExecutionContext) -> ToolOutput:
    ...
```

handler 只接收已验证输入模型，不接收原始 JSON；成功时必须返回本工具声明的输出模型实例。同步底层 API 必须由工具内部显式放入 `asyncio.to_thread()`，Registry 不隐式包装同步函数。

### 3.2 ToolOutput

`ToolOutput` 是工具成功执行后的业务结果，不是执行终态信封：

```python
class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: str
    metadata: dict[str, object] = Field(default_factory=dict)
    data: dict[str, object] = Field(default_factory=dict)
```

- `content` 是模型可见文本的基础内容；
- `metadata` 保存路径、数量、截断标志等执行事实；
- `data` 保存 UI、Trace 或未来 SDK 使用的业务数据；
- `ToolOutput` 不包含 `is_error`、attempt、artifact 或 retry 状态；
- 具体工具可以用嵌套 Pydantic 模型收紧 `metadata` 和 `data`；
- output model 不符合声明是内部工具契约错误，不转换为模型可修正失败，也不自动重试。

### 3.3 ToolRegistry

composition root 显式构建 Registry：

```python
registry = ToolRegistry(available_names=("read_file", "grep", ...))
registry.freeze()
```

每个内置工具位于 `miniagent/tools/<tool_name>/`，至少包含 `tool.py` 和 `prompt.py`。Registry 按显式名称和固定包约定加载定义；新增目录不会自动成为可用工具。

冻结过程：

1. 校验名称非空且全局唯一。
2. 校验 input model 和 output model 的 `extra="forbid"`，并分别生成 schema。
3. 为每个工具生成包装参数 schema，加入框架字段 `correction_of_tool_use_id: str | None`。
4. 将 input model 的本地 `$defs/$ref` 解析为 OpenAI-compatible schema，保证 object 节点使用 `additionalProperties: false`，并设置 `strict: true`。
5. 对无法无损表达的结构、递归模型或不支持关键字报告工具名和 schema 路径并拒绝冻结。
6. 按 `python.module:SYMBOL` 解析 `prompt_ref`；模块、符号或非空字符串校验失败时拒绝冻结。
7. 缓存不可变 function schema、output schema 和 Prompt 正文。

Prompt 不写入 input/output model，也不由 Registry 拼装成 system Prompt。Registry 只负责启动期解析和冻结；ToolView 提供当前可见 Prompt，ContextManager 负责最终排版。修改 `prompt.py` 后通过重启应用生效，不做运行期热重载。

### 3.4 ToolTarget

工具参数校验成功后，`resolve_targets(validated_input)` 派生零个或多个内部 `ToolTarget`：

```python
ToolTarget(
    kind="file",
    capability="read",
    scope="exact",
    value="D:/study/MiniAgent/src/app.py",  # 规范化后的受控值
)
```

文件 Target Capability 固定为 `read`、`write` 和 `delete`，互不蕴含。Target Scope 固定为 `exact` 和 `subtree`；需要递归访问的工具必须声明 `subtree`，不能先用 `exact` 获准后再遍历后代。copy 同时声明 source/read 与 destination/write；move 或 rename 同时声明 source/delete 与 destination/write。多资源操作必须声明全部目标；无资源的纯计算工具可以显式返回空 targets。

相对路径以 Current Session 启动时冻结的 Workspace Root 为基准，绝对路径也允许进入目标解析。解析会消解 `..`、Windows 大小写差异、符号链接和 junction。已存在目标使用真实位置；尚不存在的目标向上寻找最近的已存在父目录，解析该父目录后再拼接剩余部分。Workspace Root 内的链接若指向外部，目标按解析后的外部位置处理。

handler 必须通过 `ExecutionContext.targets` 使用已经授权的资源目标，不能从原始 input 再建立资源定位路径或绕过 Target Authorization。首版只在授权阶段解析一次目标，不防御授权后符号链接或 junction 被替换造成的检查与使用时间差；这是明确接受的限制。

### 3.5 Target Authorization

Target Authorization 是 workspace 边界判断与交互式 permission 的唯一执行模块。它接收一个 ToolUse 已经完整解析出的全部 ToolTarget，并按以下顺序裁决：

1. 目标位于冻结的 Workspace Root 内时自动允许，包括 read、write 和 delete；危险操作确认不属于本 permission 模型。
2. 越界目标若命中 Current Session 的 Session Permission Grant，则允许。缓存键包含规范化目标、Target Capability 和 Target Scope；`exact` 不覆盖后代，`subtree` 只覆盖同能力的目录树。
3. 越界目标若命中当前 AgentRun 的拒绝缓存，则直接拒绝，避免模型重复请求相同能力。
4. 其余越界目标形成一个 Permission Request。一个 ToolUse 是最小裁决单位：该调用所有尚未授权的越界目标一次完整展示并整体拒绝或允许，不能只执行其中一部分。

Permission Decision 只有三种：

- `deny`：当前 ToolUse 不执行，并在当前 AgentRun 内按目标、能力和范围缓存拒绝；
- `allow_once`：只允许当前 ToolUse，包括该 ToolUse 自身的 transient execution retry；
- `allow_session`：允许当前 ToolUse，并把各项目标、能力和范围分别写入当前 SessionEngine 的内存授权缓存。

参数修正使用新的 `tool_use_id`，属于新的 ToolUse，不能复用 `allow_once`。用户在普通消息中明确要求访问外部路径也不构成 Permission Decision。Session Permission Grant 跨同一 Current Session 的多个 AgentRun 生效，但切换 Session、`/clear`、应用退出或重新打开历史 Session 后失效；它不写 Message Journal、Trace 或配置文件。

Target Authorization 为规范化路径而执行的最小 `exists`、`stat`、符号链接或 junction 查询属于可信内部操作，不需要先取得 permission，其结果只能用于目标解析和授权提示。工具把这些查询作为业务能力使用时仍受正常授权规则约束。

Permission Request 没有自动超时，等待时间不计入工具执行 timeout。关闭或 Escape 等同 `deny`；AgentRun 取消、Session 切换、清空或应用退出则取消待定请求，不伪造用户拒绝。恢复历史 Session 时既不恢复待定请求，也不恢复许可缓存。

### 3.6 ExecutionTraits

目标解析后由无副作用、确定性的分类器计算：

```python
ExecutionTraits(concurrency_safe: bool)
```

只有明确无副作用的只读调用才能为 `True`。分类器必须是只读取已验证 input 和规范化 targets 的确定性纯函数，不访问文件系统、网络、Session 状态或全局配置，也不重复权限判断。分类器异常时保守判为 `False`。不引入资源锁或读写集合；副作用工具一律串行。

### 3.7 ExecutionContext

`ExecutionContext` 显式承载执行期能力，例如：

- session/run/tool_use 标识；
- workspace root；
- cancellation signal；
- trace sink；
- ArtifactStore；
- 当前工具的超时和已经授权的 targets。

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

`output_schema` 是内部成功结果契约，不发送给 Model Provider。它由 output model 派生，用于启动校验、handler 返回值校验、结构化持久化和 UI/SDK 消费；Provider 仍只接收 function input schema。

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
9. **输出校验**：确认 handler 返回声明的 output model；契约不匹配直接报告内部 `ToolProtocolError`。
10. **结果治理**：规范序列化完整 `ToolOutput`，应用 `ResultPolicy`，必要时由 ArtifactStore 外置。
11. **结果封装**：生成与 `tool_use_id`、工具名绑定的终态 `ToolResult`；模型可见内容只使用 `content` 或统一的大结果预览。
12. **事件提交**：结果提交成功后，按 assistant 原始调用顺序加入 `message.jsonl`/Working Context。

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

`code` 和 `stage` 使用稳定英文标识；提供给模型的 message、字段错误和 Tool Recovery 使用英文。运行时不得直接把原始 Python 异常、环境值、provider secret、未脱敏路径或堆栈写入这些字段。UI 如需本地化，应根据结构化 code/stage 单独投影，不复用模型提示作为展示协议。

JSON 无法解析或顶层类型错误，只要有合法 `tool_use_id`，返回 `malformed_arguments` 并允许一次修正。重复 `tool_use_id`、缺少调用 ID、消息关联冲突等属于内部协议错误，直接抛出并终止批次。

### 6.2 参数修正

每个原始 `tool_use_id` 最多允许一次参数修正。校验失败结果中包含原调用 ID；下一次调用用 `correction_of_tool_use_id` 显式关联，并获得新的 `tool_use_id`。修正调用必须使用同一工具，且直接指向原始失败调用，不能形成 `A <- B <- C` 链。第二次失败返回 `correction_not_allowed` 终态失败，不执行工具。

### 6.3 执行重试

执行尝试总数最多为 3 次，即首次执行加最多 2 次重试。只有工具或执行器分类为 `transient` 且重试策略允许时才重试。超时或取消若无法确认没有副作用，返回 `outcome_unknown`，不自动重放。每次尝试记录 `attempt=1..3`。

执行 attempt 与 AgentRun 中的最终失败次数是两套机制。一次 ToolUse 即使内部执行三次 attempt 后失败，也只产生一个最终失败 ToolResult，并只计一次 AgentRun 工具失败。

### 6.4 AgentRun 内工具失败与可用性

每次 ModelCall 前，工具模块根据冻结 Registry view 和当前 AgentRun 已提交的 ToolUse/ToolResult 重新投影 ToolView，不保存额外的可变失败计数：

1. 只考察触发当前 AgentRun 的 UserMessage 之后已经提交的工具交互；
2. 按 AssistantMessage 中的原始 ToolUse 顺序计算每个 `tool_name` 的连续最终失败；并发完成顺序不参与计算；
3. 成功结果清零该工具的连续失败；不同工具互不影响；
4. 第一次或第二次最终失败后，工具在紧接着的一次 ModelCall 中仍可见，并得到一次性 Tool Recovery；提示只包含英文 `code`、`stage`、经安全处理的原因、字段错误和恢复建议，不包含失败次数、原始 arguments、敏感路径、环境值或堆栈；
5. Tool Recovery 只出现一轮；模型未重试时不继续重复；再次失败时，下一轮使用最新失败重新生成；
6. 第三次连续最终失败后，从下一次 ModelCall 的 ToolView 中完全移除该工具，不再提供 schema、索引、静态 Prompt、Recovery 或具名禁用提示；
7. 新 AgentRun 从完整冻结 Registry view 重新开始，不继承前一个 AgentRun 的失败状态。

计入阈值的失败必须属于已注册工具并已经产生终态失败 ToolResult，包括参数错误、修正拒绝、目标策略拒绝、业务失败、transient retry 耗尽和结果确定的 timeout。`unknown_tool`、用户或 Session 取消、`outcome_unknown`、尚未启动的调用、Journal 提交失败和内部协议错误不计入阈值。

`correction_of_tool_use_id` 继续负责一次显式参数修正。普通调用和修正调用的最终失败都计入相同连续失败序列；修正仍不能跨工具、重复使用或形成链。

## 7. 批次调度与取消

同一 AssistantMessage 的 ToolUse 构成一个逻辑批次。Executor 将调用分成最大连续安全段：

```text
safe A, safe B  -> 并发等待
unsafe C         -> 等待前段后串行执行
safe D           -> 开始下一安全段
```

并发只发生在连续的 `concurrency_safe=True` 调用中，不能跨越副作用工具。结果收齐后，按原始 ToolUse 顺序返回给 AgentLoop；并发完成顺序不改变下一轮 Model Context 的逻辑顺序。

同一批次已经启动或排定的调用不会因为批次内第三次同名工具失败而中途取消。批次全部终态结果按原始顺序提交后，下一次 ModelCall 才重新投影 ToolView。

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
    ├── message.jsonl
    ├── trace.jsonl
    └── tool_result/<tool_use_id>/
        ├── result.json
        └── metadata.json
```

`message.jsonl` 是可恢复的消息聊天记录；`trace.jsonl` 按真实事件顺序完整记录调用开始、尝试、重试、结束和错误；`tool_result` 只保存最终结果，不保存每次 retry 的中间输出。

结果预算按完整 `ToolOutput` 的规范 JSON UTF-8 字节数计算，不能通过把大数据放入 `metadata` 或 `data` 绕过治理。默认结果超过 50 KB 时外置；`grep` 工具超过 20 KB 时外置。阈值由 `ToolSpec` 的结果策略决定，工具只能收紧而不能突破系统硬上限。外置结果的模型可见内容必须包含：

- 原输出已被截断并外置的说明；
- 确定性预览；
- 受控结果文件路径或引用；
- 使用 `read_file` 的读取方式。

未超过阈值的成功结果把完整结构化 `output` 写入 ToolResultPart，artifact 为空。超过阈值时，ArtifactStore 将完整 ToolOutput 写为规范 `result.json`；ToolResultPart 的 `output` 为空，artifact 保存受控引用，`content` 保存统一预览和读取说明。两种表示互斥，不能在 Journal 中同时内联和外置同一完整结果。

结果文件位于 workspace 内的 `.mini/sessions/...`，因此复用已有 workspace 路径策略。Executor/ArtifactStore 生成路径，工具不能返回任意本地路径。本设计不在此规定具体读取工具的分页或二次外置策略。

并发调用的 `trace.jsonl` 按真实完成事件追加；批次结果在 `message.jsonl` 中按原始 ToolUse 顺序提交。只有 Session 接受结果后，结果才进入 Working Context。

ToolResultPart 至少持久化：

```python
ToolResultPart(
    tool_use_id: str,
    assistant_message_id: UUID,
    tool_name: str,
    content: str,
    output: Mapping[str, object] | None,
    failure: ToolFailure | None,
    artifact: ArtifactRef | None,
    is_error: bool,
    outcome_unknown: bool,
)
```

成功的小结果必须有 `output`；成功的大结果必须有 `artifact`；预期失败必须有 `failure`。模型和 Provider 投影只读取 `content` 及必要的协议状态，不能自动序列化 `output`、failure、artifact 或其他内部字段。结构化结果不得包含 provider secrets、环境值、原始工具参数或不应持久化的 Session 内容。

## 9. 核心不变量

1. Registry 冻结后不可变，工具名全局唯一；可用工具来自 composition root 的显式名称集合。
2. 每个 function schema 和 output schema 分别与冻结时的 input/output model 一致，schema 或 Prompt 引用不兼容阻止启动。
3. handler 永远收到严格验证后的 Pydantic input model、显式 `ExecutionContext` 和已经批准的 targets。
4. handler 成功时必须返回声明的 output model；失败只能走规定异常边界。
5. 未通过目标策略的调用不会执行 handler，handler 不从原始输入绕过受控 targets。
6. 参数修正每个原始调用最多一次，不能链式绕过。
7. 执行尝试最多三次，非 transient 或不确定副作用不自动重试；一次 ToolUse 最多产生一个最终失败计数。
8. 副作用工具不并发，安全段不跨越串行屏障。
9. ToolResult 始终显式绑定 `tool_use_id` 和工具名，成功 output 与 failure 互斥。
10. 未被 Session 接受的结果不得进入 Working Context，也不得影响下一次 ToolView。
11. `message.jsonl` 的工具结果按 assistant 调用顺序排列，`trace.jsonl` 保留真实事件顺序。
12. 同一 AgentRun 中工具连续最终失败三次后，从下一次 ToolView 完全移除；新 AgentRun 不继承该状态。
13. ContextManager 和 ModelAdapter 在同一 ModelCall 中消费同一个 ToolView 快照。
14. 结果预算覆盖完整 ToolOutput；外置路径由 ArtifactStore 生成，并位于当前 Session 的受控目录。
15. 内部 ID 冲突、消息关联冲突、输出模型不匹配和 Registry 契约破坏不得伪装成普通 ToolFailure。

## 10. 建议测试场景

- 显式工具名无法加载、重复工具名、无效 PromptRef、缺少 `extra="forbid"`、不支持 schema 关键字导致冻结失败；
- input/output model 分别派生稳定 schema，handler 返回错误 output model 时报告内部契约错误；
- alias 字段的缺失、多余字段和严格类型错误；
- unknown tool、malformed arguments、重复 ID 和消息关联冲突；
- 一次参数修正成功、二次修正被拒绝、跨工具或链式修正被拒绝；
- transient 执行失败重试至成功、三次耗尽、非 transient 不重试；
- 超时后结果未知且不重放；
- 连续安全段并发，副作用工具形成屏障，结果按原始顺序提交；
- 取消时不启动新工具、所有已启动任务进入终态；
- 默认 50 KB 与 grep 20 KB 外置阈值、确定性预览和受控 artifact 引用；
- 完整 ToolOutput 参与预算，metadata/data 不能绕过外置阈值，inline output 与 artifact 互斥；
- `message.jsonl` 与 `trace.jsonl` 分别验证逻辑顺序和真实事件顺序；
- 第一次或第二次最终失败只在紧接的一轮产生英文 Tool Recovery，且不显示失败次数；
- 第三次连续最终失败后 schema、工具索引、Prompt 和 Recovery 同时消失，成功会清零连续失败；
- 同批次同名工具按原始 ToolUse 顺序更新失败状态，新 AgentRun 恢复完整工具集合；
- ContextManager 与 ModelAdapter 对每次 ModelCall 使用同一个刷新后的 ToolView；
- 进程恢复时仅恢复消息记录，不重复执行未知结果的工具。

## 11. 当前实现差距

- 当前 Registry 直接接收 ToolSpec 集合，尚未按 composition root 的显式工具名和同名包约定加载；
- 当前 ToolSpec 没有 output model/output schema，handler 返回裸字符串，Executor 尚未校验结构化 ToolOutput；
- 当前 prompt_ref 未在冻结阶段解析，ToolView 仍使用占位 Prompt；
- 当前 ToolResultPart 未持久化 tool_name、结构化 output、failure 和 artifact 的互斥终态；
- 当前结果预算只计算 handler 返回文本，ArtifactStore 主要写 `result.txt`，尚未治理完整 ToolOutput JSON；
- 当前 ToolView 定义在上下文模块，AgentLoop 虽构造 ToolView，但 ModelAdapter 仍接收静态工具集合；
- 当前没有从 AgentRun 已提交结果投影一次性 Tool Recovery 和连续三次失败后的工具移除。

这些是后续实现工作，不允许新增工具继续依赖测试型旁路或把目标契约降级为现状。
