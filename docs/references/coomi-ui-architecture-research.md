# Coomi UI 架构调研及 MiniAgent 对照

## 1. 目的、范围与结论摘要

本文基于仓库内的一手源码，调研参考项目 Coomi 的 Textual 终端 UI，并与 MiniAgent 当前 UI 架构对照。重点回答：Coomi 如何组合屏幕、传递 Agent 事件、隔离流式与历史渲染、处理交互模式和恢复 UI；哪些设计值得迁移；哪些实现细节不应照搬。

调研范围：

- `docs/reference/coomi/ui/` 全部 UI 源码；
- `docs/reference/coomi/engine/loop.py` 中 `AgentLoop.run_stream()` 的事件生产链路；
- `miniagent/ui/` 当前实现；
- `docs/design-docs/textual-ui.md`；
- 三份已完成 UI ExecPlan：`textual-ui-implementation.zh-CN.md`、`ui-visual-theme-implementation.zh-CN.md`、`ui-rendering-repair.zh-CN.md`；
- `CONTEXT.md` 中 Session、Session Update、UI Projection、AgentRun 等术语。

本次是静态源码调研，没有启动或人工观察 Coomi/MiniAgent UI，也没有据此声称某个视觉故障已被运行时复现。已完成计划记录的历史测试结果只作为既有证据引用，不代表本次重新运行了这些测试。

结论摘要：

1. Coomi 最值得借鉴的不是配色，而是稳定的五区屏幕骨架，以及“历史 `RichLog`、当前流式 `StreamingPreview`、运行状态 `StatusPanel`”三路分离。主屏幕一次组合长期 Widget，运行时只更新负责当前状态的区域（`docs/reference/coomi/ui/screens/main_screen.py:49-57`；`docs/reference/coomi/ui/textual_app.py:1227-1236`）。
2. Coomi 的 producer-consumer 链路很直接：`AgentLoop.run_stream()` 产生类型化 `AgentEvent`，Textual 后台 worker 消费并映射为 UI 更新（`docs/reference/coomi/engine/loop.py:258-269`；`docs/reference/coomi/ui/textual_app.py:1217-1249`）。
3. Coomi 通过 50ms trailing throttle 和最多 500 字符的独立预览减少高频 Markdown 重绘；流结束才把完整 Markdown 写入历史（`docs/reference/coomi/ui/widgets/streaming_preview.py:17-43`；`docs/reference/coomi/ui/textual_app.py:1356-1359`）。
4. MiniAgent 的领域边界明显优于 Coomi：SessionEngine 是事实写入边界，UI 只消费 snapshot/update，并有确定性的 `UiProjection`。这个边界应保留，不能为了模仿 Coomi 让 Textual 直接消费供应商或 AgentLoop 协议（`docs/design-docs/exec-plans/completed/textual-ui-implementation.zh-CN.md:29-38`；`miniagent/ui/session_facade.py:37-57`）。
5. MiniAgent 当前的主要结构性风险是：每个 Session Update 都触发 `MessageViewport.refresh_projection()`，该方法重建布局索引、移除全部子 Widget、重新挂载当前切片；没有节流，也没有单独的进行中预览（`miniagent/ui/app.py:210-218`；`miniagent/ui/viewport.py:33-47`）。这与设计文档要求的动态高度、锚点稳定、只更新必要项不完全一致（`docs/design-docs/textual-ui.md:216-218,246-248`）。
6. 当前所谓虚拟化只按固定高度 3 计算切片，却没有为未挂载区建立占位高度，也没有在滚动和 resize 时按 projection 重建可见窗口；因此从源码看，它尚不是完整的行级虚拟滚动实现（`miniagent/ui/viewport.py:33-43,54-60`；对照目标 `docs/design-docs/exec-plans/completed/textual-ui-implementation.zh-CN.md:92-98`）。
7. 推荐迁移的是 Coomi 的渲染节奏和区域责任，不是其集中式 1400 行 App、直接写日志、按工具名关联状态、原始错误输出或宽泛吞异常的做法。

## 2. 参考项目目录与职责

### 2.1 应用协调层

`docs/reference/coomi/ui/textual_app.py` 是 UI composition root 和运行协调器。`CoomiApp` 持有 provider、AgentLoop、Session、memory、工具注册表、状态数据，以及 agent/stream/reasoning/tool/交互/退出等大量 UI 状态（`docs/reference/coomi/ui/textual_app.py:211-288`）。它负责：

- 启动时装配依赖并创建 Session（`docs/reference/coomi/ui/textual_app.py:313-350`）；
- 打开主 Screen 和各 Modal（`docs/reference/coomi/ui/textual_app.py:316-317,357-402`）；
- slash command 分发（`docs/reference/coomi/ui/textual_app.py:1008-1051,1085-1125`）；
- 后台消费 Agent 事件并更新 Widget（`docs/reference/coomi/ui/textual_app.py:1217-1412`）；
- 管理交互状态、取消、退出和 spinner（`docs/reference/coomi/ui/textual_app.py:645-815,1170-1215,1423-1449`）。

这是“薄 AgentLoop、厚 UI 协调器”的结构。优点是链路易追踪；代价是 App 同时掌握业务对象、持久化 Session、命令、渲染和生命周期，模块边界较浅。

