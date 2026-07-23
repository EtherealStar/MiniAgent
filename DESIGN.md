# Design

MiniAgent 终端 UI 的视觉系统。本文件补充 `docs/design-docs/textual-ui.md`（结构与行为权威），只定义视觉语言；两者冲突时以用户最新决策为准并回写两处。

## Theme

- 场景：深夜暗环境、半屏终端、连续一两小时的通用助手使用（写文档、周报、代码）；余光扫过即可感知运行状态。
- 结论：深色主题。配色策略 Restrained——IDE 语法高亮色彩家族（One Dark / Tokyo Night 一脉），蓝色主强调、浅橙次强调，中性灰铺底，语义色全套。

## Colors

所有正文色对 `bg` 对比度 ≥ 4.5:1。OKLCH 设计、hex 落地。

| Token | Hex | 职责 |
| --- | --- | --- |
| `bg` | `#1A1B20` | 冷中性近黑，全局底色 |
| `surface` | `#23252D` | 状态栏、modal、代码块的第二中性层 |
| `text` | `#E6E8EE` | 正文（≈14:1） |
| `text-muted` | `#989CA8` | 次要文字：You 标签、参数摘要、状态栏分隔（≈5.7:1） |
| `accent` | `#7AA2F7` | 蓝：MiniAgent 标签、模型名、选中态、focus 指示 |
| `accent-2` | `#E0A35E` | 浅橙：行内代码、warning、queued 标注 |
| `tool` | `#6FC3CF` | 青：工具名、运行中指示 |
| `success` | `#98C379` | 完成态（配 `✓`） |
| `error` | `#E06C75` | 失败终态（配 `✗` 与文字标签，≈5.4:1） |

色相分工即信息分工：蓝 = agent 身份与主操作，青 = 工具与进行中的动作，橙 = 代码与待处理，绿/红 = 终态。状态从不只靠色相，均配字符或文字。

## Typography

终端等宽单字号，层级五法：字重、颜色、缩进、留白、字符。

1. `You`：`text-muted`，不加粗。
2. `MiniAgent`：`accent` + bold——agent 的声音带品牌色。
3. 正文 Markdown：`text`；标题 bold；行内代码 `accent-2` 配 `surface` 底；代码块 `surface` 底 + 缩进 2。
4. Reasoning：`text-muted` italic；折叠行前缀 `▸`，展开体缩进 2。
5. 工具行：缩进 2；工具名 `tool`，参数摘要 `text-muted`；完成 `✓ success`，失败 `✗ error`。
6. Queued：整条 `text-muted` italic，尾部 `排队中` 标注用 `accent-2`。

## Layout

连续文档流，**消息区无任何边框**（删除 `#chat` 的 `border: solid $surface`），分区靠留白：

- 消息之间空一行；角色标签与正文之间不空行。
- 工具行、reasoning 展开体缩进 2 格（悬挂缩进），表达"agent 的工作过程"；agent 对你说的正文顶格。
- 状态栏一行：左侧 `cwd · 会话标题 · 模型名(accent)`，右侧运行态 `运行中 ⠋` / `排队 n` / 错误红字（对 `textual-ui.md` §6 的已确认修订，实现时回写该文档）。
- Composer：顶部一条 `surface` 细分隔线，不设完整边框；focus 时分隔线变 `accent`；占位文字 `输入消息，/ 打开命令` 用 `text-muted`。
- "↓ 新内容"按钮：右下角小胶囊，`accent` 文字，仅离开底部时出现。

## Motion

只保留表达状态的动：运行指示字符 spinner（`⠋⠙⠹…`，仅运行期间）。流式文字直接出现，滚动瞬时，modal 无过渡，无入场编排。

## Implementation Notes

- `pyproject.toml` 将 `textual` floor 提至 `>=0.86`（Theme API），注册自定义 Theme。
- 新建 `miniagent/ui/theme.py` 集中上表 token；`renderers/` 中的 `dim italic`/`cyan`/`red` 硬编码全部改为 token 引用。
- `render_cache.py` 随视觉重构补齐流式 Markdown block 缓存（设计文档 §9.1）。
