# 实现 MiniAgent Textual 终端 UI

本 ExecPlan 是一个持续维护的中文实现计划，必须遵守仓库根目录 `PLANS.md` 的 ExecPlan 规则。它面向没有本项目背景的实现者，说明如何把 `docs/design-docs/textual-ui.md` 落地为可运行、可测试的 Textual 终端应用。

## Purpose / Big Picture

完成后，用户可以从终端启动 MiniAgent，在一个连续的聊天文档中提交多行输入，看到模型文本、可展开的 reasoning、用户可读的工具摘要和流式更新；输入仍可在 AgentRun 期间排队并可撤回。用户可以使用 `/model`、`/session`、`/clear`、`/quit`，切换模型或历史 Session，且切换和退出会停止唯一的 Current Session worker、丢弃未持久化队列并释放写锁。

可观察的结果是：在 `D:\study\MiniAgent` 执行 `uv run python -m miniagent.ui` 能打开 Textual 界面；提交一条消息会立即出现 `queued` 用户项，随后变为正式消息并逐步出现 Assistant 内容；打开历史 Session 会替换完整 UI Projection；运行 `uv run python -m pytest` 时新增 UI 测试全部通过。无供应商密钥时，应用仍可启动并在真正提交时显示可读的模型不可用错误。

## Progress

- [x] (2026-07-23) 阅读 `AGENTS.md`、`PLANS.md`、Textual UI 设计及当前核心实现，确认 UI 尚不存在且计划需遵循单文件 ExecPlan 格式。
- [x] (2026-07-23) 增加 Textual 依赖、UI composition root 和与现有 `SessionEngine` 的生命周期适配接口。
- [x] (2026-07-23) 实现 snapshot/SessionUpdate 的确定性 UI Projection、消息渲染和敏感信息过滤。
- [x] (2026-07-23) 实现命令、Composer、状态栏、可见区消息视图和两个 Modal。
- [x] (2026-07-23) 接入唯一 SessionEngine worker、无密钥模型错误映射和安全退出流程。
- [x] (2026-07-23) 完成 UI 单元/集成测试、compileall 和全量 pytest 验收。

## Surprises & Discoveries

- 当前 `miniagent/session.py` 提供 `begin_run()`、`emit()`、`enqueue_input()` 和 `cancel()`，但设计文档要求的 `start/submit/withdraw/stop/snapshot` 门面尚未存在。UI 实现必须先增加一个薄适配层或补齐等价接口，不能让 Textual 直接操作私有队列或 Journal。
- 当前 `main.py` 是无 UI 的演示 composition root，且 `pyproject.toml` 尚未声明 `textual`。依赖和入口是实现前置条件，不应把 UI 逻辑塞进 `main.py`。
- `Session Update` 是可丢失的展示通知而不是恢复事实；因此事件缺口不做自动补放，重新打开 Session 必须使用完整 snapshot 重建投影。
- Textual 将 `Screen` 作为 App 子节点时不会把焦点稳定交给嵌套输入框；App composition root 直接组合 viewport、status bar 和 Composer 后，Pilot 才能可靠提交 Enter。

## Decision Log

- Decision: 新建 `miniagent/ui/`，不把 Textual 类型引入 `domain.py`、`loop.py` 或 `tools/`。
  Rationale: UI 是展示和生命周期编排边界；核心模块需要保持可在无终端环境下测试和替换。
  Date/Author: 2026-07-23 / Codex
- Decision: 使用一个 `session_transition_lock` 串行化首条消息创建、Session 切换、`/clear` 和关闭；普通提交不持有该锁。
  Rationale: 设计规定全进程只有一个 Current Session worker，锁可避免切换和创建竞态，同时不阻塞普通排队输入。
  Date/Author: 2026-07-23 / Codex
- Decision: 首版采用“完整 snapshot + 可丢失 update + 可见行虚拟渲染”，不实现 cursor、revision、历史分页或后台 Session。
  Rationale: 与 `textual-ui.md` 的非目标一致，先保证可恢复的事实来源和长历史的渲染性能。
  Date/Author: 2026-07-23 / Codex
- Decision: `RuntimeSession` 以 `RuntimeSession.start/open` 作为唯一创建/恢复入口，worker 在 facade 内部运行，UI 只依赖 `SessionHandle`。
  Rationale: 现有 `SessionEngine` 公开的是持久化提交和队列原语；把 AgentLoop 调度藏在 facade 中可保持 Textual 不解析模型协议。
  Date/Author: 2026-07-23 / Codex

## Outcomes & Retrospective

已完成首版可运行入口和 UI 边界实现。`uv run python -m miniagent.ui` 使用 Textual 启动；无供应商密钥时启动不创建 Session 目录，首次提交由 `_UnavailableLoop` 持久化模型不可用终态。UI 测试覆盖命令、投影替换/撤回、布局索引、敏感字段过滤、首条创建和 writer lock 释放；全量 `pytest` 共 111 项通过。

