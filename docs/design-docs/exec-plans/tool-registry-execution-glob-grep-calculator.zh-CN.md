# 先完善工具注册与执行，再实现 glob、grep、calculator

本 ExecPlan 是一份持续维护的中文执行计划，遵循仓库根目录 `PLANS.md`。实现顺序固定为先补齐工具注册表与执行器的公共协议，再实现共享文件搜索基础设施，最后接入 `glob`、`grep` 和 `calculator`。执行者每完成一个里程碑都要更新本文件的 `Progress`、`Surprises & Discoveries`、`Decision Log` 和相应验收证据。

## Purpose / Big Picture

完成后，模型可以通过冻结且显式启用的工具注册表调用三个可靠的内置工具：在 workspace 中发现路径、搜索 UTF-8 文本内容、以及对受限数值表达式进行精确计算。用户可从工具调用结果看到稳定排序、截断提示、行号和精度安全的数值；越界路径、受保护目录、非法参数、结果过大和计算域错误都由统一执行协议处理，而不是由具体工具自行绕过框架。

## Progress

- [ ] 2026-07-24：已阅读 `AGENTS.md`、`PLANS.md`、工具设计指南、工具注册执行设计及目标工具契约；尚未修改实现。
- [ ] 补齐 `ToolRegistry`、`ToolSpec`、`ToolOutput`、目标解析/授权、执行与结果治理的公共协议。
- [ ] 建立 `_filesystem_search` 私有深模块及共享 glob 方言、忽略规则和稳定 walker。
- [ ] 实现并注册 `glob`。
- [ ] 实现并注册 `grep`。
- [ ] 实现并注册 `calculator`。
- [ ] 完成端到端、回归和全量验证，更新本计划的证据与复盘。

## Surprises & Discoveries

- Observation：当前设计将 `glob` 与 `grep` 的遍历规则集中到不注册为工具的 `miniagent/tools/_filesystem_search/`，避免两个工具各自实现一套路径安全语义。
  Evidence：`docs/design-docs/tools/README.md` 明确指定 `patterns.py`、`ignores.py`、`walker.py` 和 `models.py` 的职责。
- Observation：结果预算计算的是完整 `ToolOutput` 的规范 JSON，而不仅是 `content`。
  Evidence：`tool-design-guidelines.md` 与 `tool-registry-and-execution.md` 都要求 metadata/data 与 content 一起参与预算。
- Observation：用户要求先写计划且明确不读当前执行计划；本次只创建新的计划，不读取已有执行计划内容。
  Evidence：本次工作没有读取 `docs/design-docs/exec-plans/` 下已有文件。

## Decision Log

- Decision：注册表和执行器公共协议作为第一阶段，三个工具作为后续阶段。
  Rationale：工具契约要求严格 Pydantic schema、统一 targets、取消、重试和 ResultPolicy；先完成这些边界才能避免工具内部产生旁路。
  Date/Author：2026-07-24 / Codex
- Decision：`glob` 与 `grep` 复用私有 `_filesystem_search` 深模块，`calculator` 不引入资源 target。
  Rationale：共享搜索安全规则必须一致；纯计算没有文件、网络、Session 或外部服务副作用。
  Date/Author：2026-07-24 / Codex
- Decision：依赖使用 `uv` 管理，新增 `pathspec`、`regex`、`mpmath` 后运行 `uv lock` 和 `uv sync`。
  Rationale：仓库 `AGENTS.md` 禁止直接使用 pip 修改环境，工具契约又明确指定这些库。
  Date/Author：2026-07-24 / Codex

## Outcomes & Retrospective

在每个里程碑完成时填写：实现了哪些用户可见行为、哪些设计约束通过测试证明、是否发现未覆盖的现有实现差距。最终填写全量测试结果、可接受限制（例如首版授权后 symlink/junction 替换的 TOCTOU 限制）和后续工作，不把未实现项写成已完成。

## Context and Orientation

工具框架的核心入口位于 `miniagent/tools/registry.py`、`miniagent/tools/models.py`、`miniagent/tools/schema.py`、`miniagent/tools/validation.py`、`miniagent/tools/policy.py`、`miniagent/tools/executor.py` 和 `miniagent/tools/artifacts.py`。实现时先阅读这些现有模块及其测试，再按本文顺序修改；不要以旧的裸字符串工具接口作为兼容目标。

每个正式内置工具都放在 `miniagent/tools/<tool_name>/`，至少有 `__init__.py`、`tool.py` 和 `prompt.py`。`ToolSpec` 是注册聚合，`ToolInput`/`ToolOutput` 是严格 Pydantic 模型，`ToolTarget` 描述经过规范化的资源能力和范围，`ExecutionContext` 提供已授权 target、取消信号和运行时能力。模型只看 Registry 派生的 OpenAI-compatible function schema；业务字段不能代替框架字段 `correction_of_tool_use_id`。

