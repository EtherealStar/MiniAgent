# MiniAgent 工具设计指南

本文规定在 MiniAgent 现有工具框架中新增或修改内置工具时必须遵守的设计约定。它是工具作者指南，不是工具执行框架的替代设计，也不是某个具体工具的实现计划。

框架语义以 `tool-registry-and-execution.md` 为准；上下文组装和 AgentRun 控制分别以 `context-management.md` 与 `main-loop.md` 为准。若一个工具无法通过本文列出的 `ToolSpec`、input/output model、targets、classifier、handler 和 policy 表达，应先修订相应设计文档，而不是在工具内部绕过边界。

## 1. 适用范围

本文只覆盖在现有框架中新增或修改内置工具，不覆盖：

- ToolRegistry、ToolExecutor、ArtifactStore 或 ToolView 算法的框架变更；
- 新权限模型、Hook 能力、并发调度算法或 Provider 协议；
- MCP、远程工具或第三方插件接入；
- AgentLoop、SessionEngine 或 ContextManager 的职责变更；
- 某个具体工具的实施步骤。

已确定的具体内置工具契约位于 `tools/`：

- [`glob`](tools/glob.md)：workspace 目录树中的路径发现；
- [`grep`](tools/grep.md)：workspace UTF-8 文本的逐行内容搜索；
- [`read_file`](tools/read-file.md)：已知 UTF-8 文件与受控引用的分页读取；
- [`write_file`](tools/write-file.md)：带冲突保护的 UTF-8 整文件写入；
- [`read_docs`](tools/read-docs.md)：使用 MinerU 生成受控 Markdown；
- [`todo_write`](tools/todo-write.md)：Current Session 的进程内任务状态；
- [`calculator`](tools/calculator.md)：受限、确定性的数值表达式求值；
- [`web_search`](tools/web-search.md)：使用 Tavily 的普通 Web 搜索。

具体工具文档可以收紧超时、结果预算、输入范围和重试策略，但不能放宽本文或 `tool-registry-and-execution.md` 的框架边界。

## 2. 目录与注册

每个内置工具使用同名目录：

```text
miniagent/tools/<tool_name>/
  __init__.py
  tool.py
  prompt.py
```

- `tool.py` 定义 input/output model、target resolver、classifier、async handler 和策略所需内容；
- `prompt.py` 导出英文 `PROMPT`；
- `__init__.py` 保持窄接口，不导出工具内部实现细节；
- composition root 向 ToolRegistry 显式提供可用工具名；
- Registry 按 `miniagent.tools.<tool_name>` 的固定包约定加载和冻结工具；
- 不扫描目录自动注册工具，未完成或测试工具不能因目录存在而变成模型能力。

内置工具名使用 `snake_case`，并在目录名、ToolSpec name、Provider-visible function name、Prompt 引用和测试中保持一致，例如 `read_file`、`edit_file` 和 `grep`。这是项目工具约定，不要求通用 Registry 收紧其底层名称正则。

## 3. ToolSpec 是注册聚合

一个工具的 ToolSpec 至少关联：

```python
ToolSpec(
    name=...,
    description=...,
    input_model=...,
    output_model=...,
    prompt_ref=...,
    resolve_targets=...,
    classify=...,
    handler=...,
    retry_policy=...,
    timeout_seconds=...,
    result_policy=...,
)
```

工具作者不手写 Provider function schema 或内部 output schema：

```text
input_model  -> Registry 派生 function input schema
output_model -> Registry 派生 internal output schema
```

input/output model 都必须是严格 Pydantic 模型并设置 `extra="forbid"`。输入 schema 中的 alias 是唯一外部字段名；不要同时接受 Python 字段名。

`description` 是进入 Provider schema 的一句简短英文，只说明能力，不承载复杂规则。复杂选择边界和恢复方式写入 `prompt.py`。

## 4. Prompt

`prompt.py` 使用固定符号：

```python
PROMPT = """Purpose:
<One or two sentences explaining what this tool does and when it is the right choice.>

Use when:
- <Concrete trigger for choosing this tool.>
- <Another common trigger, if useful.>

Prefer instead:
- Use `<other_tool>` when <clear boundary>.
- <Omit this section if there is no meaningful alternative.>

Rules:
- <Important calling rule that affects correctness.>
- <Important precondition, default, limitation, or runtime behavior.>
- <Do not restate schema fields unless the behavior is non-obvious.>

Returns:
- <What the model should expect in the result.>
- <Mention pagination, truncation, line numbers, task ids, or structured errors if relevant.>

If it fails:
- <How the model should recover: read first, narrow search, adjust input, ask the user, etc.>
"""
```