首版仍保留一个刻意限制：消息高度暂以固定估算值建立索引，宽度变化后的精细 Markdown 行高修正留待后续迭代；当前已按可见区和 overscan 限制控件创建，配置有效时默认入口会构造真实 OpenAI-compatible AgentLoop，无密钥时才使用可读的不可用模型终态。

## Context and Orientation

`miniagent/domain.py` 定义不可变的 `Message`、`TextPart`、`ReasoningPart`、`ToolUsePart` 和 `ToolResultPart`；`miniagent/events.py` 定义 `SessionEvent` 及 Assistant/User/Tool/Run 事件；`miniagent/session.py` 是唯一的消息事实写入边界并维护有效消息投影；`miniagent/loop.py` 执行 AgentRun；`miniagent/provider/` 负责模型配置和 OpenAI-compatible 流；`miniagent/tools/` 负责工具注册和执行。UI 只能调用公开的 SessionEngine/Repository/RuntimeConfig/Model Provider 接口，不能运行 AgentLoop、解析供应商协议、写 `message.jsonl` 或执行工具。

术语按 `CONTEXT.md` 使用：Current Session 是当前唯一可交互会话；Queued Input 是已被引擎接受但尚未持久化的内存输入；Session Update 是面向 UI 的非恢复通知；UI Projection 是 snapshot 和 update 派生的非权威展示状态；AgentRun 是一条用户输入触发的完整执行；ModelCall 是一次实际发出的模型请求。

实现前必须完整阅读以下资料，并在编码时以它们的边界为准：

- `PLANS.md`：ExecPlan 的自包含、里程碑、进度、决策和验收要求。
- `docs/design-docs/textual-ui.md`：布局、生命周期、投影、流式 Markdown、reasoning/tool 展示、虚拟滚动、命令、Modal、退出语义及测试不变量。
- `CONTEXT.md`：领域术语和禁止混用的名称。
- `docs/design-docs/main-loop.md`：SessionEngine、AgentRun、取消、队列、事件提交和恢复边界。
- `docs/design-docs/persistence-and-observability.md`：SessionRepository、writer lock、snapshot/message journal 和关闭时 drain 语义。
- `docs/design-docs/overall-architecture.md`：composition root、模块依赖方向和运行时边界。
- `docs/design-docs/openai-compatible-model-provider.md`：模型列表、配置快照、流式 ModelEvent 和错误分类。
- `docs/design-docs/tool-registry-and-execution.md`：ToolUse/ToolResult 关联、展示摘要和敏感字段过滤边界。
- `miniagent/domain.py`、`miniagent/events.py`、`miniagent/session.py`、`miniagent/provider/config.py`：现有接口和需要兼容的类型。

## Plan of Work

### Milestone 1：依赖、入口和 Session 生命周期门面

在 `pyproject.toml` 增加与 Python 3.11 兼容的 `textual` 运行依赖，更新锁文件。新建 `miniagent/ui/app.py` 作为 `MiniAgentApp` 的 composition root：注入 `SessionRepository`、`RuntimeConfig`、模型列举器和 SessionEngine factory；不在其中实现 AgentLoop。

先在 `miniagent/session.py` 或独立的 `miniagent/ui/session_facade.py` 提供明确的异步门面：`start(first_text)`、`submit(text)`、`withdraw(message_id)`、`snapshot()`、`stop(reason)` 和 update 订阅。门面负责把现有 `begin_run/emit/enqueue_input/cancel` 组合成设计要求的语义，并为 UI 返回 `message_id/run_id`。若持久化仓库尚未完成，先提供内存 fake 和 Protocol，确保 UI 测试不依赖文件或网络。

验收：应用可在无密钥环境启动空白界面，不扫描历史、不创建空 Session；首条输入创建失败时 Composer 原文保留，成功后才设置 Current Session；任何时刻最多一个 worker。

### Milestone 2：纯 UI Projection 和展示模型

新建 `miniagent/ui/projection.py`。定义 `UiMessage`、有序 `UiPart`、`queued/draft/completed/failed` 生命周期和 `Projection.apply(update)`；snapshot 应完整替换投影，update 应按 `message_id/part_id/tool_use_id` 确定性更新。草稿 discard 后不进入有效消息，缺失或乱序的非关键 update 不修改事实。

新建 `miniagent/ui/renderers/message.py`、`reasoning.py`、`tool.py`、`status.py`。TextPart 使用 Textual Markdown 渲染；流式时缓存已闭合 Markdown block，只重算末尾未闭合 block。Reasoning 折叠态取原文首个非空片段并单行截断，展开态显示原文。Tool Presentation Registry 按 tool name 生成摘要和可展开正文，未注册工具使用安全回退文案并过滤参数中的密钥、令牌、路径和大段 JSON。

