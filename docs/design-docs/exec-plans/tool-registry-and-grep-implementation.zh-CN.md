# 实现工具注册表、执行器与本地 grep 工具

本 ExecPlan 是一份持续维护的中文执行计划，必须遵守仓库根目录 `PLANS.md` 的格式和维护要求。实现过程中必须持续更新 `Progress`、`Surprises & Discoveries`、`Decision Log` 和 `Outcomes & Retrospective`；任何接口或范围变化都要同步修改全文，并在文末追加变更说明。

## Purpose / Big Picture

MiniAgent 目前没有可注册或执行的工具。完成本计划后，composition root（程序启动时集中组装依赖的位置）可以显式注册并冻结工具集合，把严格的 OpenAI-compatible function schema 交给模型，再把模型返回的工具调用交给统一执行器。首个也是本计划唯一交付的生产工具是 `grep`：它在当前 workspace 内递归搜索本地文本文件，返回稳定排序的 `路径:行号:内容` 匹配项，且不能借助绝对路径、`..` 或符号链接读取 workspace 外部文件。

用户可以用不需要模型 API 密钥的演示和测试看到完整行为：合法 `grep` 调用得到带原 `tool_use_id` 的结果；非法参数成为模型可见的结构化失败；一次显式修正可以重新执行；多个只读调用可以并发但返回顺序仍与请求顺序一致；超过 20 KB 的结果被保存到当前 Session 的受控 artifact 目录并只在结果中显示确定性预览。

## Progress

- [x] （2026-07-22 +08:00）完整阅读 `AGENTS.md`、`PLANS.md`、`docs/design-docs/tool-registry-and-execution.md`、`docs/design-docs/main-loop.md`、`docs/plans/main-loop-implementation.zh-CN.md`、`CONTEXT.md`、`pyproject.toml` 和 `main.py`，确认计划格式、模块边界、仓库现状与测试入口。
- [x] （2026-07-22 +08:00）建立工具领域类型、严格参数模型和 OpenAI-compatible schema 冻结机制；5 项注册表聚焦测试证明 alias、递归拒绝、同步 handler 拒绝和防御性 schema 副本。
- [x] （2026-07-22 +08:00）实现 workspace 目标解析、权限检查、跨批次一次修正状态和单调用执行流水线。
- [x] （2026-07-22 +08:00）实现最多三次 transient 重试、超时、协作式取消和连续安全段批次调度。
- [x] （2026-07-22 +08:00）实现受控 `FileArtifactStore`、50 KB 系统硬上限、工具结果阈值和真实顺序 trace 事件。
- [x] （2026-07-22 +08:00）实现并注册唯一生产工具 `grep`，完成本地文本搜索、跳过统计、确定性输出和 20 KB 外置。
- [x] （2026-07-22 +08:00）接入现有主循环工具端口和无密钥演示，补齐单元、集成和 Windows 路径安全测试。
- [x] （2026-07-22 +08:00）运行全部验收命令：工具聚焦测试 29 passed，全仓测试 69 passed，`compileall` 与 `main.py` 演示成功。

## Surprises & Discoveries

- Observation：仓库当前只有 `main.py` 占位入口，没有 `miniagent/` 包、`tests/` 目录或 Python 运行时依赖。
  Evidence：`main.py` 只输出 `Hello from miniagent!`，`pyproject.toml` 的 `dependencies` 为空。
- Observation：`docs/plans/main-loop-implementation.zh-CN.md` 已规划 `ToolExecutor` 端口，但主循环尚未实现，且 Git 仓库尚无首个提交。
  Evidence：现有主循环计划的 Progress 项均未实施，`git status --short` 显示当前文件均未跟踪。
- Observation：设计要求大 `grep` 结果通过 `read_file(offset, limit)` 分页读取，但本需求明确只实现 `grep` 工具。
  Evidence：`docs/design-docs/tool-registry-and-execution.md` 第 8 节指定 20 KB 外置阈值及 `read_file` 读取方式，而用户将生产工具范围限定为 `grep`。
- Observation：开始实施时主循环计划已经完成，仓库已有 `miniagent/domain.py` 中面向模型端的轻量 `ToolSpec` 和 `ToolResult`，与本计划最初调研时的空仓库状态不同。
  Evidence：实施前 `uv run python -m pytest -q` 已覆盖主循环和供应商；最终工具执行器直接实现 `miniagent/ports.py` 的 `submit_batch` 形态，并把全仓测试扩展到 69 项。