### 2.2 事件协议

`docs/reference/coomi/ui/events.py` 定义 14 个 dataclass 事件：

- 内容：`TextChunk`、`ReasoningChunk`（`events.py:13-22`）；
- 工具：`ToolStart`、`ToolRunning`、`ToolDone`、`ToolCacheHit`（`events.py:25-49`）；
- 状态：`UsageUpdate`、`CompressionEvent`（`events.py:52-62`）；
- 终止：`AgentError`、`AgentCancelled`（`events.py:65-80`）；
- Loop 模式：`LoopStepStart`、`LoopStepDone`、`LoopProgress`、`LoopIssueCreated`（`events.py:87-114`）。

事件对象只携带 UI 消费所需的规范化增量，表面上避免 UI 解析 provider SSE。但协议定义在 `ui` 包且由 engine 导入，说明参考项目存在 engine → UI 类型层的反向依赖；MiniAgent 不应复制这个依赖方向。

### 2.3 Screen

`screens/main_screen.py` 定义固定主屏幕；其 `compose()` 依次产生 Header、历史日志、流式预览、状态栏和输入框（`docs/reference/coomi/ui/screens/main_screen.py:49-57`）。Screen 还处理日志文本复制、Esc 清除选择和把取消/退出委托给 App（`main_screen.py:75-95`）。

其他 Screen 都是 Modal：

- `CommandPalette`：过滤命令、上下移动、Enter 返回命令、Esc/Ctrl+P 关闭（`docs/reference/coomi/ui/screens/command_palette.py:66-86,88-134`）；
- `SettingsScreen`：选择 provider/skill/MCP 设置入口（`docs/reference/coomi/ui/screens/settings_screen.py:21-75`）；
- `ProviderListScreen`：浏览、编辑、新建、删除 provider（`docs/reference/coomi/ui/screens/provider_list_screen.py:17-31,79-107`）；
- `ProviderEditScreen`：preset、字段输入、Ctrl+S 保存、Esc 取消（`docs/reference/coomi/ui/screens/provider_edit_screen.py:34-43,70-103,128-161`）。

### 2.4 Widget

- `SelectableRichLog`：在 RichLog 上增加鼠标拖选、行坐标换算、选区高亮和复制文本能力（`docs/reference/coomi/ui/widgets/selectable_rich_log.py:15-36,43-72,88-125,161-195`）。
- `StreamingPreview`：展示 thinking/tool/current text，进行 50ms 节流并限制为末尾 500 字符（`streaming_preview.py:14-59`）。
- `StatusPanel`：读取 `StatusLine`，自身保存 mode/spinner/tool/compression/exit/loop 状态，以单次 `render()` 构造两行 Rich Table（`status_panel.py:15-35,38-92,95-142`）。
- `PromptTextArea`：Enter 发布 Submitted，Ctrl+Enter 插入换行（`prompt_text_area.py:18-50`）。
- `ToolCallBanner`：不是 Widget，而是 pending → running → done 的数据/Renderable 构建器，完成后写入 RichLog（`tool_call_banner.py:1-4,15-55`）。
- `PlanPanel`、`CommandList`、`ModelPicker`、`ContextPicker`：保存少量选择状态，以 `render()` 全量生成 Rich Table；键盘操作只改状态并 `refresh()`（如 `plan_panel.py:1-5,39-123`；`context_picker.py:27-83`）。
- `CustomHeader`：固定顶栏及设置入口；`tool_formatter.py`：按工具名把参数变成紧凑可读文本（`docs/reference/coomi/ui/tool_formatter.py:7`）。

### 2.5 样式

全局 `coomi.tcss` 使用垂直 Screen、黑色消息背景、GitHub 风格深色 surface/border 和蓝色强调：`#000000`、`#0d1117`、`#161b22`、`#30363d`、`#58a6ff`、`#c9d1d9`（`docs/reference/coomi/ui/tcss/coomi.tcss:3-17,24-32,64-79,90-115`）。

主区域尺寸明确：历史 `1fr`、预览 `auto` 且最多 10 行、状态栏 2 行、输入框 5 行（`coomi.tcss:25-29,55-69,73-79`）。动态面板使用 `auto + max-height`，避免挤占整个消息区（`coomi.tcss:35-52,117-133`）。Modal 使用居中容器、明确宽度、最大高度和滚动（`coomi.tcss:85-115,135-216`）。

## 3. 启动与屏幕组合

`CoomiApp.compose()` 返回空列表，并在 `on_mount()` 中 `push_screen(MainScreen(...))`，所以长期 Widget 树只有 MainScreen 一个所有者（`docs/reference/coomi/ui/textual_app.py:309-317`）。随后 App 装配 config/provider/tool/memory/AgentLoop，建立状态上下文并创建 Session（`textual_app.py:318-350`）。欢迎信息通过 `call_after_refresh` 延后到 Screen 可查询之后写入日志（`textual_app.py:352-353`）。

固定骨架的价值是：流式运行中不会反复创建输入框或状态栏；动态交互只插入到骨架指定位置。它也降低了 CSS 选择器和焦点对象不一致的机会。

