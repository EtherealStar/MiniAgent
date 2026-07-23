# 实现 MiniAgent 双层主循环

本 ExecPlan 是一份持续维护的中文执行计划，必须遵守仓库根目录 `PLANS.md` 的格式和维护要求。实现完成后，用户可以通过模型名、OpenAI-compatible API 根地址和 API Key 配置一个模型供应商，提交一条消息，看到模型的流式回复，看到工具调用及其结果被记录，再根据工具结果自动进入下一次模型调用；用户还可以取消运行、在上下文过长时触发一次压缩，并从事件序列判断运行最终为何停止。

## Purpose / Big Picture

MiniAgent 当前只有 `main.py` 的启动占位代码。此计划要建立一个可测试的主循环：`SessionEngine` 管理会话历史和事件顺序，`AgentLoop` 只执行一次 `AgentRun`，OpenAI-compatible 模型供应商由 `ModelAdapter` 隔离，工具由 `ToolExecutor` 隔离。缺少供应商配置时应用仍可启动，并能明确报告未配置；配置完整时，适配器使用 Chat Completions SSE 流产生统一 `ModelEvent`。完成后可以用内存 HTTP 传输和假的工具运行端到端测试，不需要真实供应商密钥且不消耗 Token；测试输出应证明历史不会被上下文压缩改写、草稿中断不会污染有效消息、工具结果能按 `tool_use_id` 关联，以及 `max_turns`、取消和输入过长都有结构化结果。

## Progress

- [x] （2026-07-22 14:00+08:00）阅读 `PLANS.md`、`docs/design-docs/main-loop.md`、`docs/design-docs/tool-registry-and-execution.md` 和 `CONTEXT.md`，确定术语、边界和验收场景。
- [x] （2026-07-22 14:30+08:00）阅读 `docs/design-docs/openai-compatible-model-provider.md`，把供应商配置、HTTP/SSE 转换、错误、取消和零 Token 测试纳入计划。
- [x] （2026-07-22）建立 Python 包结构、不可变消息/Part/事件模型、序列化和 ID 关联规则。
- [x] （2026-07-22）实现 `SessionEngine` 的唯一事件写入边界、序列号、幂等提交、运行队列、UI 隔离及内存/JSONL transcript 端口。
- [x] （2026-07-22）实现 `AgentLoop` 的 ModelCall、草稿事件、工具批次、停止原因和错误边界。
- [x] （2026-07-22）实现上下文投影、摘要检查点、工具结果裁剪和一次性 `prompt_too_long` 强制压缩重试。
- [x] （2026-07-22）实现 ProviderConfigLoader 和 OpenAI-compatible `ModelAdapter`，完成无真实网络的供应商协议测试。
- [x] （2026-07-22）接入工具执行端口、贯穿模型/工具的取消信号、跨 chunk think 标签可选处理和一次性恢复处理。
- [x] （2026-07-22）补齐 40 项单元及端到端测试，完成命令行演示、全量测试和静态语法验收。

## Surprises & Discoveries

- Observation：仓库当前没有应用包、测试目录或运行时依赖。
  Evidence：`main.py` 仅打印 `Hello from miniagent!`，`pyproject.toml` 的 `dependencies` 为空。
- Observation：主循环设计明确把工具注册/执行、Hook、transcript 存储和 UI 渲染排除在核心循环之外。
  Evidence：`docs/design-docs/main-loop.md` 第 2 节和 `docs/design-docs/tool-registry-and-execution.md` 第 1 节分别声明了边界。
- Observation：供应商适配器只转换传输协议，不能组装 AssistantMessage、拼接工具参数或解析普通文本中的 `<think>` 标签。
  Evidence：`docs/design-docs/openai-compatible-model-provider.md` 第 2、3 和 6 节明确规定这些职责属于 AgentLoop 或未来的独立文本处理层。