- Observation：`asyncio.to_thread()` 的外层任务被取消后不会强制终止底层线程，直接取消可能使 grep 扫描脱离 AgentRun。
  Evidence：`grep_handler` 使用 `asyncio.shield()` 保留扫描任务，执行器先触发 attempt 级协作式取消，handler 等待扫描线程观察信号并退出后再传播取消。
- Observation：修正资格必须在批次开始时取快照，否则同一批次后面的调用能引用前面刚产生的参数失败，不符合“上一轮修正”。
  Evidence：`test_correction_cannot_reference_failure_from_same_batch` 证明同批引用返回 `correction_not_allowed`，下一批首次修正仍可成功。

## Decision Log

- Decision：实现通用 `ToolRegistry` 和 `ToolExecutor` 机制，但生产注册表中只提供 `grep`；测试中的假 handler 只用于覆盖执行器分支，不作为可用工具发布。
  Rationale：这样既实现设计要求的可扩展机制，又严格遵守“只实现 grep 工具”的功能范围。
  Date/Author：2026-07-22 / Codex
- Decision：使用 Pydantic 2 的严格模型和 JSON Schema 作为参数校验真相源，增加 `pydantic>=2,<3` 运行时依赖；测试使用 `pytest`，异步用例通过 `asyncio.run()` 驱动，不额外引入异步测试插件。
  Rationale：参考设计明确要求 Pydantic、`extra="forbid"` 和严格校验；标准库即可驱动本计划的异步测试，减少依赖。
  Date/Author：2026-07-22 / Codex
- Decision：`grep` 使用 Python 标准库 `re` 和 `pathlib` 实现，不调用系统 `grep` 或 `rg` 进程。
  Rationale：不同操作系统上的可执行文件、参数和编码行为不一致；进程内实现更容易施加 workspace、符号链接、取消、超时和确定性排序规则。
  Date/Author：2026-07-22 / Codex
- Decision：大结果仍按 20 KB 阈值写入 `.mini/sessions/<session_id>/tool_result/<tool_use_id>/`，返回预览和受控引用，但本计划不实现 `read_file`。
  Rationale：保留设计要求的持久化格式和大小边界，同时不越过用户限定的工具范围。当前阶段宿主和测试可检查完整 artifact；模型只能看到预览，此限制必须在最终回顾中保留，直到后续实现 `read_file`。
  Date/Author：2026-07-22 / Codex
- Decision：工具层不写 `message.jsonl`，只返回按调用顺序排列的终态结果；结果是否进入 Transcript 和 Working Context 仍由 `SessionEngine` 接受事件后决定。
  Rationale：这保持 `docs/design-docs/main-loop.md` 规定的唯一会话写入边界，避免 `ToolExecutor` 与主循环竞争历史所有权。
  Date/Author：2026-07-22 / Codex
- Decision：保留 `miniagent/domain.py` 已有的轻量模型端 `ToolSpec`，完整注册定义使用 `miniagent/tools/models.py` 的 `ToolSpec`；二者通过 `name` 与 `function_schema` 的结构契约兼容，不复制 Registry 或执行逻辑。
  Rationale：直接改变已落地主循环和供应商使用的二参数构造器会破坏现有调用方；实际 `AgentLoop` 和 `OpenAICompatibleModelAdapter` 只读取两个公共字段，集成测试已证明冻结 spec 可直接传入模型与循环。
  Date/Author：2026-07-22 / Codex
- Decision：修正只能引用执行器此前批次记录的可修正失败；同一批次引用、跨工具、二次使用和引用修正调用都拒绝。
  Rationale：这把设计中的“上一轮”落实为清晰的批次边界，并阻止批内顺序影响修正资格。
  Date/Author：2026-07-22 / Codex

## Outcomes & Retrospective

计划已完成。默认注册表冻结后恰好包含 `grep`，生成 `strict: true` schema，所有 object 禁止额外字段，alias 是唯一外部名称，框架修正字段必填且不会进入业务模型。路径策略拒绝绝对路径、`..`、缺失目标和解析后越界的符号链接。测试证明了一次跨批次修正、修正拒绝、最多三次 transient 重试、非 transient 不重试、连续安全段并发、串行屏障、运行中取消和原调用顺序返回。