允许省略没有实际内容的 `Prefer instead` 或 `If it fails`，其余章节保持顺序稳定。简单工具可使用：

```python
PROMPT = """Purpose:
<What this tool does.>

Use when:
- <When to call it.>

Rules:
- <Important usage rule.>

Returns:
- <Result shape.>
"""
```

Prompt 规则：

- 使用英文，与 Provider-visible description 和工具错误保持一致；
- 不写统一工具标题，最终标题由 ToolView/ContextManager 排版；
- 不重复 schema 已经清楚表达的类型和 required 字段；
- 只写该工具独有的选择边界、前置条件、限制、结果解释和失败恢复；
- 不把权限或安全边界寄托给 Prompt；target policy 和 Executor 才是执行事实源；
- 不承诺 handler、target policy 或结果治理无法保证的能力；
- 不写产品文案、实现细节、长篇示例或跨工具的通用偏好；
- 不动态加入某次调用错误。一次性 Tool Recovery 由 ToolView 从已提交 ToolResult 投影。

`prompt_ref` 使用 `python.module:SYMBOL`，例如：

```python
prompt_ref="miniagent.tools.read_file.prompt:PROMPT"
```

Registry 在冻结阶段解析引用并校验非空字符串。正式注册的内置工具不得省略 Prompt 或使用占位文本。修改 Prompt 后通过重启应用生效，不做运行期热重载。

## 5. Input Model

input model 只表达工具业务参数，不包含运行时能力或会话状态。遵守以下规则：

- 使用严格 Pydantic 类型，禁止多余字段；
- 外部 alias 稳定且含义明确；
- 用 schema 表达结构约束，用 Pydantic validator 表达字段间和值级约束；
- 不在 handler 中重新接受或宽松转换原始 JSON；
- 框架字段 `correction_of_tool_use_id` 不属于业务 input model，由 Executor 在验证前剥离；
- 默认值、边界和容易误用的语义必须在字段描述或 Prompt 中说清楚，但不要重复堆叠。

## 6. Output Model

每个工具必须声明自己的 output model，并继承或遵守 ToolOutput 的统一成功形状：

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
- 具体工具应尽量用嵌套 Pydantic model 收紧 metadata/data，而不是返回无约束对象；
- output model 不包含 `is_error`、attempts、artifact、retry 或 failure；
- handler 成功时必须返回声明的 output model 实例，不返回裸字符串或任意 dict；
- output model 不匹配是内部 ToolProtocolError，不是模型可修正错误，也不得自动重试。

只有 `content` 默认进入模型上下文。metadata/data 不自动序列化给模型，但会作为结构化成功结果随 ToolResultPart 持久化，供恢复后的 UI 或 SDK 使用。不得在其中保存 provider secrets、环境值、原始工具参数或不应持久化的 Session 内容。

## 7. Targets 与受控能力

凡是触达文件、目录、命令、URL、外部服务或 Session 状态的工具，都必须从已验证 input 派生一个或多个 ToolTarget：

```python
ToolTarget(
    kind="file",
    capability="read",
    scope="exact",
    value="D:/study/MiniAgent/src/app.py",
)
```

- resolver 负责从业务 input 声明完整目标、Target Capability（read/write/delete）和 Target Scope（exact/subtree）；路径规范化、Workspace Root、Protected Workspace Subtree 与 permission 统一交给 Target Authorization；
- 多资源操作声明全部目标，例如 copy 同时声明 source/read 与 destination/write；
- move/rename 同时声明 source/delete 与 destination/write；递归访问必须声明 subtree，不能用 exact 授权后再遍历后代；
- 没有资源目标的纯计算工具显式返回空 targets；
- handler 通过 `ExecutionContext.targets` 使用已经授权的资源，不从原始路径参数重新建立旁路；
- handler 不自行实现另一套权限、路径或 guard 规则。
- Protected Workspace Subtree 由 Target Authorization 统一裁决；递归工具从普通祖先扫描时必须按具体工具契约跳过受保护子树，不能借祖先的 subtree target 绕过显式许可；
- Current Session 的内部状态使用 `session_state/<capability>/exact`，session identity 由 Executor 绑定，不能成为模型业务参数；
- 固定外部只读服务使用 `external_service/read/exact` target，并由 composition root 显式配置和启用；向外部服务上传本地内容必须声明 `external_service/write/exact` 并取得 Permission Decision；
- ArtifactRef 与 DocumentRef 只能由受控 store 生成和登记；精确受控引用可以获得 Protected Workspace Subtree 读取豁免，模型手写的相似路径不能；
- 模型参数不能提供或扩大固定服务 host、session identity 或受控引用范围。