验收：给定同一 snapshot 和 update 序列，投影结果完全相同；同一 AssistantMessage 中的 text/reasoning/tool 顺序不变；UI 输出不包含 `tool_use_id`、原始 arguments、Journal sequence 或供应商错误堆栈。

### Milestone 3：主界面组件、Composer 和 slash command

按设计新建 `miniagent/ui/screen.py`、`composer.py`、`status_bar.py`、`commands.py`。`ChatScreen` 组合连续文档流的 `MessageViewport`、`NewContentButton`、补全 overlay、StatusBar 和 Composer；状态栏只显示 cwd、Session 标题/空白状态和当前 model。

`Composer` 仅保存未提交文本，`Enter` 提交、`Ctrl+Enter` 换行、`Tab` 补全、`Escape` 关闭补全或 Modal。`commands.py` 只在首 token 精确匹配时识别 `/model`、`/session`、`/clear`、`/quit`；未知斜杠文本作为普通消息。`Ctrl+C` 按“清空 Composer、取消活动 Run、两次确认退出”的优先级处理，并使用 1.5 秒窗口。

验收：组件测试覆盖多行输入、命令补全、未知命令、焦点恢复和快捷键优先级；提交后 queued 消息立即出现，Composer 被清空，生命周期转换期间提交控件被禁用。

### Milestone 4：虚拟滚动与流式更新

新建 `miniagent/ui/layout_index.py`、`render_cache.py`、`viewport.py`。`VirtualLayoutIndex` 保存消息高度、前缀高度和 scroll_y 到消息定位；`MessageRenderCache` 缓存可复用 Strip；`MessageViewport` 只创建可见区加 overscan 的 widgets。流式内容增长、Markdown 换行、reasoning 展开和终端 resize 时更新受影响高度。

实现滚动锚点：用户在底部时自动跟随新内容；用户向上查看历史时不拉动视口，并显示紧凑的返回底部按钮；resize 先重排可见区，空闲时修正屏幕外高度。不要引入历史分页，打开 Session 仍一次载入完整 snapshot。

验收：用数千条假消息证明屏幕外不创建 widget；流式追加和 resize 后锚定消息与行偏移保持不变；离开底部时新消息只增加按钮提示。

### Milestone 5：Session/Model Modal 和切换机制

新建 `miniagent/ui/modals/model_picker.py`、`session_picker.py`。Model Picker 每次打开都调用 Provider 列表接口，不做应用级缓存；失败保留当前 model 并显示可读错误；成功通过 RuntimeConfig 原子写回，当前 AgentRun 不变。Session Picker 只在打开时扫描仓库，显示标题、更新时间和可打开状态；损坏条目保留但不可确认。

切换流程在 `session_transition_lock` 内执行：先预打开目标并获取 writer lock，失败时关闭 Modal 且保持当前 Run；成功后 stop 当前 Engine、取消活动 Run、丢弃队列、释放旧锁，启动目标唯一 worker，用目标 snapshot 替换 projection 并滚到底部。`/clear` 和 `New session` 复用同一流程，但回到空白状态且不创建空目录。

验收：目标预打开失败绝不影响当前 Session；成功切换会停止旧 Run；选择当前 Session 不触发切换；`/clear` 与 `New session` 行为一致。

### Milestone 6：退出、错误映射和真实入口

在 `app.py` 实现 `/quit` 和双击 `Ctrl+C` 的统一异步关闭：获取生命周期锁、禁止新操作、调用 `stop(APPLICATION_SHUTDOWN)`、drain Journal、释放 writer lock，超时后退出并允许恢复逻辑补记 `PROCESS_INTERRUPTED`。把 `StopReason`、Provider 错误和持久化失败映射成短中文文案，不把异常对象或 trace 放入消息区。

更新 `main.py` 只做依赖组装并调用 `MiniAgentApp.run()`；保留现有无网络演示为独立函数或测试入口，避免启动 UI 时执行示例工具。必要时提供 `python -m miniagent.ui` 的 `__main__.py`。

验收：正常完成不额外显示状态项；取消、模型不可用、上下文过长、限制、持久化失败和超时各有明确文案；关闭后 worker 已结束且锁可再次获取。

### Milestone 7：测试、演示和文档回填

新增 `tests/ui/`：`test_commands.py`、`test_projection.py`、`test_layout_index.py`、`test_renderers.py`、`test_app_lifecycle.py`、`test_modals.py`，使用假的 SessionEngine、Provider 和 Textual pilot，不访问真实网络、凭据或用户 Session 目录。覆盖设计文档第 15 节所有不变量，特别是首条创建失败、queued 撤回、切换失败保护、snapshot 替换、虚拟滚动锚点、敏感信息过滤和退出释放锁。