`grep` 对单文件和目录执行稳定的 UTF-8 正则搜索，支持大小写与单 glob，忽略 `.git`、`.mini`、NUL 二进制和不可解码文件，限制长行和匹配数，并在摘要中报告跳过与截断。超过 20 KB 的真实 grep 输出写入受控 artifact，返回字节数、SHA-256 和确定性预览。全仓最终为 69 passed，编译与无密钥演示成功。明确遗留限制仍是本计划不实现 `read_file`：模型目前只能看到外置结果的预览和引用，宿主可检查全文，未来需由独立工具计划补上分页读取。

## Context and Orientation

术语以根目录 `CONTEXT.md` 为准。`ToolSpec` 是一个工具的不可变定义；`ToolRegistry` 在程序启动阶段收集这些定义并冻结，冻结后才能生成给模型的 function schema，且不能继续注册；`ToolUse` 是模型提出的调用意图；`ToolExecutor` 把原始调用变成一个终态 `ToolResult`；`ToolTarget` 是从已校验参数解析出的受控本地目标；`ExecutionContext` 显式携带 workspace、Session、调用 ID、取消信号、trace sink 和 artifact 能力。预期的参数、权限和执行失败返回 `ToolFailure`，而重复调用 ID、结果关联冲突等内部协议破坏直接抛出异常。

实施前必须完整阅读下列仓库内参考资料，而不是只依赖本计划中的摘要：

- `AGENTS.md`：仓库级工作约束，尤其是 ExecPlan 必须遵守 `PLANS.md`。
- `PLANS.md`：计划格式、持续维护、验收、恢复和证据要求。
- `docs/design-docs/tool-registry-and-execution.md`：本实现的主规范，覆盖注册、schema、校验、目标策略、修正、重试、调度、取消和结果持久化。
- `docs/design-docs/main-loop.md`：确认 `AgentLoop` 只提交批次、`SessionEngine` 是历史唯一写入边界，以及工具结果按原始调用顺序进入上下文。
- `docs/plans/main-loop-implementation.zh-CN.md`：确认将出现的 `ToolExecutionBatch`、`ToolResult`、`Cancellation` 和 `ToolExecutor` 端口名称，避免两个计划各自定义不兼容接口。
- `CONTEXT.md`：使用项目统一术语并避免混淆 ToolUse、ToolResult、AgentRunResult、Transcript 和 Working Context。
- `设计prompt.md`：仅作为原始需求背景；与上述细化设计冲突时，以上述设计文档和本计划已记录的范围决定为准。

实施时主循环、Session、供应商和领域类型已经存在。本计划复用 `miniagent/domain.py` 的 `ToolUsePart`、`ToolExecutionBatch` 和扩展后的 `ToolResult`，并按以下结构新增工具实现。模型端保留已有轻量 `ToolSpec` 构造兼容性，Registry 的完整 spec 通过相同只读字段直接供主循环和供应商消费：

- `miniagent/tools/models.py` 保存冻结的数据对象、枚举、Protocol 和异常，包括 `ToolSpec`、`ToolTarget`、`ExecutionTraits`、`ExecutionContext`、`ToolCall`、`ToolResult`、`ToolFailure`、`RetryPolicy` 和 `ResultPolicy`。
- `miniagent/tools/schema.py` 将 Pydantic JSON Schema 转换并校验为 OpenAI-compatible strict function schema。
- `miniagent/tools/registry.py` 实现可构建、可冻结、冻结后只读的 `ToolRegistry`。
- `miniagent/tools/policy.py` 解析 workspace 内目标并执行路径、文件类型和符号链接边界检查。
- `miniagent/tools/artifacts.py` 在受控 Session 目录原子写入最终结果及 metadata。
- `miniagent/tools/executor.py` 实现单调用流水线、修正记账、重试、超时、取消和批次调度。
- `miniagent/tools/grep.py` 定义 `GrepInput`、目标解析、只读分类器、异步 handler 和 `grep_spec`。
- `miniagent/tools/__init__.py` 只导出调用方需要的稳定接口。
- `tests/tools/` 保存 schema、注册表、执行器、路径安全、artifact 和 `grep` 行为测试；测试文件使用 `tmp_path`，不搜索真实仓库内容。