## Plan of Work

### Milestone 0：实现前基线与参考资料

先在仓库根目录确认 Python 版本、`pyproject.toml`、现有测试和上述核心模块的真实函数名。阅读 `docs/design-docs/tool-design-guidelines.md` 全文，随后阅读 `docs/design-docs/tool-registry-and-execution.md` 的 ToolSpec、ToolRegistry、ToolTarget、Target Authorization、执行流水线、失败/修正/重试、批次调度、结果持久化和核心不变量章节。再阅读 `docs/design-docs/tools/README.md`、`glob.md`、`grep.md`、`calculator.md`。

本阶段不写代码；产物是实现文件清单、当前测试基线和需要从旧接口迁移的差距记录。参考文档：`AGENTS.md`、`PLANS.md`、`docs/design-docs/tool-design-guidelines.md`、`docs/design-docs/tool-registry-and-execution.md`、`docs/design-docs/tools/README.md`、`docs/design-docs/tools/glob.md`、`docs/design-docs/tools/grep.md`、`docs/design-docs/tools/calculator.md`。

### Milestone 1：先修改工具注册表与执行器公共机制

在 `miniagent/tools/models.py` 等现有模型模块中补齐严格的 `ToolOutput`、`ToolSpec`、`ToolTarget`、`ExecutionTraits`、`ExecutionContext`、`RetryPolicy`、`ResultPolicy` 和统一 `ToolFailure`/`ToolExecutionError` 所需字段；保留框架封闭的 `ExecutionErrorCode`，不为三个工具新增顶层错误码。确保所有 Pydantic 输入/输出模型 `extra="forbid"` 且严格校验。

在 `miniagent/tools/registry.py` 实现 composition root 显式 `available_names` 加载：只按 `miniagent.tools.<name>` 同名包导入，不扫描目录；冻结时检查唯一名称、输入/输出 schema、`$defs/$ref` 展开、`additionalProperties=false`、`strict=true`、`correction_of_tool_use_id` 包装字段，并解析 `module:SYMBOL` PromptRef。冻结后的 ToolSpec、function schema、output schema 和 Prompt 必须不可变。

在 `miniagent/tools/executor.py` 串起解析调用、框架字段检查、快速检查、严格 Pydantic 校验、target resolver、统一授权、分类、超时/取消、handler 输出校验、完整 ToolOutput 预算、ArtifactStore 外置、终态 ToolResult 和按原始调用顺序提交。实现 transient attempt 最多三次、outcome unknown 不重放、一次修正且禁止链式修正；把 permission denied、参数错误和内部协议错误分别留在各自边界。并把当前 AgentRun 的连续最终失败投影到下一轮 ToolView，第三次连续失败后移除工具，成功清零。

在 `miniagent/tools/policy.py` 或现有授权边界中落实 `exact/subtree` 与 `read/write/delete` 不互相扩大、`.mini` Protected Workspace Subtree、普通祖先扫描跳过受保护目录、symlink/junction 解析规则和多 target 整体裁决。检查 `miniagent/context.py`、`miniagent/loop.py` 与 ModelAdapter 使用同一个刷新后的 ToolView，不把静态工具集合继续传给模型。

本阶段先写框架测试：注册冲突/错误 PromptRef/schema 失败、alias 和多余字段、handler 未获授权不执行、output protocol error、完整结果外置、重试/取消/并发屏障、修正限制、连续失败工具消失以及 message/trace 顺序。参考文档：`tool-registry-and-execution.md` 全部框架章节、`tool-design-guidelines.md` 第 3--11 节；只在本阶段验证公共机制，不实现三个业务工具。

### Milestone 2：实现共享文件搜索深模块

创建 `miniagent/tools/_filesystem_search/__init__.py`、`patterns.py`、`ignores.py`、`walker.py` 和 `models.py`。`patterns.py` 成为唯一受控 glob 编译器：路径统一 `/`，完整路径匹配，`*`/`?`/字符类不跨 `/`，`**` 只能作为完整路径段；拒绝 brace、extglob、正则、否定规则、空段及 `.`/`..`。

`ignores.py` 使用 `pathspec` 处理分层 `.gitignore`，`include_ignored=false` 时在忽略目录剪枝，`true` 时只绕过 `.gitignore`。`walker.py` 按规范化相对路径区分大小写字典序稳定遍历，先硬排除 `.git`、缓存目录、虚拟环境和 `*.pyc`，再跳过 `.mini`（除非它是显式搜索根）、symlink/junction，最后交给业务筛选；在目录/文件边界检查线程安全取消信号，并让 `asyncio.to_thread()` 线程收束后再返回。

