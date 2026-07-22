# OpenAI-compatible 模型供应商适配器设计

## 1. 背景与目标

MiniAgent 需要通过同一套内部接口调用不同的 OpenAI-compatible 模型供应商。更换供应商时，用户只需改变模型名、API 根地址和 API Key；`AgentLoop` 不感知具体供应商的 HTTP、SSE 或认证细节。

本文定义单供应商配置、Chat Completions 请求转换、流式响应转换、错误和取消语义，以及完全不消耗真实 Token 的验收方式。领域术语以仓库根目录 `CONTEXT.md` 为准，主循环如何消费 ModelEvent 以 `docs/design-docs/main-loop.md` 为准。

## 2. 范围

### 2.1 本文覆盖

- 从系统环境变量和 `.env` 加载单个 Provider Configuration；
- 缺少配置时的可启动状态和调用失败语义；
- OpenAI-compatible `POST /v1/chat/completions` 异步流式请求；
- Model Context、ToolSpec 和生成选项到请求 JSON 的转换；
- 文本、结构化 reasoning、工具调用增量、结束原因和 Token 用量的规范化；
- HTTP、SSE、JSON、超时、取消和供应商错误边界；
- HTTP 连接生命周期和无真实网络的测试策略。

### 2.2 本文不覆盖

- Responses API、非流式 Chat Completions 或多个候选答案；
- Azure Deployment 路径、`api-key` Header、自定义 Header 或查询参数认证；
- 多供应商同时配置、运行时路由、故障转移或负载均衡；
- 供应商私有请求参数的任意透传；
- `<think>...</think>` 等普通文本标签的解析；
- Assistant 草稿、工具调用组装、参数 JSON 校验、重试和消息提交；
- UI 的供应商配置表单和配置持久化。

## 3. 核心边界

```text
系统环境变量 / .env / 未来 UI
              |
              v
   ProviderConfigLoader
              |
              v
 Provider Configuration?  ----缺失---->  未配置状态
              |
              v
        ModelAdapter
       /            \
Model Context      ModelEvent 流
ToolSpec           TextDelta / ReasoningDelta / ToolUseDelta
生成选项            ResponseCompleted / ResponseFailed
              |
              v
       OpenAI-compatible Provider
```

配置来源与供应商协议是两个边界。`ProviderConfigLoader` 负责发现配置，`ModelAdapter` 只接收显式的 Provider Configuration，不读取 `.env`。未来 UI 只需产生相同的配置对象，无需改变适配器。

`ModelAdapter` 是薄协议边界。它转换每个供应商流片段，但不组装 AssistantMessage、工具调用或文本语义。`AgentLoop` 拥有草稿状态和工具增量组装；未来的独立文本处理层可以解析混在普通文本中的思考标签。

## 4. 供应商配置

### 4.1 配置项

首版配置以下环境变量：

```dotenv
OPENAI_MODEL=your-model
OPENAI_BASE_URL=https://provider.example.com/v1
OPENAI_API_KEY=your-key
OPENAI_TIMEOUT_SECONDS=60
```

`OPENAI_MODEL`、`OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 必须同时具有非空值，才能形成 Provider Configuration。`OPENAI_TIMEOUT_SECONDS` 可选，默认值为 60 秒，必须是大于零的有限数值。

系统环境变量优先于 `.env`；加载 `.env` 时不得覆盖进程已有变量。配置值在加载后形成不可变快照，ModelCall 期间不重新读取环境。

### 4.2 未配置状态

缺少或留空任何必需项不会阻止应用启动。配置加载结果必须能区分：

- `Configured`：包含完整且已验证的 Provider Configuration；
- `NotConfigured`：包含缺少的配置项名称，不包含任何配置值。

未来 UI 可以把 `NotConfigured` 显示为“没有供应商”。如果上层仍尝试创建或调用 ModelAdapter，则抛出 `ProviderNotConfiguredError`；`AgentLoop` 将其映射为 `MODEL_UNAVAILABLE`。不得返回空响应或静默跳过 ModelCall。

### 4.3 API 地址规范化

`OPENAI_BASE_URL` 表示 API 根地址，不接受已经包含 `/chat/completions` 的完整请求地址。规范化时解析 URL，而不是搜索原始字符串：

1. 拒绝缺少 `http` 或 `https` scheme、缺少 host、包含 query 或 fragment 的地址。
2. 移除路径末尾的 `/`。
3. 如果最后一个路径段是精确的 `v1`，追加 `/chat/completions`。
4. 否则追加 `/v1/chat/completions`。

示例：

```text
https://provider.example.com
  -> https://provider.example.com/v1/chat/completions