`grep` 的外部参数固定为 `pattern: str`、`path: str = "."`、`include: str | None = None`、`case_sensitive: bool = True` 和 `max_matches: int = 100`。`pattern` 使用 Python 正则表达式语义；`path` 可以指向 workspace 内一个文件或目录；`include` 是相对于搜索根目录的单个 glob 过滤器；`max_matches` 限制返回匹配数并取值 1 至 1000。输出按规范化相对路径、行号排序，每项形如 `src/app.py:12:matched text`。实现对单行显示内容设置固定上限并标记截断，避免一行无限放大结果；遇到包含 NUL 字节的文件视为二进制并跳过，非 UTF-8 文件也跳过并在摘要计数，不因单个文件中止整个调用。目录搜索默认忽略 `.git` 和 `.mini`，防止扫描版本库内部数据或把先前 artifact 再次纳入结果。

## Plan of Work

### Milestone 1：建立工具契约和冻结注册表

先在 `pyproject.toml` 加入 Pydantic 2 和 pytest 配置，再创建 `miniagent/tools/models.py`。所有注册数据使用 frozen dataclass 或等价不可变类型。`ToolSpec` 的 handler 必须是异步 callable，参数是具体 `BaseModel` 子类与 `ExecutionContext`；同步底层工作由 handler 自己通过 `asyncio.to_thread()` 调度。`RetryPolicy` 限制总尝试次数不超过 3，`ResultPolicy` 同时保存工具阈值和系统硬上限。

在 `miniagent/tools/schema.py` 从 `input_model.model_json_schema(by_alias=True)` 生成业务 schema，再在顶层加入可空但必填的 `correction_of_tool_use_id`。遍历 schema 时内联本地 `$defs/$ref`，拒绝递归引用、无法无损表达的联合结构和 OpenAI strict schema 不支持的关键字；每个 object 节点补上 `additionalProperties: false`，并确保 properties 全部出现在 required 中。错误必须包含工具名和 schema 路径。外部只暴露 alias，不接受 Python 字段名。

在 `miniagent/tools/registry.py` 实现 `register()`、`freeze()`、`get()` 和 `enabled_view(names=None)`。冻结时检查非空全局唯一短名、Pydantic `extra="forbid"`、异步 handler 以及 schema 可表达性，并缓存深度不可变或每次防御性复制的 function schema。冻结前不得查询 schema，冻结后注册必须失败。`enabled_view` 只能引用已冻结定义，不能修改主注册表。

该里程碑结束时，测试应证明一个 `GrepInput` 能生成 `strict: true` schema，包装字段存在且必填，默认值仍通过可空类型表达；重复名、允许额外字段、递归/不支持 schema、同步 handler 和冻结后注册都会给出定位明确的启动错误。

### Milestone 2：实现目标策略和单调用校验流水线

在 `miniagent/tools/policy.py` 以 `ExecutionContext.workspace_root.resolve(strict=True)` 为信任根。拒绝绝对 `path`、任何语义上的父目录逃逸、解析后不在 workspace 下的路径、越界符号链接、缺失目标以及非普通文件/目录目标。Windows 上比较规范化路径时使用 `Path.relative_to()`，不要用字符串前缀。目标解析成功后生成相对 workspace 的规范化 `ToolTarget(kind="file" | "directory", operation="read", value=...)`。

在 `miniagent/tools/executor.py` 先实现一次调用的阶段顺序：验证 `tool_use_id` 和名称；解析 JSON arguments 且要求顶层 object；解析工具；检查并剥离框架字段；用冻结 schema 做顶层缺失/多余字段快速检查；调用 `model_validate(..., strict=True, by_alias=True, by_name=False)`；解析和检查目标；计算 `ExecutionTraits`；在超时与取消边界中调用 handler；最后封装带相同 `tool_use_id` 和工具名的成功或失败结果。快速检查只是更清晰的早期错误，Pydantic 仍是权威校验。字段错误路径统一使用外部 alias。

预期错误以 `ToolFailure(code, stage, message, field_errors, correctable, retryable)` 返回。未知工具、JSON 损坏、非 object、缺字段、多字段、严格类型错误、无效正则、目标不存在和越界都不能调用 handler。重复 `tool_use_id` 或已经提交过终态结果属于内部协议错误，应抛出专用异常并中止批次，不能伪装成普通失败。

实现每个 Session 独立的修正记账。只有 `malformed_arguments`、快速校验或 Pydantic 参数失败可标记 `correctable=True`；后续新调用通过 `correction_of_tool_use_id` 指向同一 Session、同一工具、上一轮的原始失败调用。每个原调用最多一次修正，修正不能再被修正，也不能跨工具或形成链。失败时返回 `correction_not_allowed` 且不运行 handler。