- Observation：仅在读取到下一个 SSE 行后检查取消，会让无数据连接无法及时结束。
  Evidence：`OpenAICompatibleModelAdapter._lines_until_cancelled()` 必须同时等待下一行和 `Cancellation.wait()`，取消胜出时关闭响应并抛出 `CancelledError`。
- Observation：强制压缩是否有效不能用 `ModelContext` 对象相等判断，因为构建出的临时 system 消息拥有新 ID。
  Evidence：实现改为比较投影中实际内容字符数；若未减少则直接返回 `PROMPT_TOO_LONG`，不发送无意义重试。

## Decision Log

- Decision：使用标准库 `dataclasses`、`enum`、`uuid` 和 `asyncio` 实现核心协议；模型适配器和工具执行器使用 Protocol 接口。
  Rationale：当前项目无依赖，核心循环应先可用、可替换，避免把供应商 SDK 或具体工具策略耦合进循环。
  Date/Author：2026-07-22 / Codex
- Decision：以 `docs/design-docs/exec-plans/main-loop-implementation.zh-CN.md` 作为独立 ExecPlan，不改写作为规范文件的 `PLANS.md`。
  Rationale：`PLANS.md` 明确要求具体 ExecPlan 独立成文件，并在计划中引用它。
  Date/Author：2026-07-22 / Codex
- Decision：事件先由 `SessionEngine` 接受并分配 `sequence`，再更新 Working Context；UI 投递失败不改变 AgentRun 控制流。
  Rationale：这同时保证历史唯一写入边界、确定性重放和 UI 断线后的恢复。
  Date/Author：2026-07-22 / Codex
- Decision：具体供应商实现使用复用的 `httpx.AsyncClient` 直接调用 `POST /v1/chat/completions`，不使用 OpenAI SDK，并通过 `python-dotenv` 只在配置层加载 `.env`。
  Rationale：这样可以精确控制 SSE、超时、连接所有权和错误映射，同时保证 ModelAdapter 不隐式读取配置。
  Date/Author：2026-07-22 / Codex
- Decision：OpenAI-compatible 适配器将 `<think>` 原样输出为 `TextDelta`；如需转换为 reasoning，另设位于适配器与 AgentLoop 草稿组装之间的可配置文本处理器。
  Rationale：供应商设计明确禁止适配器解释普通文本语义，独立处理层能保持协议转换单一职责并兼容主循环的 reasoning 展示需求。
  Date/Author：2026-07-22 / Codex
- Decision：主循环中的 `ToolSpec` 只携带冻结后的 function schema；参数校验、并发、重试和 handler 仍由外部 `ToolExecutor` 负责。
  Rationale：这使主循环可以执行和测试完整工具批次，同时不重复实现 `tool-registry-and-execution.md` 所属模块的职责。
  Date/Author：2026-07-22 / Codex
- Decision：命令行验收固定使用脚本化内存模型，真实 Provider Configuration 只做状态诊断。
  Rationale：默认命令可稳定演示事件顺序和停止原因，不访问网络、不泄露配置且不消耗 Token。
  Date/Author：2026-07-22 / Codex

## Outcomes & Retrospective

双层主循环、上下文投影、OpenAI-compatible Chat Completions SSE、供应商配置、工具执行端口、取消与进程恢复均已实现。用户在无配置时可正常启动并看到缺失变量名；同一命令随后用内存模型演示严格递增事件序列，并输出 `reason=COMPLETED` 和准确的 `turn_count`。`uv run python -m pytest -q` 通过 40 项测试，所有供应商测试均使用 `httpx.MockTransport`，没有真实网络或 Token 消耗。

本计划仍有意不实现具体 UI、完整 ToolRegistry/ToolExecutor 策略、摘要模型、memory 注入和 transcript 文件的反序列化加载；这些属于引用设计中划定的独立模块。当前 JSONL 端口提供可靠追加写和失败边界，进程恢复使用内存重放后的事件投影，不会重放未知工具副作用。

## Context and Orientation