MiniAgent 曾尝试用 `ChatScreen` 组合 UI，但已完成计划明确记录：把 Screen 作为 App 子节点时焦点不能稳定交给嵌套输入框，最终由 App 直接 compose 才使 Pilot 的 Enter 提交稳定（`docs/design-docs/exec-plans/completed/textual-ui-implementation.zh-CN.md:20-25`）。当前运行入口确实由 `MiniAgentApp.compose()` 直接组合 viewport、按钮、状态栏和 Composer 外壳（`miniagent/ui/app.py:103-110`）。`miniagent/ui/screen.py:12-16` 仍保留一份未使用且结构不同的 `ChatScreen`，这是已归档修复计划明确未清理的旧代码（`docs/design-docs/exec-plans/completed/ui-rendering-repair.zh-CN.md:41-43`）。

因此应借鉴“单一组合所有者”，但不必照搬“App push MainScreen”；MiniAgent 现有 App 直接组合是经过仓库内测试决策形成的约束。

## 4. 事件生产与消费链路

### 4.1 Producer：AgentLoop

`AgentLoop.run_stream()` 在开始时把用户消息加入 Session、重置取消与循环检测，并可先产出压缩事件（`docs/reference/coomi/engine/loop.py:258-276`）。每轮 ModelCall：

1. 检查取消；
2. 流式读取 provider chunk；
3. 将 reasoning/content/usage 归一化为 `ReasoningChunk`、`TextChunk`、`UsageUpdate`；
4. provider 提示工具开始时先发一个无参数 `ToolStart`（`loop.py:283-317`）；
5. 收齐 tool call 后写 Assistant 消息，再为实际调用发带 arguments 的 `ToolStart`，随后 `ToolRunning`、`ToolDone` 或 `ToolCacheHit`（`loop.py:340-369`）；
6. 工具结果写回 Session，继续下一轮；无工具时写完整 Assistant 消息并返回（`loop.py:394-425`）。

取消点分布在 ModelCall 前、流式 chunk 间和工具执行后（`loop.py:283-287,299-303,396-399`）。LLM 异常被降级为非致命 `AgentError` 并向 Session 注入系统式 Assistant 消息后正常返回（`loop.py:318-338`）；达到迭代上限也写 Assistant 摘要并发非致命错误通知（`loop.py:427-455`）。

### 4.2 Consumer：Textual worker

`CoomiApp._run_agent()` 使用 `@work(exclusive=True)`，确保 Agent 消费在 Textual 异步 worker 中执行，且同一方法的新 worker会排斥旧 worker（`docs/reference/coomi/ui/textual_app.py:1217-1219`）。开始时初始化运行缓冲，查询四个固定 Widget，把用户输入写入历史，状态改为执行中，预览显示 Thinking，并启动 spinner（`textual_app.py:1220-1238`）。

消费循环按事件类型路由：

- `TextChunk` → 累加 `_stream_buffer`，更新 preview；
- `ReasoningChunk` → 累加 `_full_reasoning`；
- tool 事件 → 更新 `ToolCallBanner`，完成时写历史；
- usage/compression → 更新 StatusLine/StatusPanel；
- cancel/error → 写终态提示并退出当前 run（`textual_app.py:1248-1342`）。

流结束后补写未展示 reasoning、把完整 Markdown 写入历史、清空 preview，并可能处理取消时保存的 buffered input（`textual_app.py:1344-1371`）。`finally` 无条件停止 spinner、重置 running/cancel/banner/status/preview/input，再做统计和 memory extraction（`textual_app.py:1389-1412`）。

### 4.3 这条链路的设计含义

它是一条单方向的同步消费链：AgentLoop 只 yield；App 是唯一 UI 路由器；Widget 不调用 AgentLoop。事件本身不拥有历史恢复语义，真正历史仍在 Coomi Session 内。

MiniAgent 的对应链路更深：AgentLoop → SessionEngine 提交/SessionUpdate → `RuntimeSession._publish()` → `MiniAgentApp._on_update()` → `UiProjection.apply()` → viewport/status（`miniagent/ui/session_facade.py:146-172`；`miniagent/ui/app.py:210-234`）。这符合 MiniAgent 设计：Session Update 是可丢失通知，snapshot 是重建来源（`docs/design-docs/textual-ui.md:66,133-137`）。迁移时应在这条链路最后增加渲染调度，不应跳过 SessionEngine。

## 5. 历史、流式与状态区域分离

Coomi 把三类变化频率不同的内容物理分区：

- 历史：`SelectableRichLog`，只追加用户输入、完成的 reasoning、完成的 Markdown、工具终态和错误提示；
- 流式：`StreamingPreview`，只显示当前尾部文本、Thinking 或工具名；
- 状态：`StatusPanel`，只显示模型、上下文、累计 token、spinner 和执行模式。

相关布局见 `main_screen.py:49-57`，对应消费见 `textual_app.py:1227-1236,1255-1327,1356-1359`。

这种分离的关键收益：

- 高频 chunk 不修改历史 Widget 树；
- 未闭合 Markdown 不污染已完成历史；
- spinner 只刷新两行状态；
- 完成态有明确的“提交到历史”时刻。

代价是当前完整 Assistant 回复在流结束前只显示末尾 500 字符，且 preview 截取可能从 Markdown 结构中间开始（`streaming_preview.py:40-43`）。它是性能优先的预览，不是完整草稿投影。