https://provider.example.com/v1/
  -> https://provider.example.com/v1/chat/completions

https://gateway.example.com/openai
  -> https://gateway.example.com/openai/v1/chat/completions

https://gateway.example.com/openai/v1
  -> https://gateway.example.com/openai/v1/chat/completions
```

域名或非末尾路径段中出现字符串 `v1` 不改变判断结果。Azure Deployment 等非标准路径不在首版范围内。

## 5. 请求协议

### 5.1 ModelAdapter 接口

ModelAdapter 接收 MiniAgent 内部类型，不接收任意 OpenAI 字典：

```python
class ModelAdapter(Protocol):
    async def stream(
        self,
        context: ModelContext,
        tools: tuple[ToolSpec, ...],
        options: GenerationOptions,
        cancellation: Cancellation,
    ) -> AsyncIterator[ModelEvent]: ...
```

`GenerationOptions` 首版只允许可选的 `temperature`、`max_tokens` 和 `tool_choice`。调用前应验证这些值；未知选项是调用契约错误，不发送请求。

### 5.2 HTTP 请求

适配器向规范化后的 URL 发送 `POST`，并使用固定 Header：

```http
Authorization: Bearer <OPENAI_API_KEY>
Content-Type: application/json
Accept: text/event-stream
```

请求体至少包含：

```json
{
  "model": "<OPENAI_MODEL>",
  "messages": [],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

`messages` 由 Model Context 按顺序转换。适配器必须覆盖 MiniAgent 使用的 `system`、`user`、`assistant` 和 `tool` 消息，以及 Assistant 工具调用和与其关联的 tool result。转换不得丢失 `tool_use_id`。

只有调用方提供 `temperature` 或 `max_tokens` 时才发送对应字段。首版不发送或处理多候选参数。

没有工具时，不发送 `tools` 和 `tool_choice`。有工具时，将每个冻结的 `ToolSpec.function_schema` 转换为 OpenAI-compatible function tool，并默认发送 `"tool_choice": "auto"`；调用方可以覆盖为 `"none"` 或指定一个函数。不得发送空的 `tools: []`。

### 5.3 超时

HTTP 连接超时固定为 10 秒。流读取的无数据超时使用 `OPENAI_TIMEOUT_SECONDS`，默认 60 秒。超时不是取消，必须产生类别为 `timeout` 的 `ResponseFailed`。

## 6. 流式响应协议

### 6.1 SSE 外壳

适配器只接受成功 HTTP 响应中的 SSE 数据帧。它忽略空行和 SSE 注释，读取每个 `data:` 字段；`data: [DONE]` 表示供应商流终止。普通数据必须是合法 JSON 对象，否则产生 `ResponseFailed(protocol_error)` 并停止消费响应。

适配器可以维护完成供应商协议所需的传输元数据，例如尚待 `[DONE]` 确认的 `finish_reason` 和最终 `usage`，但不得维护 Assistant 草稿或拼接内容。

### 6.2 ModelEvent 转换

每个有效供应商增量独立转换，不等待后续内容：

- `delta.content` 的非空字符串转换为 `TextDelta`；
- `delta.reasoning_content` 的非空字符串转换为 `ReasoningDelta`；
- `delta.tool_calls[*]` 转换为 `ToolUseDelta`，保留供应商给出的 tool call index、ID 片段、类型、函数名片段和 arguments 片段；
- `finish_reason` 保存为终态原因，不表示适配器已经组装好消息；
- 最终 `usage` 若存在，则提取 `prompt_tokens`、`completion_tokens` 和 `total_tokens`。

`delta.content` 中的 `<think>` 标签和标签内文本仍是普通 `TextDelta`，适配器不得识别、删除或改写。工具 `arguments` 也只作为字符串片段输出；适配器不得拼接、解析或校验其 JSON。

供应商正常结束时输出一个 `ResponseCompleted`，包含 `finish_reason` 和可选用量。供应商不支持或没有返回 usage 时，用量为 `None`，不视为失败。

### 6.3 终态约束

一次未取消的调用必须恰好输出一个终态：`ResponseCompleted` 或 `ResponseFailed`。终态之后不得输出其他 ModelEvent。流在明确结束前断开、缺少合法终止信息或响应外壳无法解析时，产生 `ResponseFailed(protocol_error)`；是否重试和如何作废已有草稿由 AgentLoop 决定。

## 7. 错误、重试与取消

### 7.1 ResponseFailed

所有请求发出后可预期的供应商失败都转换为 `ResponseFailed`：

```text
authentication       HTTP 401 或 403
rate_limit           HTTP 429
client_error         其他 HTTP 4xx
server_error         HTTP 5xx
timeout              连接或流读取超时
connection_error     DNS、TLS 或连接中断
protocol_error       无效 SSE、JSON 或响应结构
```

错误事件可以包含 HTTP 状态码、供应商 `error.code`、`error.type`、`error.message` 和 `x-request-id` 等请求 ID。无法解析的错误正文只保留有限长度的文本摘要。不得包含 API Key、请求 Header 或完整请求体。

缺少 Provider Configuration 和无效调用参数发生在请求前，属于显式异常，不伪装成供应商流错误。内部不变量破坏和代码缺陷也直接抛出。

### 7.2 不自动重试

ModelAdapter 每次调用只发出一次 HTTP 请求，不重试 `429`、`5xx`、超时或连接错误。特别是供应商已经输出部分流内容时，适配器不得暗中重放完整请求。AgentLoop 根据 `ResponseFailed`、草稿状态和 turn 限制统一决定是否重试。

### 7.3 取消

收到取消时，适配器立即关闭当前 HTTP 响应并继续抛出 `asyncio.CancelledError`，不产生 `ResponseFailed`。取消属于 AgentRun 控制流，不是供应商失败；AgentLoop 负责作废草稿并返回 `CANCELLED`。

## 8. HTTP 客户端生命周期

具体实现使用 `httpx.AsyncClient`，不用 OpenAI SDK。ModelAdapter 持有并复用一个 client，以复用连接池；它同时提供 `async close()` 和异步上下文管理器，确保连接可确定关闭。

构造函数允许注入外部 `AsyncClient`。适配器不得关闭不由自己创建的 client；所有权必须在构造时确定。该注入点用于测试中的 `httpx.MockTransport`，不应暴露到 AgentLoop 接口。

`.env` 使用 `python-dotenv` 加载。该依赖只属于配置层，不进入 ModelAdapter。

## 9. 安全与可观察性

- API Key 只能进入 Authorization Header，不得出现在事件、异常文本、日志或对象 repr 中。
- 日志可以记录供应商 host、模型名、HTTP 状态、错误类别、请求 ID、耗时和 Token 用量。
- 默认不记录消息正文、工具参数、完整请求体或完整响应帧。
- 配置诊断只列出缺失的变量名，不回显已有变量的值。

## 10. 验收场景

自动测试全部使用 `httpx.MockTransport` 或等价的内存传输，不访问真实供应商，也不提供默认真实供应商 smoke test。至少验证：

1. 系统环境变量覆盖 `.env`，缺少必需项返回 `NotConfigured` 且应用仍能启动。
2. 四类 API 根地址按第 4.3 节规则得到正确的 Chat Completions URL，无效 URL 在请求前被拒绝。
3. 请求使用 Bearer Token、流模式和 usage 选项；没有工具时不出现工具字段，有工具时默认 `tool_choice="auto"`。
4. `temperature` 和 `max_tokens` 仅在显式提供时出现，未知生成参数被拒绝。
5. 文本、结构化 reasoning 和工具调用分别输出原始增量；跨 chunk 的工具参数不在适配器中拼接。
6. `<think>` 文本原样作为 `TextDelta`，不会被错误转换为 `ReasoningDelta`。
7. 正常结束产生一个包含 finish reason 和可选 usage 的 `ResponseCompleted`。
8. HTTP 状态、超时、连接中断、无效 SSE 和无效 JSON 映射到相应 `ResponseFailed`，错误信息不泄露 API Key。
9. 取消关闭响应并抛出 `asyncio.CancelledError`，不产生失败终态。
10. client 被复用于多次调用，并且只有适配器拥有的 client 会由适配器关闭。

测试不得依赖外部网络、真实 `.env` 或用户机器上的供应商凭据。每个测试显式构造配置和模拟响应，以保证可重复且不产生 Token 费用。

## 11. 依赖关系与实现落点

实现阶段建议保持以下模块边界：

```text
miniagent/provider/config.py       Provider Configuration、加载结果和 .env 加载
miniagent/provider/openai.py       OpenAICompatibleModelAdapter 与请求转换
miniagent/provider/events.py       ModelEvent 数据类型
miniagent/provider/errors.py       配置、调用和协议错误
tests/provider/                    配置、请求、SSE、错误和生命周期测试
```

`miniagent/provider/` 不导入 `SessionEngine`、UI 或工具执行器。它只依赖领域只读输入类型、`httpx` 和配置层使用的 `python-dotenv`。`AgentLoop` 依赖抽象的 ModelAdapter Protocol，不依赖 `OpenAICompatibleModelAdapter` 具体类。