术语以 `CONTEXT.md` 为准。Session 是包含多次 AgentRun 的会话；AgentRun 是一条不可变用户输入触发的一次完整执行；ModelCall 是一次实际发出的模型请求，`turn_count` 只统计它。Transcript 是按顺序追加的权威事件记录，Working Context 是当前 AgentRun 已被 SessionEngine 接受的消息视图，Model Context 是一次 ModelCall 的裁剪/摘要后的输入投影。Draft AssistantMessage 是尚未完成的流式草稿，Discard 是通过事件使草稿退出有效历史而不删除底层事件。

计划实施时以如下文件关系为导航：

- `main.py` 是启动入口，最终只负责创建依赖并调用会话接口，不承载循环细节。
- `miniagent/domain.py`（新建）放置 `Message`、`Part`、工具关联、停止原因和 `AgentRunResult` 等纯数据协议。
- `miniagent/events.py`（新建）放置未排序事件载荷和 `SessionEvent` 信封。
- `miniagent/session.py`（新建）实现 `SessionEngine`，拥有消息投影、序列号、输入队列和取消信号。
- `miniagent/loop.py`（新建）实现 `AgentLoop`，只依赖只读初始消息、ContextBuilder、ModelAdapter、工具批次提交器和事件 sink。
- `miniagent/context.py`（新建）实现 Working Context 到 Model Context 的构建、工具结果裁剪和不可变摘要。
- `miniagent/ports.py`（新建）声明模型、工具、事件确认、Hook 和持久化的 Protocol；具体实现可在测试中替换。
- `miniagent/provider/config.py`（新建）放置不可变 Provider Configuration、`Configured`/`NotConfigured` 加载结果和 `.env` 加载。
- `miniagent/provider/events.py`（新建）放置供应商层输出的 `ModelEvent` 数据类型。
- `miniagent/provider/errors.py`（新建）放置未配置、调用契约和协议错误。
- `miniagent/provider/openai.py`（新建）实现请求转换、SSE 解析和 `OpenAICompatibleModelAdapter`。
- `tests/`（新建）保存模型流、上下文、会话和端到端行为测试。
- `tests/provider/`（新建）使用 `httpx.MockTransport` 保存配置、URL、请求、SSE、错误、取消和客户端生命周期测试。

实现前必须完整阅读这些参考设计：`PLANS.md`（ExecPlan 约束）、`docs/design-docs/main-loop.md`（主循环职责、事件、停止和恢复语义）、`docs/design-docs/openai-compatible-model-provider.md`（配置、Chat Completions、SSE、错误、取消和 HTTP 生命周期）、`docs/design-docs/tool-registry-and-execution.md`（ToolRegistry/ToolExecutor 的输入输出及批次契约）、`CONTEXT.md`（术语和禁用混用）。`设计prompt.md` 只作为需求背景阅读，不能覆盖上述设计中的边界。

## Plan of Work

### Milestone 1：建立可运行的领域协议

在 `miniagent/domain.py` 定义不可变 `Message`、`TextPart`、`ReasoningPart`、`ToolUsePart`、`ToolResultPart` 和 `ContextSummary`。所有对象生成 UUID；工具结果显式保存 `tool_use_id` 和 `assistant_message_id`，不依赖数组位置。定义 `StopReason`，至少包含 `COMPLETED`、`MAX_TURNS`、`PROMPT_TOO_LONG`、`CANCELLED`、`PROCESS_INTERRUPTED`、`MODEL_UNAVAILABLE`、`EVENT_COMMIT_FAILED`，以及冻结的 `AgentRunResult`。

在 `miniagent/events.py` 定义 `SessionEvent`（`event_id`、`session_id`、`run_id`、`sequence`、时间和 payload）以及 `AssistantMessageStarted`、`AssistantPartDelta`、`AssistantMessageCompleted`、`AssistantMessageDiscarded`、`ToolUseDetected`、`ToolResultRecorded` 等载荷。事件载荷不得自行生成会话序列号。

验收是可以导入这些类型并序列化/反序列化一组消息；重复 `tool_use_id` 或不匹配的来源 ID 应在领域边界被拒绝。