MiniAgent 的设计目标不同：Draft AssistantMessage 本身进入 UI Projection，普通文本应在流式期间完整按 Markdown 显示，并缓存闭合块（`docs/design-docs/textual-ui.md:216-218`）。当前 `MarkdownBlockCache` 已按闭合块缓存（`miniagent/ui/render_cache.py:50-77`），但 viewport 仍对每次 update 重挂所有可见消息（`miniagent/ui/viewport.py:33-47`）。更合适的迁移是：保留完整 Draft 投影，同时把“当前 Draft 的高频刷新”节流/合并，并只刷新受影响消息 Widget；不必退化为仅显示最后 500 字符。

## 6. 流式预览与节流

`StreamingPreview.show_text()` 只覆盖 `_pending_text`。若当前没有 timer，则设置 0.05 秒 timer；timer 到期取最新 pending 值、截取最后 500 字符并更新 Markdown（`docs/reference/coomi/ui/widgets/streaming_preview.py:24-43`）。这是 trailing-edge coalescing：50ms 窗口内多个 chunk 合并为一次渲染。

切换到 thinking/tool 或 clear 时停止 timer 并清理 pending，避免过期文本覆盖新状态（`streaming_preview.py:45-64`）。但 `_cancel_throttle()` 自身不清 `_pending_text`；`show_thinking()` 和 `show_tool()` 也不清 pending，只有 `clear_preview()` 清理（`streaming_preview.py:45-63`）。由于下一次 `show_text()` 会覆盖 pending，这通常不造成显示回滚，但残留状态使行为依赖下一次事件顺序，迁移时应显式清理。

MiniAgent 当前没有类似渲染合并：`RuntimeSession._publish()` await 每个 callback，App callback 立即 apply 和 refresh（`miniagent/ui/session_facade.py:146-151`；`miniagent/ui/app.py:210-218`）。推荐在 UI 边界实现每帧或 30-50ms 合并：保留最新 projection dirty-set，单一 timer/call_later 刷新；完成、错误、取消、Session 切换属于必须立即 flush 的边界事件。

## 7. Tool 生命周期

Coomi producer 会对同一个调用产生两次 `ToolStart`：provider 首次声明工具名时无参数，收齐调用后再带完整参数（`docs/reference/coomi/engine/loop.py:305-317,340-369`）。UI 用 `_active_banners: dict[str, ToolCallBanner]` 按工具名复用 banner，第二次事件补参数；`ToolRunning` 改状态，`ToolDone/ToolCacheHit` pop 并把 Rich Table 写入历史（`docs/reference/coomi/ui/textual_app.py:1277-1314`）。取消时所有活跃 banner 标为 cancelled 并写历史（`textual_app.py:1414-1419`）。

这套生命周期表达清晰，但关联键是 `tool_name`，而不是调用 ID（`textual_app.py:1280-1286,1299-1311`）。同名调用一旦并发或事件交叠，就可能共用/弹出错误 banner。ToolDone 自带 `elapsed`，UI 实际使用 banner 本地计时，并没有消费事件 elapsed（`docs/reference/coomi/ui/events.py:39-43`；`tool_call_banner.py:33-40`）。Banner 还保留 `_expanded`，却没有发现切换它的交互路径（`tool_call_banner.py:28,76-86`）。

MiniAgent 使用 `tool_use_id` 关联 ToolUse 和 ToolResult，同时按原 ToolUse 顺序合并结果，完成顺序不改变布局（`miniagent/ui/projection.py:123-156`；设计要求 `docs/design-docs/textual-ui.md:226-228`）。这是更正确的模型，必须保留。可迁移的是“进行中用轻量态、完成后冻结终态”和“取消时显式完成 UI 状态”，不应迁移按工具名关联。

## 8. Reasoning 生命周期

Coomi 把 ReasoningChunk 全部累加到 `_full_reasoning`，记录首块时间；首个 TextChunk 到达且正式文本尚为空时，才把 reasoning 写为带耗时的折线块，然后清空缓冲（`docs/reference/coomi/ui/textual_app.py:1255-1275`）。如果没有后续 TextChunk，流结束时有兜底写入（`textual_app.py:1344-1354`）。

`Ctrl+R` 只翻转 `_reasoning_visible`（`textual_app.py:1203-1205`）。它不重新渲染已经写入 RichLog 的 reasoning，也不保留可展开 Widget，因此“切换显示/隐藏”只影响尚未提交的 reasoning，不能折叠/展开历史块。这与描述上的全局 reasoning toggle 不完全一致。

MiniAgent 将 reasoning 保留为有序 `UiPart`，渲染器有折叠预览和展开正文（`miniagent/ui/projection.py:176-184`；`miniagent/ui/renderers/message.py:60-74`），模型更强。但当前 `render_message()` 调用没有传入 per-message `reasoning_expanded` 状态，viewport 也没有 reasoning 点击/按键切换路径（`miniagent/ui/viewport.py:41-43`）。因此数据/渲染能力存在，交互闭环仍不完整。

## 9. 交互状态机与动态挂载

### 9.1 状态机

