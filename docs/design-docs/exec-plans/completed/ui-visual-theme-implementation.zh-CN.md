# 实现 MiniAgent 终端 UI 视觉主题与流式 Markdown

本 ExecPlan 是一个持续维护的中文实现计划，遵守仓库根目录 `PLANS.md` 的规则。它面向没有本项目背景的实现者，说明如何把 `DESIGN.md` 定义的视觉系统落地到现有 Textual UI（`miniagent/ui/`），并补齐 `docs/design-docs/textual-ui.md` §9.1 要求的流式 Markdown 块缓存。

## Purpose / Big Picture

完成后，MiniAgent 终端界面从 Textual 默认主题变为一套自定义深色视觉系统：冷中性近黑底色、蓝色主强调、浅橙次强调、青色工具语义色，全部 token 集中在 `miniagent/ui/theme.py`。消息区是无边框的连续文档流：`You` 标签灰色、`MiniAgent` 标签蓝色加粗、reasoning 灰色斜体可折叠、工具行缩进两格并显示 ✓/✗ 终态、queued 消息整行灰斜体带橙色"排队中"标注。状态栏左侧显示 cwd、会话标题、模型名，右侧显示运行态（运行中 spinner、排队数、出错）。Assistant 的普通文本在流式期间渲染 Markdown，已闭合 block 的解析结果被缓存，只有末尾未闭合 block 被重复解析。

可观察结果：在 `D:\study\MiniAgent` 执行 `uv run python -m miniagent.ui` 能看到深色自定义主题；`uv run python -m pytest -q` 全部通过（含新增的块缓存、状态栏和渲染器测试）。

## Progress

- [x] (2026-07-23) 与用户完成视觉方向讨论并写入 `PRODUCT.md`、`DESIGN.md`；确认实现范围为主题 + 排版重构 + 流式 Markdown。
- [x] (2026-07-23) 新建 `miniagent/ui/theme.py`（调色板常量、Textual Theme、Rich 命名样式表），`MiniAgentApp` 注册主题并更新 CSS（移除失效的 `#chat` 边框规则与 Footer）。主题注册必须在 `__init__` 完成并重建样式表变量快照，见 Surprises。
- [x] (2026-07-23) 新建 `miniagent/ui/render_cache.py`：`split_closed_blocks`（代码围栏感知的闭合块切分）与 `MarkdownBlockCache`（闭合块只解析一次，末尾未闭合块每次重解析）。
- [x] (2026-07-23) 重构 `miniagent/ui/projection.py`：TOOL 角色的 ToolResultPart 按 `tool_use_id` 合并进 assistant 消息的对应 tool part，无法配对的保留为独立消息。
- [x] (2026-07-23) 重构 `renderers/message.py`、`renderers/status.py`、`status_bar.py`、`composer.py`、`viewport.py`（角色标签色彩分工、工具行缩进与终态字符、queued 样式、状态栏右侧运行态 spinner、Composer 占位提示、返回底部按钮）与 modal 样式 token 化。返回底部按钮的右下角定位由 `MessageViewport._reposition_new_content_button` 按尺寸换算，见 Surprises。
- [x] (2026-07-23) 修订 `docs/design-docs/textual-ui.md` §6 状态栏措辞（右侧运行态为已确认的新增）。
- [x] (2026-07-23) 新增/更新测试并运行 `uv run python -m compileall miniagent tests main.py` 与 `uv run python -m pytest -q` 全量验收：169 passed。

## Surprises & Discoveries

- 观察：`app.py` 现有 CSS 中 `#chat { border: solid $surface; }` 是失效规则——`MiniAgentApp.compose` 直接组合 viewport/status/composer，`#chat` 容器只存在于未被使用的 `ChatScreen` 中。删除该规则不改变现状渲染，但消除了误导。
  证据：`miniagent/ui/screen.py` 定义 `#chat`，`app.py` 的 `compose` 未挂载 `ChatScreen`（已完成计划记录的 Textual 焦点问题所致）。
- 观察：领域规则规定 `ToolResultPart` 只能属于 `Role.TOOL` 的独立消息（`miniagent/domain.py` 的 `Message.__post_init__` 校验），因此工具结果与工具调用在投影中天然是两条消息；展示层的"工具行内联在 assistant 块中"必须在投影或渲染层做配对。选择在投影层合并，因为 `textual-ui.md` §9.3 要求"完成顺序不改变布局"，投影层合并后渲染层无需跨消息查找。
  证据：`miniagent/domain.py:97-100`；`docs/design-docs/textual-ui.md` §9.3。