为共享 walker 编写临时目录测试：分层 ignore、硬排除优先级、链接不跟随、`.mini` 祖先跳过、排序、取消和 Windows/POSIX 分隔符一致性。参考文档：`docs/design-docs/tools/README.md` 的共享文件搜索边界、`glob.md` 第 3--4 节、`grep.md` 第 3--4 节。

### Milestone 3：实现并注册 glob

创建 `miniagent/tools/glob/__init__.py`、`tool.py` 和 `prompt.py`。在 `tool.py` 实现严格 `GlobInput`：`pattern`、`path`、`kind`、`include_ignored`、`max_results`，校验非空路径、模式长度、完整路径匹配和 1..1000 的结果上限；resolver 只产生一个 `directory/read/subtree` target，handler 只从 `ExecutionContext.targets[0]` 取得根目录。

实现确定性 walker 调用、kind 筛选、达到上限立即停止、`truncated` 语义和取消；20 秒 timeout、单次 attempt、`concurrency_safe=True`、20 KiB `externalize` ResultPolicy。定义 `GlobMatch`、`GlobMetadata`、`GlobData`、`GlobOutput`，content 每行一个 workspace-relative 路径，目录文本以 `/` 结尾，metadata/data 不回显原始参数或 ignore 内容；非法输入交由 Executor 产生 `invalid_arguments`，授权根目录整体不可读抛 `RESOURCE_UNAVAILABLE`。

在 `prompt.py` 使用契约中的英文模板，明确 glob 与 grep/read_file 边界；composition root 将 `glob` 加入显式可用名称。测试 schema、targets、路径语义、kind、ignore/protected/link、排序、上限、输出结构和 artifact 外置。参考文档：`docs/design-docs/tools/glob.md` 全文、`tool-design-guidelines.md` 第 2--12 节。

### Milestone 4：实现并注册 grep

创建或迁移 `miniagent/tools/grep/__init__.py`、`tool.py` 和 `prompt.py`，不保留旧 `grep.py` 的裸字符串或中文错误作为契约。严格实现 `GrepInput` 的九个业务字段：regex/literal 模式、include/exclude、内容大小写、上下文、ignore 和 1..1000 的 `max_matches`；path 必须是已存在目录，include/exclude 各自 OR 且 exclude 优先，数组去重和长度限制在验证阶段完成。

复用 `_filesystem_search` walker，仅读 UTF-8/UTF-8 BOM；NUL 或非 UTF-8 文件跳过并计数。regex 使用 `regex` 包逐行 search、每行最多 50ms，literal 使用 Unicode case folding 并维护原文坐标；按匹配行计数，收集非重叠 span，合并相邻/重叠上下文区间，达到上限立即停止。长行限制为 500 Unicode 字符，匹配窗口必须包含首个匹配，data 不保存完整长行。

定义 `MatchSpan`、`GrepLine`、`GrepGroup`、`GrepMetadata`、`GrepData`、`GrepOutput`；content 使用 `>`/空格行标记、路径/1-based 行号和 `--` 分隔，零结果和截断使用契约文本。设置 30 秒 timeout、单次 attempt、`concurrency_safe=True`、20 KiB `externalize` ResultPolicy；整体不可读映射 `RESOURCE_UNAVAILABLE`，regex 超时映射 `DEADLINE_EXCEEDED`。注册 `grep` 并测试所有稳定排序、span/零宽、上下文、编码、预算、取消和权限不变量。参考文档：`docs/design-docs/tools/grep.md` 全文、`docs/design-docs/tools/README.md` 共享搜索边界。

### Milestone 5：实现并注册 calculator

创建 `miniagent/tools/calculator/__init__.py`、`tool.py` 和 `prompt.py`。严格 `CalculatorInput` 只含 `expression` 与 `precision`，expression trim 后非空且不超过 1024 Unicode 字符，precision 为 1..100。用 Python AST/token 解析后仅允许十进制字面量、括号、规定的一元/二元运算、白名单函数和常量；拒绝属性、索引、导入、赋值、容器、比较、布尔、条件、lambda、推导式、关键字参数和任意调用，禁止 `eval()`。

使用独立的 mpmath context，在请求 precision 上加固定 guard digits，不修改共享 `mpmath.mp.dps`；整数闭合运算尽可能保持 arbitrary-precision int。实现 `//` 为 `floor(a/b)`、`%` 为 `a-b*floor(a/b)`、十进制 ties-to-even round、弧度三角函数和严格函数 arity。执行前/执行中限制 AST 128 节点、深度 32、字面量 256 位、参数 32 个、整数 4096 bits、指数绝对值 10000；拒绝复数、NaN、无穷和定义域外结果。