Coomi 用 `_interactive_mode` 统一表示 `none | command | question | model_picker | context_picker`，并同步旧的 command/question 布尔标志（`docs/reference/coomi/ui/textual_app.py:279-285,645-650`）。App 注册 priority bindings，让方向键、Enter、Esc 在 TextArea 之前检查（`textual_app.py:216-225`）。`check_action()` 根据 mode 决定拦截或放行，然后 action 方法把按键路由给当前面板（`textual_app.py:651-729`）。

它避免多个临时 Widget 同时处理同一按键，也让输入框仍可服务 question 的 Other 文本。问询模式的左右键还有“Other 已选但未填时禁止切题”的约束（`textual_app.py:685-694`）。

状态机缺点是大量代码访问面板私有成员，如 `_is_other_selected`、`_other_texts`、`_active_q`（`textual_app.py:688-693,773-782`），App 与 Widget 内部表示紧耦合。

### 9.2 动态挂载

PlanPanel、CommandList、ModelPicker、ContextPicker 均通过 `await self.screen.mount(panel, before=log)` 插入历史日志之前，结束后 `remove()`（`textual_app.py:833-869,889-906,910-935,962-981`）。因此长期骨架不变，临时交互占据历史区上方，并可由 TCSS 控制最大高度。

这是值得迁移的局部模式：命令补全等瞬时 overlay 不应写成历史文本。但 MiniAgent 已经为 model/session 使用 ModalScreen（`miniagent/ui/modals/model_picker.py:11-26`；`session_picker.py:14-32`），这更符合其设计文档。只需要为 slash completion、permission/reasoning 等缺口选用一致的 overlay/modal 机制，不必建立与 Coomi 相同的全局字符串状态机。

## 10. 输入行为

Coomi `PromptTextArea` 显式绑定 Ctrl+Enter 为换行，拦截裸 Enter，trim 后发布 Submitted（`docs/reference/coomi/ui/widgets/prompt_text_area.py:28-50`）。复制、粘贴、剪切、撤销、重做也显式绑定（`prompt_text_area.py:28-35`）。

存在两处不一致：

1. MainScreen placeholder 写“Shift+Enter 换行”，实现实际是 Ctrl+Enter（`docs/reference/coomi/ui/screens/main_screen.py:54-56`；`prompt_text_area.py:28-40`）。
2. `_on_text_submit()` 的 `/clear` 分支直接调用异步 `_handle_clear()` 而未 await 或 create_task（`docs/reference/coomi/ui/textual_app.py:1107-1109`；定义在 `1149-1166`），该路径会产生未等待 coroutine，清理不会按预期执行。旧 `on_input_submitted()` 路径则正确 await（`textual_app.py:1030-1032`）。

MiniAgent Composer 拦截 Enter 并在有文本时提交、随即 clear；Ctrl+C/Escape 发布取消请求（`miniagent/ui/composer.py:23-35`）。它没有显式 Ctrl+Enter binding，也没有 Tab completion 路由。虽然 `commands.py` 有 `complete_command()`，当前 App 仅在提交时 parse command，没有命令列表/补全 UI（`miniagent/ui/commands.py:13-27`；`miniagent/ui/app.py:122-138`）。这与设计要求的 Ctrl+Enter、Tab 和 Escape 补全行为仍有距离（`docs/design-docs/textual-ui.md:256-268`）。

## 11. Modal 与设置界面

Coomi 的 Command Palette、Settings、Provider List/Edit 是真正的 ModalScreen，使用 callback 接收结果并串联下一层 Modal（`docs/reference/coomi/ui/textual_app.py:357-402`）。Modal 的作用域明确：它们拥有自己的 bindings、focus 和 dismiss 结果，不污染主屏幕交互模式。

MiniAgent 已有 ModelPickerModal 和 SessionPickerModal（`miniagent/ui/modals/model_picker.py:11-26`；`session_picker.py:14-32`）。但当前 model picker 的数据只来自 `_model_name()`，最多一个当前模型或“未配置模型”，没有打开时向 Provider 列表接口拉取模型（`miniagent/ui/app.py:244-252`）。这不满足设计“每次打开直接调用 Provider 列表、不做应用级缓存”的目标（`docs/design-docs/textual-ui.md:273-276`）。

Session picker 会在打开时扫描 repository，符合按需加载；切换时先 open replacement，再 stop current（`miniagent/ui/app.py:188-207`）。但 `RuntimeSession.open()` 会立即 `_start_worker()`（`miniagent/ui/session_facade.py:87-99`），因此 replacement worker 在旧 current 停止前已经启动，短暂违反“最多一个活动 SessionEngine worker”的意图。正确的预打开应只取得并验证 writer lock/snapshot，直到旧 worker 停止后再启动目标 worker。

## 12. 错误、取消与恢复

### 12.1 Coomi

AgentLoop 把 provider 异常和迭代上限变成非致命 AgentError，让 UI 显示警告并允许继续（`docs/reference/coomi/engine/loop.py:318-338,427-455`）。UI 根据 `is_fatal` 选择错误/警告文本，然后明确提示用户可以继续输入（`docs/reference/coomi/ui/textual_app.py:1334-1342`）。未分类 UI 异常按连接、超时、认证或一般错误显示，并在 finally 恢复状态（`textual_app.py:1373-1396`）。

取消通过 CancelToken 多处检查；App 取消时还可把输入框现有文本放进 token buffer，当前 run 结束后作为下一次输入继续（`textual_app.py:1170-1183,1361-1369`）。空闲 Esc 采用 2 秒双击退出，状态栏和 placeholder 同时提示（`textual_app.py:1185-1201`）。