该里程碑通过表驱动测试证明每个失败阶段、错误码、字段路径、handler 未调用次数和一次修正的状态转换都符合预期。

### Milestone 3：实现重试、调度、取消与结果持久化

让 handler 只通过明确的异常类型报告 `transient`、普通业务失败或 `outcome_unknown`。执行器仅对 retry policy 允许的 transient 失败重试，总尝试次数最多 3；参数错误、目标策略错误、普通执行失败均不重试。每个 attempt 写 trace。超时或取消时，如果当前 `ExecutionTraits` 明确是无副作用只读调用，返回 `timeout` 或 `cancelled`；否则返回 `outcome_unknown`，且绝不自动重放。

批次执行前按原始顺序完成解析、校验、目标和分类，再划分最大连续安全段。连续 `concurrency_safe=True` 的调用通过 `asyncio.gather()` 并发，非安全调用形成屏障并串行运行；分类器抛错时保守地视为非安全。取消后不启动后续段，对已经启动的任务发出协作式取消并等待全部进入终态。最终 tuple 永远按原始 ToolUse 顺序排列，不按完成先后排列。虽然生产环境目前只有只读 `grep`，测试用内存 ToolSpec 构造安全和非安全 handler，以证明通用调度器的屏障语义。

在 `miniagent/tools/artifacts.py` 实现 `FileArtifactStore`。结果 UTF-8 编码超过工具阈值时，将完整最终内容原子写入 `<workspace>/.mini/sessions/<session_id>/tool_result/<tool_use_id>/result.txt`，metadata 写入同目录 `metadata.json`；临时文件和最终文件必须处于同一目录，写完后用替换操作提交。`grep_spec` 阈值是 20 KB，其他测试 spec 默认 50 KB，且任何工具不能提高系统硬上限。模型可见结果包含截断说明、固定头尾或固定前缀预览、相对 artifact 路径、字节数和 SHA-256；本计划不声称模型能用尚未实现的 `read_file` 读取全文。

trace sink 接收真实发生顺序的 `call_started`、`attempt_started`、`retry_scheduled`、`call_finished` 和错误事件。测试使用内存 sink 验证并发完成顺序，不在执行器内写 `message.jsonl`。该里程碑结束时，重试次数、屏障时序、取消终态、20 KB 边界、原子 artifact 和按请求顺序返回都必须有行为测试。

### Milestone 4：实现唯一生产工具 grep

在 `miniagent/tools/grep.py` 定义严格的 `GrepInput` 和 `grep_spec`。参数模型在构造时验证非空 pattern、合法正则、相对 path、glob 格式和 match 上限；真正的 workspace 边界仍由目标策略统一判断。目标解析器产生一个只读文件或目录目标，分类器固定返回 `ExecutionTraits(concurrency_safe=True)`。

handler 在异步边界内用 `asyncio.to_thread()` 执行同步扫描，并定期检查 `ExecutionContext` 的取消信号。若目标是目录，使用 `pathlib` 枚举普通文件，排除 `.git`、`.mini` 和越界符号链接，再按 POSIX 风格相对路径排序；`include` 只过滤文件相对搜索根的路径。逐文件按 UTF-8 文本读取，二进制、解码失败和读取时消失的文件计入跳过摘要。正则按行搜索，每行至多产生一个结果，达到 `max_matches` 立即停止并标记结果被调用参数截断。无匹配是成功，正文返回明确的 `No matches` 和扫描摘要，而不是失败。

测试用 `tmp_path` 创建嵌套文本、空文件、Unicode 内容、二进制文件、非 UTF-8 文件、长行、大小写样本和越界符号链接。验收应证明正则、大小写、单文件、递归目录、glob、稳定排序、行号、match 上限、跳过摘要和路径安全。在不支持创建符号链接的 Windows 环境中，测试应检测能力后明确 skip；不能把权限限制误报成实现通过。

### Milestone 5：接入 composition root、主循环端口和演示

在 `miniagent/tools/__init__.py` 提供一个 `build_default_registry()`，只注册 `grep_spec` 并立即冻结。`main.py` 的最小演示构造临时或明确的 workspace、Session ID、`FileArtifactStore`、内存 trace sink 和 `ToolExecutor`，提交一个固定 `grep` ToolUse，再把 function schema、结构化结果和 trace 摘要打印为 JSON。演示不得要求外部模型、网络或 API key，也不得修改被搜索文件。