## 8. Classification 与并发

`classify(validated_input, targets)` 只产生 ExecutionTraits，并遵守：

- 它是确定性、无副作用的纯函数；
- 不访问文件系统、网络、Session 或全局配置；
- 不重复 target policy 的权限判断；
- 只有明确无副作用的只读调用可以设置 `concurrency_safe=True`；
- 写入、删除、移动、命令执行、外部调用和状态变更一律串行；
- 无法证明安全时返回 False；classifier 异常由 Executor 保守降级为串行。

## 9. Handler

handler 统一为异步函数：

```python
async def handler(args: ToolInput, context: ExecutionContext) -> ToolOutput:
    ...
```

- 只接收严格验证后的业务 input 和显式 ExecutionContext；
- 使用 context 提供的 targets、取消信号、Trace 和运行时能力；
- 不读取全局 Session 状态；
- 同步阻塞 API 由工具内部显式放入 `asyncio.to_thread()`；
- 必须协作响应取消，不能让后台任务脱离 AgentRun；
- 成功返回 output model；预期业务失败抛出规定的 ToolExecutionError；
- 不自行构造 ToolResult、ToolResultPart、ArtifactRef 或模型错误信封；
- 不自行持久化大结果或拼装统一预览。

## 10. 失败、修正与重试

工具错误面向模型的 code、stage、message、字段错误和恢复建议使用英文。不要把 Python 异常、堆栈、环境值、secret 或未经处理的敏感路径直接返回给模型。

handler 抛出的 `ToolExecutionError` 必须使用 `tool-registry-and-execution.md` 定义的封闭 `ExecutionErrorCode`，并提供安全英文 `safe_message`。工具不得自创顶层执行错误码；具体原因通过 message 表达。参数验证、Target Authorization、取消、outcome unknown 和内部协议错误继续由 Executor 各自的边界负责，不能伪装成 handler 执行失败。

两种重试不能混淆：

```text
单次 ToolUse：transient execution attempt 最多 3 次
单次 AgentRun：同一工具连续 3 个最终失败后从下一轮 ToolView 移除
```

- RetryPolicy 默认一次 attempt；只有 transient 且可确认安全重放时才增加 attempt；
- 参数、目标和业务拒绝不做 Executor 内部 retry；
- outcome unknown 或可能已经产生副作用时不重放；
- 一次 ToolUse 的多次 attempt 只产生一个最终失败计数；
- correction 是新的 ToolUse，继续遵守一次显式修正、同工具、不可链式的规则；
- 第一次或第二次最终失败只在紧接的一次 ModelCall 中产生 Tool Recovery，不显示失败次数；
- 第三次连续最终失败后，下一轮不再提供该工具的 schema、索引、静态 Prompt、Recovery 或具名禁用提示；
- 成功清零对应工具的连续失败；新 AgentRun 使用完整工具集合重新开始；
- 取消、outcome unknown、unknown tool、未启动调用和内部协议错误不计入阈值。
- `permission_denied` 表达用户没有授权当前调用，也不计入阈值；工具不得通过参数修正或内部 retry 绕过新的 Permission Decision。

## 11. Result Policy 与持久化

ResultPolicy 约束完整 ToolOutput 的规范 JSON UTF-8 大小，包括 content、metadata 和 data。工具不能通过结构化字段绕过结果预算。每个内置工具显式声明或继承：

```python
ResultPolicy(
    max_inline_bytes=50 * 1024,
    overflow_behavior="externalize",  # externalize | error
    max_model_tokens=None,
)
```

- 未显式配置时使用 50 KiB 与 `externalize`；具体工具可以声明自己的字节阈值和溢出行为；
- `externalize` 把完整 ToolOutput 交给 ArtifactStore；`error` 返回 `RESOURCE_EXHAUSTED` 且不得创建 ArtifactRef；
- `max_model_tokens` 若非空，只计算模型可见 content，并使用 AgentRun 冻结的 tokenizer；
- 字节或 Token 达到工具上限时按 overflow_behavior 处理，不能由 handler 返回半个成功结果；
- 选择 `error` 的读取工具必须用输入范围和测试证明所有成功输出均可内联。