风险：

- AgentError message 包含异常字符串、绝对源码路径、Session ID 和执行统计，并被 UI 原样显示（`loop.py:320-330,445-455`；`textual_app.py:1334-1341`）；这不符合 MiniAgent 禁止暴露内部路径、协议和供应商错误堆栈的约束。
- App 的 fallback 也把 `{e}` 原样写进日志（`textual_app.py:1373-1388`）。
- 多处 `except Exception: pass` 会把 Widget 查询、mount/remove 和 refresh 故障静默吞掉，例如 `textual_app.py:835-839,892-897,920-926`。这有助于 finally 恢复，但会隐藏 UI 不显示的根因。
- `_handle_clear()` 创建新 Session 后直接删除旧 Session（`textual_app.py:1149-1166`），其持久化语义与 MiniAgent 的可恢复 Journal/Current Session 模型不兼容。

### 12.2 MiniAgent

`RuntimeSession.stop()` 设置 stopping、取消 active、取消并 await worker，最后关闭 SessionEngine（`miniagent/ui/session_facade.py:131-144`）；App 用 transition lock 串行化 quit/clear/switch（`miniagent/ui/app.py:173-207`）。这比 Coomi 的纯 UI flag 更接近可验证的生命周期边界。

但 `RuntimeSession._publish()` 顺序 await UI callbacks（`session_facade.py:146-151`），而 callback 会同步触发全 viewport 重建（`app.py:210-218`）。因此 UI 渲染开销可能反向给 SessionEngine update 发布施加背压。另一方面 App 的 `_refresh_view()`、状态更新又宽泛吞异常（`app.py:214-241`），会把实际渲染错误变成“状态已提交但界面没变化”。建议渲染调度与 Session update 回调解耦，并把异常写入安全 trace/测试可观察通道，而不是静默 pass。

## 13. 数据与视图边界

Coomi 的 `StatusLine` 自称纯数据持有层，StatusPanel 持有引用并 render（`docs/reference/coomi/ui/status_line.py:1-4,60-72`；`status_panel.py:25-35,92-142`）。但 `StatusLine.set_context_window_size()` 会直接写 `.coomi/state.json`（`status_line.py:18-45,104-107`），因此它并非纯内存 view model，而是混合了持久化副作用。

Coomi 的其余“immutable render”小组件确实保持简单：状态字段变化 → `refresh()` → `render()` 生成新的 Rich Table，如 PlanPanel（`plan_panel.py:1-5,100-123`）和 StatusPanel（`status_panel.py:38-92`）。这种模式适合有限选项小组件，不适合长历史。

MiniAgent 的 `UiProjection` 是更严格的非权威 reducer：snapshot 完整 replace；Session Update 通过 message/part/tool ID 确定性 upsert、delta、discard 和合并工具结果（`miniagent/ui/projection.py:52-85,85-121,123-176`）。设计文档也明确 UI Projection 不可反向修改 Journal（`docs/design-docs/textual-ui.md:322-343`）。这条边界是 MiniAgent 架构的核心，不应为了获得 Coomi 的简洁渲染而削弱。

## 14. 当前 MiniAgent 与 Coomi 的架构对照

| 维度 | Coomi | MiniAgent 当前 | 判断 |
| --- | --- | --- | --- |
| 组合所有者 | App push MainScreen，Screen 组合五区（`textual_app.py:309-317`; `main_screen.py:49-57`） | App 直接组合三段及 Composer 外壳（`miniagent/ui/app.py:103-110`） | MiniAgent 现状有已验证焦点原因，应保留单一 App 组合；删除/归档旧 ChatScreen |
| 事实来源 | Coomi Session，UI worker同时写历史显示 | SessionEngine/Journal；UI 仅 snapshot/update 投影（`session_facade.py:50-57,110-119`） | MiniAgent 更强，必须保留 |
| 流式显示 | 独立 preview，50ms/500 chars，结束写 RichLog | Draft 在 Projection，闭合 Markdown block cache | 保留完整 Draft，迁移节流和局部刷新 |
| 历史渲染 | RichLog append-only | Visible slice 的 Static Widget | RichLog 简单稳定，但不满足 MiniAgent 结构化展开与恢复；不可直接替换 |
| 刷新粒度 | preview/status/历史分别更新 | 每 update remove/mount 可见切片 | 当前 MiniAgent 粒度过粗 |
| 工具关联 | tool_name | tool_use_id | MiniAgent 正确 |
| reasoning | 运行内字符串，提交后不可真正折叠 | 有序 UiPart 与折叠渲染 | MiniAgent 模型正确，缺交互状态 |
| 临时交互 | inline mount + 全局模式机；设置用 Modal | model/session Modal；命令补全缺失 | 按交互性质组合 overlay 与 Modal |
| worker | Textual `@work(exclusive=True)` 每次 run | RuntimeSession 唯一 asyncio Task 持续消费队列 | MiniAgent 更符合 Current Session 领域模型 |
| 错误 | 直接写日志，可能泄漏异常/路径/ID | 终态投影 + notify，但刷新异常静默 | 保持安全映射，增加可观察性 |
| CSS | 硬编码 GitHub 深色，多区稳定尺寸 | token 化主题，三段式固定布局 | 不需要复制颜色；可借鉴区域尺寸约束 |

