# 修复 MiniAgent Textual UI 的重复输入区与默认边框

本 ExecPlan 是持续维护的实现计划，遵守仓库根目录 `PLANS.md`。它修正已归档计划 `docs/design-docs/exec-plans/completed/ui-visual-theme-implementation.zh-CN.md` 未被测试捕获的终端渲染问题。

## Purpose / Big Picture

完成后，从仓库根目录运行 `uv run python -m miniagent.ui` 时，界面稳定呈现一个连续文档流、单行状态栏和唯一的五行 Composer。Composer 只显示顶部细分隔线，不继承 Textual `TextArea` 的其余三条默认边框；占位文字始终覆盖在 Composer 内，终端 resize 后不会出现第二个输入区或旧帧残影。自动化测试会检查组件数量、几何关系、计算后的四边样式和至少两种终端尺寸。

## Progress

- [x] (2026-07-24) 复核用户截图、当前源码、Textual 0.89.1 API 与官方布局、主题和测试文档。
- [x] (2026-07-24) 建立无头几何探针，确认当前 DOM 只有一个 Composer，但计算样式仍保留右、下、左三条 `tall` 默认边框。
- [x] (2026-07-24) 增加 Composer 外壳回归测试；旧实现因右边框仍为 `tall` 按预期失败。
- [x] (2026-07-24) 将应用样式提取到 TCSS，固定三段式布局并为布局容器设置不透明背景。
- [x] (2026-07-24) 简化主题初始化，TCSS 只使用 Textual 标准主题变量。
- [x] (2026-07-24) 聚焦 UI 测试 39 passed，compileall 无错误，全量 pytest 170 passed。

## Surprises & Discoveries

- Observation: `TextArea.DEFAULT_CSS` 对普通态和 focus 态都设置完整 `border`；只覆盖 `border-top` 不会清除另外三边。
  Evidence: 当前无头应用的计算样式为 top=`solid`，right/bottom/left=`tall`。
- Observation: 当前测试只验证主题名称、占位符显示状态和 spinner，没有断言最终几何或计算样式。
  Evidence: `tests/ui/test_app_lifecycle.py` 中不存在 Composer 数量、region 包含关系或 border side 断言。
- Observation: Textual 的最终 SVG 帧可以作为无需新增依赖的渲染级补充探针。
  Evidence: 111×39 导出帧中 `输入消息` 只出现一次，主题名为 `miniagent`。

## Decision Log

- Decision: 保留一个局部 `Container` 承载 Composer 和占位 `Static`，用显式 layer 与绝对 offset 叠放。
  Rationale: Textual 0.89.1 的 `TextArea` 构造器没有 placeholder 参数；官方 `position: absolute` 语义明确以父容器左上角为坐标原点。
  Date/Author: 2026-07-24 / Codex
- Decision: TCSS 只引用 `$background`、`$surface`、`$panel`、`$primary`、`$text-muted` 等标准主题变量。
  Rationale: 这样样式表可在自定义主题注册前安全解析，并在主题切换后由 Textual 正常更新，无需直接改写 `stylesheet` 的变量快照。
  Date/Author: 2026-07-24 / Codex
- Decision: 本次不引入新的截图测试依赖，先用 Textual 官方 `run_test(size=...)` 对最终几何和计算样式做确定性验收。
  Rationale: 当前工作区已有大量未提交改动；几何与 border 断言能精确捕获本次缺陷，避免为一次聚焦修复扩大依赖面。视觉快照依赖可作为后续独立增强。
  Date/Author: 2026-07-24 / Codex

## Outcomes & Retrospective

修复完成。`MiniAgentApp` 通过 `miniagent/ui/miniagent.tcss` 加载确定性三段式布局，Composer 在普通态和 focus 态都先清除完整默认边框再恢复顶部细线。占位层位于固定五行、不透明的 Composer 外壳中，80×24 与 111×39 resize 回归测试均证明只有一个 Composer 且占位层未逸出。

验收结果为 UI 测试 39 passed、全量测试 170 passed、compileall 无错误。额外导出的 111×39 最终 SVG 帧只包含一次占位文案。没有增加依赖；未处理与本任务无关的旧 `ChatScreen` 清理。