- 未超过阈值：完整 output 随 ToolResultPart 写入 Message Journal；
- 超过阈值：ArtifactStore 写入完整 `result.json`，ToolResultPart 只保存统一 content 预览和 ArtifactRef；
- inline output 与 artifact 互斥；
- handler 不选择 artifact 路径，不直接写 Session 结果目录；
- Message Journal 按原始 ToolUse 顺序保存终态结果，Trace 保留真实完成顺序；
- 本指南不规定具体读取工具的分页或二次外置策略。

## 12. 新工具最低测试

每个新增内置工具至少覆盖：

1. 显式工具名可以按同名包约定加载，错误模块或 PromptRef 在 Registry 冻结时失败；
2. input/output model 都禁止额外字段，派生 schema 与 alias 正确；
3. Provider function schema 包含正确 name、英文 description 和严格 input schema；
4. 英文 Prompt 使用规定模板，只在工具可见时与 schema 同时出现；
5. malformed arguments、缺失字段、多余字段和严格类型错误不调用 handler；
6. resolver 产生 capability/scope 完整的 targets；Target Authorization 拒绝时不触碰资源，exact/subtree 与 read/write/delete 不会相互扩大；
7. handler 只使用批准 targets，classifier 对读写调用给出正确并发特征；
8. handler 返回错误 output model 时报告内部契约错误；
9. 成功结果只把 content 投影给模型，metadata/data 结构化持久化；
10. 预期异常转换为英文结构化 ToolFailure，未知实现异常不泄露敏感诊断；
11. transient attempt、非 transient、不确定副作用和取消分别符合重试规则；
12. 完整 ToolOutput 超过预算时外置，inline output 与 artifact 互斥；
13. 连续安全调用并发，副作用工具形成串行屏障，结果按原始顺序提交；
14. 第一次或第二次最终失败只在下一轮产生一次 Recovery，且不显示次数；
15. 第三次连续最终失败后工具从整个 ToolView 消失，成功清零，新 AgentRun 恢复；
16. ContextManager 与 ModelAdapter 在同一 ModelCall 中消费同一个刷新后的 ToolView；
17. 文件系统测试只使用 pytest temporary directories，不依赖真实网络、凭据或用户 Session 数据。
18. 越界多目标按单个 ToolUse 整体裁决，handler 只收到全部获准的 targets；permission 等待不消耗工具 timeout，拒绝不触发连续失败移除。
19. Protected Workspace Subtree、硬排除目录和固定 external_service target 分别遵守其授权语义，祖先 subtree 或业务参数不能形成旁路。
20. ToolExecutionError 只使用框架级 ExecutionErrorCode，safe_message 不泄露实现异常或 secret，transient 只有在 RetryPolicy 允许时才重放。
21. `overflow_behavior=error` 在字节或 Token 边界生成 RESOURCE_EXHAUSTED，不保存部分 output 或 ArtifactRef。
22. Current Session 的 ArtifactRef、DocumentRef 与 session_state exact target 能通过受控目录裁决，伪造引用或跨 Session 访问被拒绝。
23. external_service/write 本地内容外传在 handler 前取得 Permission Decision，拒绝时不读取业务正文或发送网络请求。

开发时先运行聚焦测试。完成代码变更前按仓库约定运行：

```text
uv run python -m compileall miniagent tests main.py
uv run python -m pytest -q
```

## 13. 提交前检查

- 工具是否能完全通过现有 ToolSpec 和执行边界表达？
- 目录、工具名、Provider name 和测试是否使用同一个 snake_case 名称？
- description 与 Prompt 是否为英文，Prompt 是否只描述模型使用决策？
- input/output schema 是否都从严格 Pydantic model 派生？
- handler 是否只返回成功 ToolOutput，并通过授权 targets 使用资源？
- 每个目标是否声明了最小 Target Capability 和准确的 exact/subtree 范围，复合操作是否声明全部目标？
- 受保护子树、硬排除资源和固定外部服务是否由统一 Target Authorization 表达，而不是藏在 Prompt 或 handler 中？
- 并发安全是否有明确无副作用依据？
- transient retry 是否确认可安全重放？
- content、metadata 和 data 是否都不泄露敏感信息？
- 大结果是否交给 ResultPolicy 和 ArtifactStore？
- 禁止外置的工具是否声明 `overflow_behavior=error`、自限流并覆盖边界测试？
- 测试是否覆盖 schema、Prompt、targets、输出、失败、并发、结果治理和动态 ToolView？