完成后运行全量 pytest 和最小人工演示；把测试数量、关键输出和发现回填本计划的 `Progress`、`Surprises & Discoveries`、`Artifacts and Notes` 与 `Outcomes & Retrospective`。

## Concrete Steps

所有命令从 `D:\study\MiniAgent` 执行。依赖修改后运行：

    uv lock
    uv run python -m pytest

UI 单测和 Textual pilot：

    uv run python -m pytest tests/ui -q

静态语法检查：

    uv run python -m compileall miniagent tests main.py

启动终端应用：

    uv run python -m miniagent.ui

人工验收至少执行：提交普通消息；在运行中提交第二条并撤回；打开 `/session` 选择一个有效和一个损坏条目；切换 `/model`；执行 `/clear`；用 `Ctrl+C` 取消后再次按下退出。预期每一步都有设计文档规定的可读状态，且应用不打印原始协议或密钥。

## Validation and Acceptance

功能验收以行为为准：空启动无目录副作用；首条消息成功后才出现正式 UserMessage；排队项可见、可撤回并在提交事件后转正；流式文本不会重复或丢失，reasoning/tool 按原顺序展示；长历史只渲染可见范围且滚动锚点稳定；Session 切换失败保持旧会话，成功切换停止旧 worker；模型列表不缓存；退出释放唯一 worker 和 writer lock。

`uv run python -m pytest` 必须通过全部原有和新增测试。任何依赖 Textual 的测试都使用 `textual.app.App.run_test()` 或等价的 pilot，避免要求真实 TTY；Provider 列表和 SessionRepository 使用 fake。若环境没有 `uv`，使用 `.venv\Scripts\python.exe -m pytest`，并把替代命令记录在本节。

## Idempotence and Recovery

重复运行测试不得读取或修改真实 `.env`、Session 目录或 API 凭据；所有 fixture 使用 `tmp_path` 和内存依赖。UI update 丢失不会修改 Journal，重新打开 Session 以 snapshot 完整替换 projection。切换和退出若在中途异常，必须在 `finally` 中释放生命周期锁、旧 writer lock 和订阅任务；重试应从当前 Current Session 状态继续，不创建空 Session。不要使用 Git reset、递归删除或覆盖用户文件作为恢复手段。

## Artifacts and Notes

实施时在本节追加最小证据，例如：

    测试：uv run python -m pytest
    结果：111 passed
    静态：uv run python -m compileall miniagent tests
    结果：通过
    Pilot：无密钥启动不创建 sessions 目录；未知 slash 文本正常提交；queued 投影可撤回；停止后 writer lock 可再次获取

只记录能证明用户行为的短日志或小范围 diff，不嵌入完整模型响应、Transcript 或敏感配置。

## Interfaces and Dependencies

最终应提供以下稳定边界（具体类型可按现有实现调整，但语义不得改变）：

    class SessionHandle(Protocol):
        async def start(self, first_text: str) -> "AcceptedInput": ...
        async def submit(self, text: str) -> "AcceptedInput": ...
        async def withdraw(self, message_id: UUID) -> bool: ...
        async def snapshot(self) -> "SessionSnapshot": ...
        async def stop(self, reason: str) -> None: ...
        def subscribe(self, callback: Callable[[SessionUpdate], Awaitable[None]]) -> Callable[[], None]: ...

    @dataclass(frozen=True)
    class AcceptedInput:
        message_id: UUID
        run_id: UUID

    class UiProjection:
        def replace(self, snapshot: SessionSnapshot) -> None: ...
        def apply(self, update: SessionUpdate) -> set[UUID]: ...

    class VirtualLayoutIndex:
        def update_height(self, message_id: UUID, height: int) -> None: ...
        def visible_range(self, scroll_y: int, viewport_height: int, overscan: int) -> tuple[int, int]: ...

    class ToolPresentationRegistry(Protocol):
        def present(self, tool_use: ToolUsePart, result: ToolResultPart | None) -> ToolPresentation: ...

Textual 只依赖这些领域 Protocol，不让领域层依赖 Textual。运行时依赖为 `textual`，已有 `httpx`、`pydantic` 和 `python-dotenv` 保持不变；测试使用 pytest、pytest-asyncio 和 Textual pilot。所有模型/Session 真实实现都通过 composition root 注入，便于在无网络环境下验证。

---

变更说明（2026-07-23）：首次创建 Textual UI 中文 ExecPlan。根据 `PLANS.md` 补充自包含背景、参考文档、Session 生命周期适配、UI Projection、流式 Markdown、虚拟滚动、命令/Modal、退出机制、测试验收和恢复策略；尚未开始 UI 代码实现。