### Milestone 2：实现 SessionEngine 写入边界

在 `miniagent/session.py` 实现 `SessionEngine`。它接受用户输入的只读快照，创建 `AgentRun`，通过 `emit(payload)` 原子地分配严格递增的 `sequence`、保存事件并更新有效消息投影。`emit` 返回确认；关键事件写入失败要可被 AgentLoop 识别为 `EVENT_COMMIT_FAILED`。UI sink 作为可失败的独立回调，失败只记录并允许按 sequence 重放。运行期间到达的新用户输入进入队列，不传入当前 AgentLoop。

提供 `cancel(run_id)` 和恢复函数：重放已有事件，未完成草稿追加 discard，未知工具结果不自动重试，运行以 `PROCESS_INTERRUPTED` 标记。持久化先提供内存实现和 JSONL 端口，具体 transcript 文件格式留给后续实现。

验收包括序列严格递增、相同 event_id 幂等、UI sink 抛错不终止运行，以及排队输入在当前运行完成后才被取出。

### Milestone 3：实现供应商配置和 OpenAI-compatible ModelAdapter

在 `miniagent/provider/config.py` 定义不可变 Provider Configuration。`ProviderConfigLoader` 从系统环境变量和 `.env` 读取 `OPENAI_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` 和可选的 `OPENAI_TIMEOUT_SECONDS`；环境变量优先，`.env` 不覆盖已有值，加载后形成快照。三个必需值缺少任意一个时返回只包含缺失变量名的 `NotConfigured`，应用仍能启动；完整配置返回 `Configured`。上层仍尝试创建或调用未配置适配器时抛出 `ProviderNotConfiguredError`，由 AgentLoop 映射为 `MODEL_UNAVAILABLE`。超时默认 60 秒，必须为正有限数。API 根地址必须通过 URL 解析器验证 scheme、host、query 和 fragment，移除尾部斜杠，再按最后一个路径段是否精确为 `v1` 生成 Chat Completions URL；已经包含 `/chat/completions` 的完整地址应在请求前拒绝。

在 `miniagent/ports.py` 定义 `ModelAdapter.stream(context, tools, options, cancellation)` 的异步 Protocol，在 `miniagent/provider/events.py` 定义 `TextDelta`、`ReasoningDelta`、`ToolUseDelta`、`ResponseCompleted` 和 `ResponseFailed`。`GenerationOptions` 首版只接受可选的 `temperature`、`max_tokens` 和 `tool_choice`；未知参数和非法值属于请求前契约错误，不发送请求。

在 `miniagent/provider/openai.py` 使用可复用的 `httpx.AsyncClient` 实现 `OpenAICompatibleModelAdapter`。请求固定使用 Bearer Token、JSON、SSE、`stream=true` 和 `stream_options.include_usage=true`，按顺序转换 system/user/assistant/tool 消息并保留工具调用 ID。没有工具时不发送 `tools` 或 `tool_choice`；有工具时发送冻结的 function schema，默认 `tool_choice="auto"`。连接超时固定 10 秒，流无数据超时来自配置。

SSE 解析器忽略空行和注释，只处理 `data:`，把合法 JSON 增量逐片映射为文本、结构化 reasoning 和原始工具调用片段；不拼接工具名或 arguments，不组装 AssistantMessage，`<think>` 始终保持 `TextDelta`。正常结束输出唯一 `ResponseCompleted` 并携带 finish reason 与可选 usage。HTTP 401/403、429、其他 4xx、5xx、超时、连接错误和协议错误分别形成 `authentication`、`rate_limit`、`client_error`、`server_error`、`timeout`、`connection_error`、`protocol_error` 的 `ResponseFailed`，每次未取消调用必须恰好有一个终态。错误摘要和日志不得包含 API Key、Header、消息正文或完整请求/响应。

适配器每次调用只发送一次 HTTP 请求，绝不自行重试。取消时立即关闭响应并继续抛出 `asyncio.CancelledError`，不输出 `ResponseFailed`。适配器提供 `async close()` 和异步上下文管理器；只关闭自己创建的 client，不关闭注入的 client。