- 观察：Textual 0.89.1 的 `TextArea` 无 `placeholder` 参数，`Theme.__init__` 接受 `variables` 字典存放自定义 token（CSS 中以 `$名字` 引用）。占位提示用覆盖在 Composer 上层的 `Static` 实现。
  证据：`inspect.signature` 运行时检查。
- 观察（2026-07-23 实现期）：`App.__init__` 会用当时的默认主题快照一份 CSS 变量（`Stylesheet(variables=self.get_css_variables())`），`on_mount` 里注册主题太晚——启动解析类级 CSS 时 `$surface-2` 等自定义变量不存在，抛 `UnresolvedVariableError`。修复：`__init__` 中 `register_theme` + `set_reactive(App.theme, ...)`（跳过 watcher，App 尚未运行）+ `stylesheet.set_variables(get_css_variables())` 重建变量快照。
  证据：`textual/app.py:643`；修复前 `tests/ui/test_app_lifecycle.py` 两个用例均报 `reference to undefined variable '$surface-2'`。
- 观察（2026-07-23 实现期）：旧 CSS 里 `right: 2; bottom: 1` 是 Textual 不支持的属性——此前整个 `MiniAgentApp.CSS` 因解析失败被静默丢弃，应用一直在用默认主题运行（这正是"裸奔的 Textual 默认主题"反模式的实际成因）。Textual 的绝对定位由 `apply_absolute` 把部件原点重置到父容器左上角再叠加 `offset`，CSS 无法表达靠右/靠底；`align` 按全部子节点包围盒整体平移，也不能单独锚定一个子节点。最终由 `MessageViewport._reposition_new_content_button` 按父容器与按钮尺寸换算 offset，避免全屏透明容器遮挡视口鼠标事件。
  证据：`textual/layout.py:135`（`apply_absolute`→`reset_origin`）、`textual/_arrange.py:99-106`（align 整体平移）；修复前样式表报 `Invalid CSS property 'right'`。

## Decision Log

- Decision: 视觉 token 的权威是 `DESIGN.md`，代码中集中在 `miniagent/ui/theme.py`；渲染器使用 Rich 命名样式（`ui.*`、`markdown.*`），由 App 在挂载时 `console.push_theme` 注入。
  Rationale: 命名样式让调色板单点可改，且 Rich 的 `Markdown` 渲染器只认 `markdown.*` 命名样式，push_theme 是统一机制。
  Date/Author: 2026-07-23 / Claude
- Decision: 流式 Markdown 用 Rich `Group` 组合已缓存的 `rich.markdown.Markdown` 实例与末尾未闭合块的临时实例，而不是预先渲染成 `Text`。
  Rationale: `Markdown` 在构造时完成解析，缓存实例即缓存解析；`Group` 让 Textual 按实际宽度现场排版，终端宽度变化无需缓存失效。
  Date/Author: 2026-07-23 / Claude
- Decision: 状态栏右侧运行态（运行中/排队 n/出错）是对 `textual-ui.md` §6"只显示三项"措辞的修订，用户已在讨论中拍板；实现时同步修订该文档。
  Rationale: AGENTS.md 要求请求与设计文档冲突时显式决策；场景句"余光扫过知状态"需要该信息。
  Date/Author: 2026-07-23 / Claude（用户确认）
- Decision: 强调色最终定为 IDE 语法高亮家族（蓝 `#7AA2F7` 主、浅橙 `#E0A35E` 次、青 `#6FC3CF` 工具），否决了初稿的琥珀金。
  Rationale: 用户明确偏好 IDE 代码高亮式配色。
  Date/Author: 2026-07-23 / 用户

## Outcomes & Retrospective

(2026-07-23) 全部五个阶段完成，`uv run python -m compileall miniagent tests main.py` 无错误，`uv run python -m pytest -q` 169 passed（含新增 `tests/ui/test_render_cache.py` 8 例、`test_renderers.py` 渲染器与状态栏 11 例、`test_projection.py` 工具结果合并 4 例、`test_app_lifecycle.py` 主题/占位提示/spinner 3 例）。