若 `docs/plans/main-loop-implementation.zh-CN.md` 已经实施，则让工具执行器满足 `miniagent/ports.py` 中的 `ToolExecutor.submit_batch(batch, cancellation)`，并复用其领域类型。若主循环尚未实施，则先在工具模块提供同形接口和 standalone 演示，不创建假的 `SessionEngine`；在两个计划中记录待主循环落地后的薄适配任务。无论实施顺序如何，`AgentLoop` 都只能看到冻结 schema 和按原调用顺序的终态结果，不能直接访问 Registry 可变状态、handler 或 artifact 路径生成器。

该里程碑的最终人工证据是：默认注册表恰好列出 `grep`；演示 schema 的 `strict` 为 true；搜索仓库内专门 fixture 得到预期路径和行号；相同调用 ID 贯穿 ToolUse、trace 和 ToolResult；退出码为 0。

## Concrete Steps

所有命令均从仓库根目录 `D:\study\MiniAgent` 执行。先确认 Python 和依赖环境：

    uv sync
    uv run python --version

按测试先行顺序，每个里程碑先写一个会失败的行为测试，再加入最小实现并反复运行聚焦测试：

    uv run python -m pytest tests/tools/test_registry.py -q
    uv run python -m pytest tests/tools/test_executor.py -q
    uv run python -m pytest tests/tools/test_artifacts.py -q
    uv run python -m pytest tests/tools/test_grep.py -q

全部实现后运行：

    uv run python -m pytest -q
    uv run python -m compileall miniagent tests main.py
    uv run python main.py

2026-07-22 的真实执行结果为：聚焦命令 `uv run python -m pytest tests/tools -q` 得到 `29 passed`；全仓命令得到 `69 passed`；`compileall` 无错误；演示退出码为 0，并输出 `{"registered_tools":["grep"],"strict":true}` 以及包含 `tests/fixtures/demo_grep.txt:1:...DEMO_NEEDLE...` 的成功结果。

预期最终测试摘要没有 failed 或 error。演示输出的关键字段应类似：

    {"registered_tools":["grep"],"strict":true}
    {"tool_use_id":"demo-grep-1","tool_name":"grep","status":"success","content":"...:1:needle..."}

实现者应在本节把命令调整为仓库届时的实际测试文件名，并记录真实测试数量和关键输出。每完成或暂停一个步骤，都要更新 `Progress`；发现平台差异、Pydantic schema 差异或接口冲突时，立即更新 `Surprises & Discoveries` 和 `Decision Log`。

## Validation and Acceptance

验收以外部可观察行为为准。默认注册表冻结后只能查询，恰好暴露 `grep`；其 function schema 为 strict function，所有 object 禁止多余属性，框架修正字段存在且业务 handler 永远收不到它。合法调用只读取 workspace 内文件并返回稳定的相对路径、准确行号和匹配文本。绝对路径、`..`、符号链接逃逸、缺失目标、额外参数和宽松类型转换全部产生带原 `tool_use_id` 的结构化失败，且 handler 未运行。

一次参数失败可以由新的 ToolUse 显式修正；第二次、跨工具或链式修正被拒绝。transient 失败最多执行三次，普通失败不重试。连续只读调用可重叠执行，屏障两侧不能重叠，结果仍按输入顺序返回。取消后不启动新调用，所有已启动调用都有终态。大于 20 KB 的 `grep` 结果在受控目录留下完整、哈希可核验的 artifact，模型可见内容只包含确定性预览和引用。

`uv run python -m pytest -q`、`uv run python -m compileall miniagent tests main.py` 和 `uv run python main.py` 必须成功。新增测试应先证明缺少实现时失败，再在实现后通过。若 `uv` 不可用，可使用项目虚拟环境中的 `python -m pytest`，但必须把替代命令和环境原因记录回本计划。

## Idempotence and Recovery

Registry 构建和冻结不写磁盘，可重复创建；对同一实例重复 `freeze()` 应是无副作用或返回同一冻结视图，但冻结后 `register()` 必须失败。搜索只读，不修改目标文件。测试只写 pytest 的临时目录。artifact 以相同 `session_id/tool_use_id` 为唯一终态位置：首次提交使用原子替换，已经存在且哈希相同可幂等返回，内容不同则视为内部 ID 冲突并拒绝覆盖。