验收完全使用 `httpx.MockTransport`：验证环境变量优先级、缺失配置、四类 URL、请求 JSON、消息和工具 ID 转换、文本/reasoning/工具片段、usage、全部错误映射、取消及 client 所有权。测试不得读取用户真实 `.env`、访问网络或消耗 Token。

### Milestone 4：实现流事件处理和 AgentLoop

在供应商 ModelAdapter 与草稿组装之间提供可选文本处理端口。默认实现透传 `TextDelta`；配置启用 `<think>` 兼容时，由独立状态机处理跨 chunk 和未闭合标签并产生 reasoning/text 事件。OpenAI-compatible 适配器本身始终保持原始文本。

在 `miniagent/loop.py` 实现 `AgentLoop.run(...)`。每次实际发出 ModelCall 前检查取消和 `turn_count`，发出后立即递增；每次调用使用新 Assistant `message_id`。流式内容先进入草稿，完成事件被 Session 接受后才加入 Working Context。流中断时追加 discard 并用新 ID、新 turn 重试；`finish_reason=length` 则保留当前消息并用 `continuation_of_message_id` 新消息续写。

AssistantMessage 完成后，将其 ToolUsePart 组成 `ToolExecutionBatch`，提交给外部工具执行器并等待每个 `tool_use_id` 的终态结果。结果按原始调用顺序加入下一轮 Model Context；工具失败是模型可见结果，不直接结束 AgentRun。最后一次允许的 ModelCall 产生工具调用时，工具仍执行，随后以 `MAX_TURNS` 停止，不再调用模型。

验收使用假的 ModelAdapter 验证：单轮文本返回 `COMPLETED`；工具调用会继续下一轮；乱序工具完成仍按原调用顺序组装；流中断草稿不出现在有效历史；长度截断生成 continuation；达到限制、取消和模型失败返回对应结构化原因。

### Milestone 5：实现上下文构建和输入过长处理

在 `miniagent/context.py` 定义 `WorkingContext` 和 `ContextBuilder.build()`。构建顺序固定为 system prompt、ContextSummary、恢复边界后的原始消息和当前动态内容。工具结果裁剪、旧消息摘要和 memory 注入只产生 Model Context，不修改 Transcript 或旧 ContextSummary。

实现已知 token/字符预算的主动裁剪；若 ModelAdapter 返回 `prompt_too_long`，只允许一次强制压缩并重新发起完整 ModelCall（该请求仍计一个 turn）。压缩没有减少输入或再次过长时返回 `PROMPT_TOO_LONG`。记录摘要的 `covers_through_message_id` 和 `resume_from_message_id`，后续消息不修改旧摘要。

验收包括对比 Transcript 和 Model Context 的快照证明原始消息未被改写，摘要边界可恢复，过长错误最多触发一次压缩重试且每个真实请求计数。

### Milestone 6：接入工具、取消和恢复契约

根据 `docs/design-docs/tool-registry-and-execution.md` 在 `ports.py` 约束 `ToolExecutor.submit_batch(batch, cancellation)` 的输入输出：核心循环不解析 schema、不决定串行/并发、不执行 handler，只传递已完成 ToolUsePart 并接收带 ID 的终态 ToolResult。保留 PreTool/PostTool Hook 为可选端口，Hook 异常按设计边界分类，不能改变消息关联。

让取消信号贯穿模型流和工具批次：不再启动新的 ModelCall 或工具；已启动且无法撤销的工具允许完成并记录真实副作用；无法确认副作用时标为未知，不能自动重放。进程恢复只重放事件和有效消息，不重放未知工具。

验收包括模型流中取消、不可取消工具期间取消、事件提交失败和进程恢复场景，并核对核心不变量第 1 至第 12 条。

### Milestone 7：入口、测试和可观察演示