resolver 返回空 targets，classifier 固定 `concurrency_safe=True`，通过 `asyncio.to_thread()` 求值；timeout 5 秒、单次 attempt、系统默认 ResultPolicy。定义 `CalculatorMetadata`、`CalculatorData`、`CalculatorOutput`，content 必须等于字符串 `data.value`，精确整数不受 precision 影响，实数稳定格式化并规范负零。按契约将域错误映射 `DOMAIN_ERROR`、预算超限映射 `RESOURCE_EXHAUSTED`、其他预期求值失败映射 `OPERATION_FAILED`，不创建私有顶层错误码。注册 `calculator` 并测试并发 precision 隔离、0.1+0.2、AST 白名单、所有函数/常量/边界预算和输出脱敏。参考文档：`docs/design-docs/tools/calculator.md` 全文、`tool-design-guidelines.md` 第 5--11 节。

### Milestone 6：集成、迁移与完整验证

在 composition root 的显式工具列表中按完成顺序加入 `glob`、`grep`、`calculator`，确认未完成目录不会自动注册。为 `pyproject.toml` 增加缺失的 `pathspec`、`regex`、`mpmath`，运行 `uv lock` 后 `uv sync`，不手工编辑 `uv.lock`。更新旧 grep 测试和调用点，使其使用结构化 output、targets 和新的英文错误协议；不扩展到未请求的 `read_file`、`write_file` 或远程工具。

先运行聚焦测试，再从仓库根目录运行：

    uv run python -m compileall miniagent tests main.py
    uv run python -m pytest -q

必要时启动：

    uv run python -m miniagent.ui

验收必须证明：Registry 冻结后 schema/Prompt 可见且只读；未授权 target 不启动 handler；普通 workspace 搜索跳过硬排除、`.mini` 和链接；glob/grep 结果稳定且预算超限生成受控 ArtifactRef；calculator 不依赖网络/文件/Session，独立精度并发不互相污染；失败、取消、重试、修正、连续失败移除和新 AgentRun 恢复都符合注册执行设计。

## Concrete Steps

所有命令从 `D:\study\MiniAgent` 执行。实施者应在每个里程碑后先运行对应的 `uv run python -m pytest -q tests/tools/...` 聚焦测试，再运行编译检查；依赖变更后必须执行 `uv lock` 和 `uv sync`。测试只能使用 pytest 临时目录、fake context 和本地 fixture，不访问真实网络、凭据或用户 Session 数据。

## Validation and Acceptance

验收以行为为准：给 `glob` 一个包含嵌套目录、忽略目录、`.mini` 和链接的临时 workspace，结果只含允许且排序稳定的路径；给 `grep` UTF-8、BOM、二进制、坏编码和长行样本，匹配行、坐标、上下文和计数准确；给 `calculator` `0.1 + 0.2`、负数整除取模、三角函数、非法 AST、除零和超预算表达式，分别得到精度安全成功或规定的结构化失败。完整测试通过后，更新本节记录实际测试数量、关键输出和仍保留的架构限制。

## Idempotence and Recovery

所有新增工具目录和测试均为可重复创建；依赖同步以 `pyproject.toml`/`uv.lock` 为源，不手工修改锁文件。若单个里程碑失败，保留已通过的框架测试，修正该里程碑后重新运行聚焦测试，不回退无关用户改动。不要使用破坏性 git 命令。执行取消、超时或权限拒绝时必须确认没有遗留线程任务、未提交的伪造结果或未经授权的资源访问。

## Artifacts and Notes

计划完成后应留下：三个同名工具包、共享 `_filesystem_search` 私有模块、注册表/执行器公共协议的测试、结构化 ToolOutput/ToolFailure 测试、更新后的依赖锁文件，以及本计划中带时间戳的进度和验收证据。不要在 output、日志、fixture 或文档中保存 provider secret、环境值、原始工具参数、完整长行、表达式 AST 或用户 Session 内容。

## Interfaces and Dependencies

实现结束时至少应存在以下稳定接口：

    registry = ToolRegistry(available_names=("glob", "grep", "calculator"))
    registry.freeze()

    async def handler(args: ToolInput, context: ExecutionContext) -> ToolOutput:
        ...

    resolve_targets(validated_input) -> tuple[ToolTarget, ...]
    classify(validated_input, targets) -> ExecutionTraits

`glob`/`grep` 的共享模块只暴露给工具包内部，不定义 ToolSpec；`calculator` 的 resolver 明确返回空 tuple。依赖由 `pyproject.toml` 声明：`pathspec` 负责分层 `.gitignore`，`regex` 负责可超时正则，`mpmath` 负责独立十进制精度；使用 `uv` 维护锁定环境。

