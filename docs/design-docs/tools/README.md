# 内置工具设计

本目录保存具体内置工具的稳定业务契约。框架级执行语义以 [`tool-registry-and-execution.md`](../tool-registry-and-execution.md) 为准，工具作者约定以 [`tool-design-guidelines.md`](../tool-design-guidelines.md) 为准。

## 工具索引

- [`glob`](glob.md)：在 workspace 目录树中按路径模式发现文件和目录。
- [`grep`](grep.md)：在 workspace 目录树中搜索 UTF-8 文件内容。
- [`read_file`](read-file.md)：按行分页读取已知 UTF-8 文件与受控结果引用。
- [`write_file`](write-file.md)：带哈希冲突保护地创建或完整替换 UTF-8 文件。
- [`read_docs`](read-docs.md)：通过 MinerU 把 PDF 或 Word 文档转换为受控 Markdown。
- [`todo_write`](todo-write.md)：替换 Current Session 的进程内结构化 TodoList。
- [`calculator`](calculator.md)：执行受限且确定性的数值表达式。
- [`web_search`](web-search.md)：通过 Tavily 返回普通 Web 搜索结果。

这些工具保持正交边界：`glob` 只发现路径，`grep` 按内容搜索，`read_file` 读取已知文本，`write_file` 只做整文件写入，`read_docs` 只把文档转换为 Markdown，`todo_write` 只管理进程内任务状态，`calculator` 不访问资源，`web_search` 不抓取网页全文或生成二次答案。外置 ToolOutput 与 MinerU DocumentRef 最终都由 `read_file` 分页读取。

## 共享文件搜索边界

`glob` 与 `grep` 共享一个不注册为工具的私有深模块：

```text
miniagent/tools/_filesystem_search/
  __init__.py
  patterns.py
  ignores.py
  walker.py
  models.py
```

- `patterns.py` 是受控 glob 方言的唯一编译器；
- `ignores.py` 使用 `pathspec` 解释分层 `.gitignore`；
- `walker.py` 提供稳定目录遍历、硬排除、Protected Workspace Subtree 跳过和 symlink/junction 跳过；
- `models.py` 只保存候选项和扫描统计等内部类型；
- 私有模块不定义 ToolSpec，不进入 ToolRegistry，也不投影给模型。

共享 glob 方言匹配相对于 ToolInput `path` 的完整路径。路径统一使用 `/`，模式区分大小写。`*`、`?` 和字符类不能跨 `/`；`**` 只可作为完整路径段，表示零个或多个目录层级。禁止 brace expansion、extglob、正则、否定规则、前导或结尾 `/`、空路径段以及 `.`、`..` 路径段。

硬排除优先于 `.gitignore`、`include_ignored` 和业务 include/exclude：

```text
.git/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.cache/
.tox/
.nox/
.venv/
venv/
*.pyc
```

`node_modules/`、`build/`、`dist/`、`coverage/`、`.idea/` 和 `.vscode/` 不属于硬排除，由 `.gitignore` 决定。符号链接和 Windows junction 不跟随，也不作为匹配项返回。workspace 根目录 `.mini/` 是 Protected Workspace Subtree：普通祖先扫描始终跳过；只有 ToolInput 显式把 `.mini` 或其后代作为搜索根目录时才产生 Permission Request。

`include_ignored=false` 时按目录层级应用 `.gitignore` 并尽早剪枝；`include_ignored=true` 时绕过 `.gitignore`，但不能绕过硬排除、Protected Workspace Subtree 或 symlink/junction 规则。

## 依赖边界

实现这些契约需要把以下依赖声明在 `pyproject.toml` 并由 `uv lock` 更新锁文件：

- `pathspec`：分层 `.gitignore`；
- `regex`：带单次匹配超时的正则搜索；
- `mpmath`：可控十进制有效精度的数值计算；
- `tavily-python`：Tavily 搜索 SDK。

不得用隐式系统命令或机器上偶然存在的 `git`、`rg`、PowerShell、浏览器替代这些契约。