在 `main.py` 加入 composition root：加载供应商配置，未配置时显示缺失变量名但保持程序可启动；演示和测试使用内存 `SessionEngine`、脚本化假模型或 MockTransport 适配器，以及一个无副作用假工具。保持 UI 和持久化实现可替换。添加 `pytest` 测试（必要时把测试依赖加入 `pyproject.toml`），覆盖主循环设计第 14 节和供应商设计第 10 节列出的场景，尤其是结构化 reasoning、工具增量组装、工具乱序、SSE 错误、prompt too long、取消、UI 断线和恢复。

演示命令从仓库根目录运行，输出事件序列、最终停止原因和 turn_count，便于人工确认机制真的工作，而不只是类型检查通过。

## Concrete Steps

所有命令均在 `D:\study\MiniAgent` 执行。实施每个里程碑后先运行：

    uv run python -m pytest

供应商里程碑可单独运行：

    uv run python -m pytest tests/provider -q

预期所有请求都由 `httpx.MockTransport` 接收，测试期间没有外部网络访问。若尚未安装依赖，先在 `pyproject.toml` 增加运行时依赖 `httpx`、`python-dotenv` 和测试依赖 `pytest`、`pytest-asyncio`，运行 `uv lock` 后重试。运行最小演示：

    uv run python main.py

不设置供应商环境变量时，预期程序正常启动并只列出缺少的变量名，不回显任何配置值。使用内存供应商演示时，输出至少包含一条按顺序编号的事件、工具结果关联的 `tool_use_id`、`reason=COMPLETED`（无工具场景）或 `reason=MAX_TURNS`（达到限制场景），以及准确的 `turn_count`。检查静态语法：

    uv run python -m compileall miniagent tests main.py

每次改变接口后，更新本计划的 `Progress`、`Surprises & Discoveries` 和 `Decision Log`，记录时间和证据；不要把未验证的假设写成已完成。

## Validation and Acceptance

功能验收以行为为准：缺少供应商配置时应用可启动，尝试调用才映射为 `MODEL_UNAVAILABLE`；完整配置能生成规范化 Chat Completions URL，并将 Model Context、ToolSpec 和生成选项准确转为请求。MockTransport 返回文本、reasoning、跨 chunk 工具调用、finish reason 和 usage 时，适配器逐片输出且只产生一个终态；`<think>` 在适配器层保持普通文本。HTTP/SSE/JSON/超时/连接错误产生正确且不泄露 API Key 的失败类别，取消抛出 `asyncio.CancelledError`，适配器不自动重试。

给假的或内存传输的模型一条用户消息后，SessionEngine 接受事件并产生严格递增序列；模型流中的文本和 reasoning 可重放；模型返回工具调用后，工具结果显式引用对应 `tool_use_id`，下一轮按原调用顺序看到结果；中断草稿不会出现在有效消息历史；输出截断会产生新的 continuation 消息；输入过长最多强制压缩一次；取消不会启动新的模型调用；进程恢复不会重复执行工具副作用。

测试命令 `uv run python -m pytest` 必须全部通过。新增测试应在实现前能构造出失败断言，在实现后通过；最终报告测试数量和关键场景。`uv run python main.py` 必须在无外部 API 密钥下退出码为 0，并输出结构化停止结果。若测试环境没有 `uv`，使用项目虚拟环境中的 `python -m pytest`，并在本节记录替代命令。

## Idempotence and Recovery

所有事件以 `event_id` 去重，重复提交不会重复追加消息；同一 `run_id` 的恢复只允许将未完成草稿作废一次。上下文压缩是追加新的不可变摘要，不修改历史，因此可重复构建。Provider Configuration 在一次加载后不可变，重复测试显式传入隔离环境和临时 `.env`，不得读取或改写用户真实凭据。测试和演示只使用 MockTransport、内存或仓库内临时目录，不删除用户文件。若某个里程碑失败，保留已通过的测试，修复后从该里程碑重新运行命令；不要通过重置 Git 或删除已有文件来恢复。

## Artifacts and Notes