## 15. 已发现的不一致与缺陷

### 15.1 Coomi 参考实现

1. 输入提示 Shift+Enter 与实际 Ctrl+Enter 不一致（`main_screen.py:54-56`; `prompt_text_area.py:28-40`）。
2. TextArea 提交路径 `/clear` 漏 await，和旧 Input 路径行为不一致（`textual_app.py:1030-1032,1107-1109,1149-1166`）。
3. 工具活动态按 `tool_name` 而不是调用 ID 关联，不支持同名交叠（`textual_app.py:1277-1313`）。
4. ToolCallBanner 的 expanded 状态没有可见的切换入口（`tool_call_banner.py:28,76-86`）。
5. Ctrl+R 只影响未来 reasoning 提交，不能切换历史块（`textual_app.py:1203-1205,1255-1267`）。
6. preview 直接截尾 500 字符，可能切断围栏代码或 Markdown 结构（`streaming_preview.py:40-43`）。
7. AgentError/UI fallback 原样暴露异常、源码绝对路径和 Session ID（`loop.py:320-330,445-455`; `textual_app.py:1334-1341,1373-1388`）。
8. `StatusLine` 名称上是纯数据，实际直接持久化状态文件（`status_line.py:1-4,36-45,104-107`）。
9. App 大量访问 Widget 私有字段，并广泛吞异常，降低边界与可诊断性（`textual_app.py:688-693,773-782,835-868`）。
10. MainScreen 内 CSS 与全局 TCSS 都定义 prompt 样式，形成双重样式来源（`main_screen.py:26-37`; `coomi.tcss:72-83`）。

### 15.2 MiniAgent 当前实现

1. `ChatScreen` 是未使用、且与 App 当前 DOM 不一致的第二份布局定义（`miniagent/ui/screen.py:12-16`; `miniagent/ui/app.py:103-110`）。
2. `MessageViewport.refresh_projection()` 每次 update remove 全部子节点再 mount，未按 dirty message 局部刷新（`miniagent/ui/viewport.py:33-47`）。
3. “虚拟化”用固定高度 3 新建临时 index，只 mount 切片但不保留未挂载区高度；scroll/resize 不重新计算 projection 窗口（`viewport.py:33-43,54-60`）。这与动态高度/锚点目标不一致。
4. 没有 UI refresh throttle；Session callback 被同步 await，可能把渲染成本传回更新发布链（`session_facade.py:146-151`; `app.py:210-218`）。
5. reasoning 渲染支持 expanded 参数，但 viewport 没有展开状态或交互传参（`renderers/message.py:25-35,52-74`; `viewport.py:41-43`）。
6. command completion 函数存在，Composer/App 没有 Tab 或 inline list 集成（`commands.py:24-27`; `composer.py:23-35`; `app.py:122-138`）。
7. model picker 没有按打开动作获取 provider 模型列表，只显示当前模型（`app.py:130-134,244-252`）。
8. `RuntimeSession.open()` 立即启动 replacement worker，App 随后才 stop current，切换瞬间可能有两个 worker（`session_facade.py:87-99`; `app.py:199-207`）。
9. `_refresh_view()`、状态更新都静默吞异常，可能正是“事实更新了但 UI 不显示”的诊断盲区（`app.py:214-241`）。
10. 已完成视觉修复只证明两个尺寸下单一 Composer、边框和占位几何；它没有证明实际 provider 流、长历史虚拟化、动态 reasoning 或高频 update 的显示正确。该计划自己记录的是 39 个 UI 测试、170 个全量测试和导出帧，而本次未重跑（`docs/design-docs/exec-plans/completed/ui-rendering-repair.zh-CN.md:16,39-43,84-93`）。

## 16. 对 MiniAgent 的迁移建议

### 优先级 P0：先建立可观察的渲染路径

1. 去掉 `_refresh_view()` 等路径的静默吞异常，改为安全日志/trace，并在 UI 测试中让异常失败。用户区仍只显示短错误，不暴露原异常。
2. 给 `SessionUpdate → UiProjection → Viewport` 增加可测试计数：update 类型、dirty message IDs、计划刷新次数、实际刷新次数。不要记录文本、arguments、路径或 Session 内容。
3. 增加 Textual Pilot 场景，覆盖多个 AssistantPartDelta、高频流、工具、reasoning、取消和 resize。先证明是 update 没到、projection 错、还是 viewport 没重绘。

理由：当前宽泛 `except Exception: pass` 会隐藏根因，直接重构 UI 可能只改变症状（`miniagent/ui/app.py:214-241`）。

### 优先级 P1：迁移 Coomi 的渲染调度，而非数据模型

1. 在 App/UI adapter 内增加单一 30-50ms refresh scheduler，合并普通 draft delta；complete/discard/error/cancel/switch 立即 flush。
2. `UiProjection.apply()` 已返回 dirty message set，应真正使用返回值，而不是丢弃（当前 `miniagent/ui/app.py:210-212`）。
3. 为每个挂载消息建立 keyed Widget；dirty set 只更新对应 renderable。只有 snapshot replace、排序/可见范围变化时才调整子树。
4. StatusBar 继续独立刷新，spinner timer 不触碰消息区；这对应 Coomi 的 preview/status 分区原则。