## Context and Orientation

`miniagent/ui/app.py` 的 `MiniAgentApp` 是运行入口并组合三个纵向区域：`MessageViewport`、`StatusBar` 和 Composer 外壳。`miniagent/ui/composer.py` 的 `Composer` 继承 Textual `TextArea`，因此自动获得内置的完整边框样式。`miniagent/ui/theme.py` 定义自定义主题和 Rich 命名样式。结构与行为权威是 `docs/design-docs/textual-ui.md`，视觉权威是根目录 `DESIGN.md`。

当前 CSS 内嵌在 `MiniAgentApp.CSS` 中。主题构造阶段通过 `set_reactive` 和 `stylesheet.set_variables` 预先修改 Textual 状态，挂载阶段又再次注册和选择主题。修复后，布局 CSS 存放在 `miniagent/ui/miniagent.tcss`，应用通过 `CSS_PATH` 加载；主题在 `on_mount` 使用公开 API 注册和选择。

## Plan of Work

先在 `tests/ui/test_app_lifecycle.py` 增加一个参数化外壳测试。在 80×24 和 111×39 下启动应用，断言只有一个 Composer、Composer 占满固定五行外壳、占位 Static 完全位于 Composer region 内，并断言未聚焦与聚焦后的右、下、左边框均为空而顶部为 `solid`。测试还会模拟 resize 后重复这些断言。

然后把 `MiniAgentApp.CSS` 移到 `miniagent/ui/miniagent.tcss`。Screen 明确使用纵向布局；消息区使用 `1fr`，状态栏固定一行，Composer 外壳固定五行且拥有不透明背景。Composer 先用 `border: none` 清除 TextArea 默认值，再为普通态和 focus 态分别恢复顶部边框。外壳声明 editor 与 placeholder 两层，占位 Static 使用绝对定位叠放。

最后删除构造函数中对 reactive 和 stylesheet 的提前改写。`on_mount` 注册并选择主题、注入 Rich 样式，然后聚焦 Composer。运行聚焦与全量验证，并把结果回写本计划。

## Concrete Steps

所有命令在 `D:\study\MiniAgent` 运行：

    uv run python -m pytest -q tests/ui/test_app_lifecycle.py
    uv run python -m compileall miniagent tests main.py
    uv run python -m pytest -q
    uv run python -m miniagent.ui

第一个命令应在样式修复前因残留 `tall` 边框失败，修复后通过。最终人工启动应只看到一个 Composer，且只有顶部细线。

## Validation and Acceptance

自动验收要求两种终端尺寸都只有一个 Composer；占位层始终位于 Composer 内；Composer 普通态和 focus 态只有顶部边框；resize 后条件保持成立。`compileall` 无错误，全量 pytest 全部通过。

人工验收运行 `uv run python -m miniagent.ui`，观察消息区无边框、状态栏一行、底部只有一个五行输入区域。输入文字后占位提示消失；调整终端大小后不出现重复框或残影。

## Idempotence and Recovery

TCSS 与测试改动可重复运行。现有工作区包含用户的其他未提交修改，修复不得重置、还原或重排无关文件。若样式表解析失败，聚焦测试会在应用挂载阶段直接失败并显示 TCSS 行号；修正该行后重新运行即可。

## Artifacts and Notes

修复前探针：

    region Region(x=0, y=34, width=111, height=5)
    top solid; right tall; bottom tall; left tall

修复后验证：

    uv run python -m pytest -q tests/ui
    39 passed in 1.39s

    uv run python -m pytest -q
    170 passed in 3.20s

    placeholder_occurrences=1
    theme=miniagent

## Interfaces and Dependencies

不增加运行时依赖。`MiniAgentApp.CSS_PATH` 指向同包下的 `miniagent.tcss`。`Composer`、`StatusBar`、`MessageViewport` 的 Python 公共接口保持不变。

Revision note (2026-07-24): 初始计划创建，用于修复已归档视觉计划漏掉的 Composer 边框、占位层和 resize 验收问题。

Revision note (2026-07-24): 完成 TCSS、主题初始化和回归测试修复，记录全量验收证据并归档计划。