实现期最重要的两个发现都记录在 Surprises：主题变量快照时机（必须在 `__init__` 注册并重建样式表变量）与 `right`/`bottom` 无效属性导致整份 CSS 被静默丢弃。后者解释了为什么应用此前一直是默认主题——本次修复后自定义深色主题真正生效。遗留项：`miniagent/ui/screen.py` 的 `ChatScreen` 仍是未挂载的旧代码，不影响运行，后续可在独立任务中删除。

## Context and Orientation

`miniagent/ui/` 是 Textual 终端 UI 模块。`app.py` 的 `MiniAgentApp` 是 composition root，直接组合 `MessageViewport`（`viewport.py`，可见区虚拟滚动的 `VerticalScroll`）、`StatusBar`（`status_bar.py`）、`Composer`（`composer.py`，`TextArea` 子类）和两个 Modal。`projection.py` 把 Session snapshot 与 `SessionUpdate` 归约为 `UiMessage`（含 `role`、`parts`、`lifecycle`）。`renderers/` 下的纯函数把 `UiMessage` 渲染为 Rich `Text`，目前样式为硬编码的 `dim italic`/`cyan`/`red`。

领域约束：`ToolUsePart` 属于 assistant 消息，`ToolResultPart` 只能属于 `Role.TOOL` 的独立消息（`miniagent/domain.py`）。`UiPart` 对工具调用保存 `name` 与 `tool_use_id`，对工具结果保存 `tool_use_id` 与 `is_error`。

视觉权威是仓库根目录 `DESIGN.md`（调色板、排版层级、布局、动效立场）；产品上下文是 `PRODUCT.md`。结构与行为权威是 `docs/design-docs/textual-ui.md`。工具链：Python 3.11、`uv`、Textual 0.89.1（已满足 Theme API ≥0.86，仅需提高 pyproject floor）。验证命令在仓库根目录运行：`uv run python -m compileall miniagent tests main.py` 与 `uv run python -m pytest -q`（不能用 `uv run pytest`，Windows 下会找不到本地包）。

## Plan of Work

第一阶段：主题基础设施。`pyproject.toml` 将 `textual>=0.70` 改为 `textual>=0.86,<1` 并运行 `uv lock`。新建 `miniagent/ui/theme.py`：调色板常量（`BG`、`SURFACE`、`SURFACE_2`、`TEXT`、`MUTED`、`ACCENT`、`ACCENT_2`、`TOOL`、`SUCCESS`、`ERROR`）、`MINIAGENT_THEME`（`textual.theme.Theme`，slots 映射 primary=ACCENT、secondary=TOOL、accent=ACCENT_2，variables 放 `muted`/`tool`/`surface-2`）、`RICH_STYLES`（`ui.label.user`/`ui.label.agent`/`ui.reasoning`/`ui.tool`/`ui.queued`/`ui.queued.tag`/`ui.error`/`ui.success`/`ui.meta` 与 `markdown.h1-h3`/`markdown.code`/`markdown.code_block`/`markdown.link`/`markdown.block_quote`/`markdown.hr`）、`MARKDOWN_CODE_THEME`（pygments `one-dark`，导入失败回退 `monokai`）。`MiniAgentApp.on_mount` 注册主题、设为当前主题并 push Rich 主题；CSS 全面改用 `$` 变量。