执行失败后保留已通过测试和已生成的诊断信息，修复后重跑当前里程碑即可。不要用删除整个 `.mini`、重置 Git 或覆盖用户文件来恢复。测试创建的 Session 目录必须位于 `tmp_path`；演示若需要生成 artifact，应使用专用 demo Session 并在输出中说明路径，不静默清理可能用于验收的证据。

## Artifacts and Notes

实施时在此追加简短、可核验的证据。例如：

    registry: frozen=True, tools=("grep",), strict=True
    grep: tool_use_id=call-1, matches=2, skipped_binary=1, truncated=False
    batch: completion_order=("call-2", "call-1"), returned_order=("call-1", "call-2")
    artifact: .mini/sessions/test-session/tool_result/call-large/result.txt, bytes=..., sha256=...

最终证据摘要：

    registry: frozen=True, tools=("grep",), strict=True
    grep: tool_use_id=demo-grep-1, matches=1, skipped_binary=0, truncated=False
    tests: tools=29 passed, all=69 passed
    batch: safe calls overlap=True, barrier starts after safe segment, returned_order=input_order
    artifact: real grep output >20480 bytes, result.txt hash equals returned sha256

不要粘贴完整大结果或整份 trace；只保存能够证明边界、顺序、重试次数和持久化完整性的摘要。

## Interfaces and Dependencies

`pyproject.toml` 最终必须声明 Python 3.11、`pydantic>=2,<3` 和 pytest 测试依赖，不引入供应商 SDK、系统 grep 包或网络服务。稳定接口至少应具有以下形态；如果主循环先实现了等价领域类型，以它为准并在 Decision Log 记录适配，不保留重复定义：

    @dataclass(frozen=True)
    class ToolSpec:
        name: str
        input_model: type[BaseModel]
        handler: AsyncToolHandler
        prompt_ref: PromptRef | None
        resolve_targets: TargetResolver
        classify: ExecutionClassifier
        retry_policy: RetryPolicy
        timeout_seconds: float | None
        result_policy: ResultPolicy
        function_schema: Mapping[str, object] | None = None

    @dataclass(frozen=True)
    class ExecutionContext:
        session_id: str
        run_id: str
        tool_use_id: str
        workspace_root: Path
        cancellation: Cancellation
        trace_sink: TraceSink
        artifact_store: ArtifactStore

    class ToolRegistry:
        def register(self, spec: ToolSpec) -> None: ...
        def freeze(self) -> None: ...
        def get(self, name: str) -> ToolSpec | None: ...
        def function_schemas(self) -> tuple[Mapping[str, object], ...]: ...
        def enabled_view(self, names: Collection[str] | None = None) -> ToolRegistryView: ...

    class ToolExecutor:
        async def submit_batch(
            self,
            batch: ToolExecutionBatch,
            cancellation: Cancellation,
        ) -> tuple[ToolResult, ...]: ...

    class GrepInput(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        pattern: str
        path: str = "."
        include: str | None = None
        case_sensitive: bool = True
        max_matches: int = Field(default=100, ge=1, le=1000)

`ToolResult` 必须至少包含 `tool_use_id`、`tool_name`、终态 status、attempt 数、成功内容或 `ToolFailure`、可选 `ArtifactRef`。`ToolFailure` 必须至少包含 code、stage、message、外部 alias 表示的 field errors、correctable 和 retryable。Registry 生成 schema，Executor 消费冻结后的 spec，`grep` handler 只消费已验证 `GrepInput` 和显式 `ExecutionContext`。

---

变更说明（2026-07-22）：首次创建中文工具系统 ExecPlan。计划按照 `PLANS.md` 补齐目的、持续维护区、仓库导航、参考设计文档、通用执行机制、唯一 `grep` 工具、分阶段验收、恢复策略和稳定接口；当前只完成调研与计划撰写，尚未开始代码实现。

变更说明（2026-07-22）：完成工具系统 ExecPlan。根据已落地的主循环调整接口兼容说明，记录 `to_thread` 取消和跨批次修正发现，实现 Registry、strict schema、Executor、路径策略、ArtifactStore 与唯一生产工具 `grep`，并写入 29 项工具测试、69 项全仓测试、编译和无密钥演示的真实验收证据；保留尚无 `read_file` 分页工具这一明确范围缺口。