### 优先级 P1：修正虚拟滚动

1. `VirtualLayoutIndex` 应长期存在于 viewport，而不是每次 refresh 用默认高度重建。
2. 记录每个 message 的测量高度，并在流式增长、Markdown 换行、reasoning 展开、resize 后更新。
3. 为未挂载前缀/后缀提供 spacer/虚拟画布高度，使 `scroll_y` 覆盖完整历史。
4. `watch_scroll_y` 和 resize 必须基于当前 projection 重新计算可见范围；保存 `(message_id, intra-message line offset)` 锚点。
5. 添加数千条消息、首尾滚动、宽度变化、流式尾部增长的几何测试，落实设计文档 `textual-ui.md:244-248,334-336`。

### 优先级 P1：保持固定骨架与单一所有者

1. 保留 `MiniAgentApp.compose()` 作为经过测试的 DOM 所有者；不要恢复嵌套 ChatScreen。
2. 删除或明确废弃 `miniagent/ui/screen.py`，避免未来修复改错布局定义。
3. 继续保留固定三段及单一 Composer 外壳；已完成修复计划证明清除 TextArea 四边默认 border 后再恢复顶部边线是必要的（`docs/design-docs/exec-plans/completed/ui-rendering-repair.zh-CN.md:18-35`）。

### 优先级 P2：补齐交互闭环

1. 为 command completion 增加 inline overlay/list 和明确的 mode/focus owner；Tab 补全、上下选择、Esc 关闭，未知 slash 仍提交普通消息。
2. 为 reasoning 建立 `message_id -> expanded` 的纯 UI 状态，点击/键盘只刷新对应消息。
3. 保持 model/session 为 Modal；model picker 每次打开调用 provider list。
4. 将 Session 切换拆为“预打开不启动 → 停旧 worker → 启目标 worker → snapshot replace”，保证任意时刻一个 worker。
5. Composer 显式绑定 Ctrl+Enter，避免行为依赖 TextArea 版本默认绑定；UI 文案与 binding 用同一常量或测试约束。

### 优先级 P2：工具与错误展示

1. 保留 `tool_use_id` 内部关联及原顺序，不显示 ID；继续使用敏感字段过滤（`miniagent/ui/renderers/tool.py:16-47,65-80`）。
2. 工具进行中状态只刷新对应 tool part，完成/取消时冻结终态。
3. 错误从结构化 StopReason/ErrorInfo 映射为短用户文案；原异常只进脱敏 trace。不要复制 Coomi 原样输出 exception/path/session ID。

## 17. 建议的验证矩阵

这些是后续修复应运行的验证，不是本次已执行结果：

| 场景 | 观察点 |
| --- | --- |
| 空启动 80×24、111×39 | 单一 Composer、固定三段、焦点在输入区 |
| 每 1-5ms 发布 500 个文本 delta | UI 不阻塞；刷新次数显著少于 delta；最终文本完整 |
| 流式 fenced code + resize | Markdown 不丢块、不重复；锚点稳定 |
| 1000+ 历史消息从尾滚到首 | 虚拟总高度正确；可到达所有消息；挂载 Widget 数受限 |
| 离开底部后继续流式 | scroll 不被拉回；“新内容”入口稳定；点击返回底部 |
| reasoning 展开/折叠 | 只刷新目标消息；高度索引和锚点同步 |
| 两个同名工具调用 | 依 tool_use_id 独立展示；顺序不因完成顺序改变 |
| cancel/error/provider unavailable | draft 终态明确；输入恢复；状态栏回 idle；无秘密/路径/堆栈 |
| Session 切换失败/成功 | 失败保留旧 current；成功全过程最多一个 worker；旧锁释放 |
| `/model`、`/session`、补全 overlay | Modal/overlay 焦点和 Esc/Enter 路由一致 |

按照仓库要求，代码修改后还应执行：

```text
uv run python -m compileall miniagent tests main.py
uv run python -m pytest -q
```

## 18. 最终判断

Coomi 提供了一个很好的“稳定 TUI 渲染基线”：固定 Widget 骨架、append-only 历史、独立且节流的当前预览、独立状态栏、后台 worker、清晰的交互模式和按需挂载。其界面稳定性来自减少高频更新触及的 UI 面积。

MiniAgent 不应该整体移植 Coomi。MiniAgent 已经有更正确的 SessionEngine、Journal、snapshot/update、UiProjection、tool_use_id 和安全展示边界。真正需要做的是把 Coomi 的刷新节奏应用到 MiniAgent 的最后一公里：利用 projection dirty set、合并流式刷新、按消息局部更新、落实真实虚拟高度和滚动锚点，并消除吞异常造成的诊断盲区。

最小正确方向可以概括为：

```text
AgentLoop
  -> SessionEngine（唯一事实写入者）
  -> SessionUpdate / snapshot
  -> UiProjection（确定性 reducer，返回 dirty IDs）
  -> UI refresh scheduler（30-50ms 合并）
  -> keyed visible message widgets + independent StatusBar
```

这条路径同时保留 MiniAgent 的领域完整性和 Coomi 的 TUI 渲染稳定性。