第二阶段：流式 Markdown。新建 `miniagent/ui/render_cache.py`：`split_closed_blocks(source) -> tuple[str, str]` 逐行扫描、跟踪 ``` 与 ~~~ 围栏，围栏外的空行是块边界，返回（已闭合前缀，未闭合尾部）；`MarkdownBlockCache.render(source) -> Group` 缓存闭合前缀对应的 `Markdown` 实例列表，前缀单调增长时只追加解析新增块，否则重建，尾部非空时追加临时 `Markdown` 实例。

第三阶段：投影配对。`UiProjection` 维护 `tool_use_id -> assistant message_id` 索引；`_from_message` 遇到 `Role.TOOL` 消息时，把每个 `ToolResultPart` 合并进已索引的 tool part（写入结果内容与 `is_error`），未配对部分保留为独立 `UiMessage`；`UiPart` 增加 `result: str | None` 字段区分"等待结果"与"已有结果"。`InputQueued`/`AssistantPartDelta` 等路径维护索引。

第四阶段：渲染与组件。`renderers/message.py` 输出 Rich renderable（`Text` 或 `Group`）：用户消息灰色 `You` 标签 + 原文（queued 时整行 `ui.queued` + 橙色 `排队中` 标注）；assistant 蓝色加粗 `MiniAgent` 标签，text part 走 `MarkdownBlockCache`，reasoning 折叠行 `▸` + 首个非空片段（`ui.reasoning`），tool part 缩进两格，按 `result` 是否为 `None` 显示 `▸`（进行中，`ui.tool`）或 `✓`（`ui.success`）/`✗`（`ui.error`）+ 工具名 + 经 `redact_sensitive` 的参数单行摘要；失败结果附首行摘要。`viewport.py` 持有按 `(message_id, part序号)` 键控的缓存表并随投影修剪，新增右下 `↓ 新内容` 按钮（离开底部时显示，点击回底）。`renderers/status.py` 拆为左段（cwd · 标题 · 模型，模型名 `ui.label.agent`）与运行态（`运行中`/`排队 n`/`出错`）。`status_bar.py` 改为左右两栏，运行中时用 `set_interval` 轮转 braille 帧。`composer.py` 关闭行号，App 侧叠加占位提示（空文本时显示 `输入消息，/ 打开命令`，`TextArea.Changed` 时切换）。Modal CSS token 化。

第五阶段：文档与验证。修订 `docs/design-docs/textual-ui.md` §6。新增 `tests/ui/test_render_cache.py`（围栏切分、缓存复用）、`test_renderers.py` 补充（标签样式名、工具终态、queued）、`test_projection.py` 补充（结果合并、未配对兜底）、`test_status` 相关用例；修复受影响旧测试。运行全量验证后回写本计划。

## Concrete Steps

全部命令在仓库根目录 `D:\study\MiniAgent` 运行：

    uv lock
    uv sync
    uv run python -m compileall miniagent tests main.py
    uv run python -m pytest -q
    uv run python -m miniagent.ui   # 人工观察主题、状态栏与流式渲染

## Validation and Acceptance

- 新增测试 `test_render_cache.py`：未闭合围栏内的空行不切块；闭合前缀单调增长时 `MarkdownBlockCache` 不重复解析已闭合块（用解析计数或实例身份断言）。
- `test_projection.py`：assistant 的 tool part 在 `ToolResultCompleted` 后携带结果与 `is_error`；孤立 tool 消息仍独立展示。
- `test_renderers.py`：assistant 标签使用 `ui.label.agent` 样式名；失败工具行含 `✗`；queued 消息含 `排队中`。
- 全量 `uv run python -m pytest -q` 通过；`compileall` 无错误。
- 人工运行 `uv run python -m miniagent.ui`：深底蓝强调主题、状态栏右段、Composer 占位提示可见；无密钥提交显示可读的模型不可用终态。

## Idempotence and Recovery

所有改动为新增文件与局部重构，`uv lock`/`uv sync` 可重复执行。若主题注册在旧 Textual 上失败，回退方式为还原 `pyproject.toml` 并重新 `uv lock`。

## Interfaces and Dependencies

    miniagent/ui/theme.py
        BG, SURFACE, SURFACE_2, TEXT, MUTED, ACCENT, ACCENT_2, TOOL, SUCCESS, ERROR: str
        MINIAGENT_THEME: textual.theme.Theme
        RICH_STYLES: dict[str, str]
        MARKDOWN_CODE_THEME: str
        apply_theme(app: App) -> None        # 注册、选中并 push Rich 样式

    miniagent/ui/render_cache.py
        split_closed_blocks(source: str) -> tuple[str, str]
        class MarkdownBlockCache:
            def render(self, source: str) -> Group

    miniagent/ui/projection.py
        UiPart 增加字段 result: str | None = None
        UiProjection 维护 tool_use_id 配对索引（replace/apply 全路径）

    miniagent/ui/renderers/message.py
        render_message(message, *, reasoning_expanded=False, md_caches: dict | None = None) -> Text | Group

    miniagent/ui/renderers/status.py
        render_status_left(cwd, session_title, model) -> Text
        render_run_state(state: RunState | None, frame: int) -> Text   # RunState: kind + count