计划执行过程中在此追加短证据，例如：

    事件序列：1 AssistantMessageStarted，2 AssistantPartDelta，3 AssistantMessageCompleted，4 ToolResultRecorded
    结果：reason=COMPLETED，turn_count=2，final_message_id=<uuid>

只记录能证明行为的日志、测试摘要或小范围 diff；完整 transcript 和供应商响应不直接嵌入计划。

    测试：40 passed in 0.88s
    演示事件：1 UserMessageRecorded，2 AssistantMessageStarted，3 AssistantPartDelta，4 AssistantMessageCompleted，5 RunTerminated
    演示结果：reason=COMPLETED，turn_count=1，final_message_id=<uuid>

## Interfaces and Dependencies

最终至少提供以下稳定接口：

    class EventSink(Protocol):
        async def emit(self, payload: EventPayload) -> SessionEvent: ...

    class ModelAdapter(Protocol):
        async def stream(self, context: ModelContext, tools: tuple[ToolSpec, ...], options: GenerationOptions, cancellation: Cancellation) -> AsyncIterator[ModelEvent]: ...

    @dataclass(frozen=True)
    class ProviderConfiguration:
        model: str
        base_url: str
        api_key: str  # 使用 dataclasses.field(repr=False)
        read_timeout_seconds: float = 60.0

    class ProviderConfigLoader:
        def load(self, environment: Mapping[str, str], dotenv_path: Path | None = None) -> Configured | NotConfigured: ...

    class OpenAICompatibleModelAdapter(ModelAdapter):
        async def close(self) -> None: ...

    class ToolExecutor(Protocol):
        async def submit_batch(self, batch: ToolExecutionBatch, cancellation: Cancellation) -> tuple[ToolResult, ...]: ...

    class ContextBuilder(Protocol):
        def build(self, working: WorkingContext, system_prompt: str, budget: int) -> ModelContext: ...

    class AgentLoop:
        async def run(self, initial_messages: tuple[Message, ...], user_message: Message, system_prompt: str, max_turns: int, event_sink: EventSink, cancellation: Cancellation) -> AgentRunResult: ...

`ProviderConfiguration` 的 `repr` 必须隐藏 API Key，也可以使用等价的内部秘密值类型。`OpenAICompatibleModelAdapter` 依赖 `httpx`，配置层依赖 `python-dotenv`，AgentLoop 只依赖 `ModelAdapter` Protocol。不得在核心接口中暴露供应商 HTTP 类型、UI 类型或可变 Session 历史。`ToolExecutor` 的 schema、参数校验、目标策略、重试和并发实现遵循 `docs/design-docs/tool-registry-and-execution.md`，由独立模块提供。

---

变更说明（2026-07-22）：首次创建中文主循环 ExecPlan。根据 `PLANS.md` 的自包含要求补充了仓库现状、参考文档、分阶段机制、具体命令、验收标准和恢复策略；尚未开始代码实现，因此实现类任务仍未完成。

变更说明（2026-07-22）：根据 OpenAI-compatible 供应商设计讨论收紧 ModelAdapter 边界。移除适配器中的 `<think>` 解析与工具调用组装职责，并同步更新测试场景、发现和决策记录，使本计划与 `docs/design-docs/openai-compatible-model-provider.md` 及主循环设计保持一致。

变更说明（2026-07-22）：将 OpenAI-compatible 模型供应商从抽象端口扩展为完整实现里程碑，补充配置快照与未配置状态、URL 规范化、Chat Completions 请求、SSE 事件、错误与取消、HTTP client 所有权、安全规则、`httpx.MockTransport` 零 Token 测试，以及对应模块、依赖、接口和验收标准。

变更说明（2026-07-22）：完成执行计划。实现领域协议、SessionEngine、ContextBuilder、AgentLoop、文本处理层、OpenAI-compatible 适配器、内存/JSONL transcript 端口和无网络演示；根据实际测试补充 SSE 取消竞速与压缩有效性发现，并记录 40 项测试通过证据和有意保留的模块边界。
