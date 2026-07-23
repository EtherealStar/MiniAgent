# 实现可恢复的 Session 持久化与低干扰可观测性

本 ExecPlan 是一份活文档。实施过程中必须持续更新 `Progress`、`Surprises & Discoveries`、`Decision Log` 和 `Outcomes & Retrospective`，使任何只拿到当前工作树和本文件的人都能继续工作。

本文件必须按照仓库根目录 `PLANS.md` 维护。计划以 `docs/design-docs/persistence-and-observability.md` 为目标设计，但在本文中重复实现所需的关键规则，执行者不应依赖对先前讨论的记忆。每次修订都要同步修改所有受影响章节，并在文末“修订说明”中记录改动和原因。

## Purpose / Big Picture

完成后，MiniAgent 的已提交对话在进程正常退出或意外中断后都能从本地 Session 目录确定性恢复。用户可以按需列出历史 Session；单个损坏目录不会遮蔽其他 Session；同一个 Session 同时只允许一个进程写入。最重要的可观察保证是：触发一次 AgentRun 的用户消息没有完成文件同步（fsync）之前，模型请求和工具调用都不会发生。

系统还会生成本地、可轮转的 Trace，用父子 Span 关联 AgentRun、Turn、ModelCall 和 ToolCall。Trace 默认只记录元数据和脱敏错误，写入变慢、队列满或文件损坏都不能回滚 Message Journal、改变 AgentRunResult，或阻塞核心运行。Message Journal 是恢复事实源；Trace 只是允许丢失的诊断材料。

实现后的演示应能在临时目录创建 Session，完成一轮含工具调用的运行，关闭并重新打开后得到相同 Transcript；手工在文件尾写入半条 JSON 后再次打开会截断尾行并正常恢复；第二个 Repository 同时打开同一 Session 会立即得到“正在使用”错误；删除或阻塞 Trace 目录后，相同运行仍然成功且 Journal 完整。

## Progress

- [x] (2026-07-23) 阅读 `AGENTS.md`、`PLANS.md`、目标设计、领域词汇、相关架构文档、当前实现与测试，确认当前基线和迁移边界。
- [x] (2026-07-23) 运行 `uv run python -m pytest -q`，确认计划编写时基线为 `69 passed`。
- [x] (2026-07-23) 建立窄 Journal record 模型、严格编解码器和恢复状态机；确定性编码、关联校验与完整工具往返测试通过。
- [x] (2026-07-23) 实现跨平台独占 writer lock、SessionRepository 的 create/open/list 和崩溃尾行恢复；Repository 与全套回归测试通过。
- [x] (2026-07-23) 将 SessionEngine 与 AgentLoop 迁移到窄提交接口和内存 FIFO/唯一 worker 路径，测试证明 user fsync 先于模型、Assistant fsync 先于工具。
- [x] (2026-07-23) 实现 metadata-only Trace 模型、有界异步 JsonlTraceSink、轮转、脱敏和降级语义，并接入 run、turn、model、tool 边界。
- [x] (2026-07-23) 完成恢复、锁竞争、Journal 故障、Trace 故障和端到端测试；全套测试在 warnings-as-errors 下通过 97 项。
- [x] (2026-07-23) 完成双轴代码审查与修复，更新活文档、最终文件布局、验证证据和残留范围。

## Surprises & Discoveries

- Observation: `AGENTS.md` 当前为空，没有额外的仓库级执行约束。
  Evidence: 2026-07-23 读取文件未得到内容。

- Observation: 当前 `miniagent/storage.py` 的 `JsonlTranscriptStore` 每次追加只执行 `write()` 和 `flush()`，没有 `os.fsync()`，也没有读取、严格恢复或 writer lock。
  Evidence: `JsonlTranscriptStore._append_line()` 在 flush 后直接退出文件上下文。

- Observation: 当前 `miniagent/events.py` 把 `event_id` 和 `sequence` 放在所有 `SessionEvent` 上，而且 `miniagent/loop.py` 会提交 Assistant started、delta、ToolUse detected 和 discarded；这些运行时事实都不应进入目标 Message Journal。
  Evidence: `AgentLoop.run()` 通过通用 `event_sink.emit()` 发出上述对象，`SessionEngine.emit()` 将所有对象交给 TranscriptStore。

- Observation: 工具执行器已有 `MemoryTraceSink` 和字典事件，但字段名、Span 关系、错误脱敏和 best-effort 失败隔离都未达到目标 Trace 契约。
  Evidence: `miniagent/tools/executor.py::_trace()` 直接等待 `trace_sink.emit({"event": ...})`，sink 异常会传播到工具运行。

- Observation: 在当前 Windows/uv 环境中，`uv run pytest -q` 收集测试时找不到本地 `miniagent` 包，而 `uv run python -m pytest -q` 正常通过。
  Evidence: 前者产生 10 个 `ModuleNotFoundError`；后者输出 `69 passed in 1.08s`。本计划统一使用后一个命令。

- Observation: Windows 上仅依赖 `msvcrt.locking` 不能可靠表达同一进程内两个 Repository 实例的竞争，因此进程内规范化路径 owner 集合是 OS 锁的必要补充，而不是锁文件存在性检查。
  Evidence: `tests/persistence/test_repository.py::test_second_writer_is_rejected_until_the_first_closes` 在当前 Windows 环境通过，并验证关闭后可以重新获取。

- Observation: 完成 Journal codec 与 Repository 后，全套测试从基线 69 项增加到 82 项且全部通过。
  Evidence: `uv run python -m pytest -q` 输出 `82 passed in 1.44s`。

- Observation: 通用 SessionEvent 兼容层会继续把 event_id 语义带入运行时通知，删除旧 `events.py`/`storage.py` 并新增纯 `updates.py` 后，生产路径可以从类型层面区分 SessionUpdate 与 Journal Record。
  Evidence: `rg -n "JsonlTranscriptStore|SessionEvent|event_id|journal_sequence" miniagent tests` 仅命中 Journal 负向测试。

- Observation: 初版 Trace 接入在缺少模型终态时先合成失败对象，导致 stream_summary 的 `terminal_received` 恒真；双轴审查同时发现相同时间戳列表缺少稳定 tie-break，以及预取消工具未形成 span 终态。
  Evidence: 新增缺终态、相同时间戳和预取消批次测试后均通过；Session 列表以 session_id 作为稳定次级顺序。

- Observation: 最终测试从基线 69 项增加到 97 项，warnings-as-errors、compileall 和静态搜索全部通过。
  Evidence: `uv run python -m pytest -q -W error` 输出 `97 passed in 1.98s`；`python -m compileall -q miniagent tests` 无输出且退出码为 0。

## Decision Log

- Decision: Message Journal 使用专用 record 联合类型和严格 codec，不再复用 `SessionEvent` 或 Trace envelope。
  Rationale: 恢复格式只允许五类业务事实；复用通用事件会再次引入 `event_id`、`sequence` 和草稿事件，并模糊权威性边界。
  Date/Author: 2026-07-23 / Codex

- Decision: 不引入数据库、OpenTelemetry、OTLP 或文件锁第三方依赖；writer lock 在一个窄的私有适配器中用 Windows `msvcrt.locking` 和 POSIX `fcntl.flock` 实现。
  Rationale: 项目只需本地单写者语义，Python 标准库可以满足目标平台；隔离平台代码后可独立测试和替换。
  Date/Author: 2026-07-23 / Codex

- Decision: `list_sessions()` 只判断 Journal 是否在无修改扫描下结构可读，不尝试获取 writer lock，因此“正在被另一进程使用”只在 `open_session()` 时确定。
  Rationale: 设计明确禁止列表扫描获取锁或改写文件。锁状态在扫描后也可能立刻变化，列表中宣称实时锁状态会造成竞态假象。
  Date/Author: 2026-07-23 / Codex

- Decision: 有效 record 必须以换行结束；获得 writer lock 的 `open_session()` 可以删除唯一的无换行尾部片段，无论该片段是否恰好能解析为 JSON。列表扫描忽略该片段但不截断。
  Rationale: 提交协议总是一次写入 JSON 加换行并 fsync；缺少换行表示没有提交完成。此规则让崩溃判定确定，不猜测操作系统究竟写入了多少字节。
  Date/Author: 2026-07-23 / Codex

- Decision: Trace 对核心调用方暴露不会抛出异常的 `BestEffortTraceSink.emit()`；文件 writer 在后台消费有界队列，队列满时递增内存 drop 计数并立即返回。
  Rationale: 仅依赖每个调用点都正确捕获异常很脆弱。失败隔离放在适配边界中，可以从构造上保证 Trace 不改变业务控制流。
  Date/Author: 2026-07-23 / Codex

- Decision: 首条消息由 `SessionRepository.create_session()` 原子创建并提交，后续消息由 SessionEngine worker 出队后提交；AgentLoop 不再提交 UserMessage。
  Rationale: 这样 Journal fsync 成功明确支配 AgentRunEnvironment 组装、模型调用和工具执行，也避免同一用户消息在 Repository 与 AgentLoop 中重复写入。
  Date/Author: 2026-07-23 / Codex

- Decision: 恢复未终止 run 时，PROCESS_INTERRUPTED 的 turn_count 取该 run 已提交的 AssistantMessage 数量，final_message_id 取最后一条已提交 AssistantMessage；这两个值是可从 Journal 确定得到的保守下界。
  Rationale: 失败或中断的 ModelCall 草稿不进入 Journal，因此崩溃后无法知道所有已发请求的精确数量。使用可证明的下界比从 Trace 猜测更符合 Journal 的权威边界。
  Date/Author: 2026-07-23 / Codex

- Decision: 后续输入通过 SessionEngine 的内存 FIFO 与 `run_next()` 唯一 worker 路径串行执行；`submit()` 只分配身份和发布 runtime update，`run_next()` 出队后才提交 user_message 并调用 AgentLoop。
  Rationale: 这同时满足排队输入非恢复事实、user fsync 先行关系和同一 Session 不交叠运行，避免公开调用方并发 `commit_user()` 毒化 handle。
  Date/Author: 2026-07-23 / Codex

- Decision: RunTerminated 的 ErrorInfo 在 SessionEngine 提交边界统一执行控制字符清理、凭据脱敏和 512 字符限长，而不是依赖每个 Provider 调用点自行处理。
  Rationale: Journal 是长期权威文件，集中净化可以覆盖 Provider、上下文和未来调用方产生的所有终态错误。
  Date/Author: 2026-07-23 / Codex

- Decision: 删除无人引用的旧 `events.py` 与 `storage.py`，以 `updates.py` 承载不带 event_id/sequence 的 runtime-only 通知。
  Rationale: 保留旧类型会让新代码再次误用通用事件持久化；明确删除比“deprecated 但可导入”更能守住权威边界。
  Date/Author: 2026-07-23 / Codex

## Outcomes & Retrospective

全部五个里程碑已完成。`miniagent/journal.py` 与 `miniagent/repository.py` 提供严格五类 Journal、确定性重放、独占 writer lock、尾片段修复和隔离列表；`miniagent/session.py`、`miniagent/loop.py` 与 `miniagent/updates.py` 提供 FIFO worker、窄提交和 runtime-only 草稿更新；`miniagent/trace.py` 及工具执行器提供 metadata-only run/turn/model/tool span、有界队列、轮转、脱敏和 best-effort 降级。恢复不会调用模型或工具，PROCESS_INTERRUPTED 只补记一次；首条 fsync、Assistant fsync、锁竞争、损坏隔离、Trace 抛错/溢出与两次 AgentRun 恢复都有自动化证据。最终为 `97 passed`，未新增运行时依赖。明确未在本计划实现的仍是完整 Textual UI 生命周期和远程 telemetry，它们本来就在非目标范围内。

## Context and Orientation

MiniAgent 是 Python 3.11 项目。`Session` 是一段持续对话；一条已持久化的用户消息触发一个 `AgentRun`；一次 AgentRun 可以包含多个 `ModelCall` 和 `ToolCall`。`Transcript` 是内存中的有效消息历史。`Message Journal` 是磁盘上的 `message.jsonl`，也是恢复 Transcript 的唯一事实源。“fsync”指将进程缓冲区中的文件内容要求操作系统同步到稳定存储；普通 `flush()` 只清空 Python 缓冲区，不能提供同等承诺。“writer lock”是操作系统维护的非阻塞独占文件锁，不是检查某个锁文件是否存在。“Trace Span”是带开始、结束、状态和父子关系的一段诊断活动；Trace 丢失不影响业务事实。

当前核心文件关系如下。`miniagent/domain.py` 定义 Message、Part、ContextSummary、AgentRunResult 和 StopReason，并提供 Message 的字典转换。`miniagent/events.py` 定义当前通用 SessionEvent。`miniagent/storage.py` 只有只写的 JsonlTranscriptStore。`miniagent/session.py` 通过 `SessionEngine.emit()` 校验、持久化并投影所有事件。`miniagent/loop.py` 组装 AssistantMessage、调用工具并把用户消息、草稿 delta、完整消息和终态都交给同一个 EventSink。`miniagent/tools/executor.py` 执行工具，并向当前简易 trace sink 写字典。`miniagent/provider/events.py` 定义模型流的 delta 与终态。`tests/test_session.py` 和 `tests/test_loop.py` 固化现有事件驱动行为，迁移时必须改成新窄接口的测试，不能为了保留旧测试而继续持久化草稿。

目标目录固定为：

    sessions/
      <session_id>/
        message.jsonl
        writer.lock
        trace/
          000001.jsonl
          000002.jsonl

每个 Journal 物理行是一个完整 JSON object，文件行序就是事实顺序，不增加 `journal_sequence`。只允许 `user_message`、`assistant_message`、`tool_result`、`context_summary` 和 `run_terminated`。QueuedInput、Assistant started/delta/discarded、ToolUseDetected、ModelEvent、SessionUpdate 和 Trace 数据都不能进入该文件。ReasoningPart 与 ToolUsePart 是完整 AssistantMessage 的组成部分，随 `assistant_message` 一次提交；工具只有在这条 AssistantMessage fsync 成功后才能执行。

## Required Reference Reading

开始实现前必须完整阅读下列仓库内文档；若实现跨越多个工作时段，在开始新的里程碑前重读与该里程碑直接相关的文档。这里列出阅读目的，不能用文件名代替本文已经给出的实现规则。

先读根目录 `PLANS.md`，它规定 ExecPlan 的维护、验证和修订方式；读根目录 `CONTEXT.md`，统一 Session、AgentRun、Transcript、Journal Record、Session Update 和 Trace Record 等术语；检查根目录 `AGENTS.md` 是否出现新的仓库约束。

随后完整阅读 `docs/design-docs/persistence-and-observability.md`，它是本工作的直接契约；阅读 `docs/design-docs/overall-architecture.md`，确认 Repository、SessionEngine、AgentLoop、UI 和 Trace 的所有权边界；阅读 `docs/design-docs/main-loop.md`，确认用户消息提交先行关系、唯一 worker 和窄提交接口；阅读 `docs/design-docs/context-management.md`，确认 ContextSummary 的覆盖边界与恢复顺序；阅读 `docs/design-docs/tool-registry-and-execution.md`，确认 ToolUse、ToolResult、重试、取消和 outcome_unknown 的关联；阅读 `docs/design-docs/openai-compatible-model-provider.md`，确认 request ID、usage、finish reason 和安全 Provider 错误从哪里取得；阅读 `docs/design-docs/textual-ui.md`，只为确认 Session picker、完整 snapshot 和 SessionUpdate 的消费者契约，本计划不实现 Textual widget。

最后阅读当前源码 `miniagent/domain.py`、`miniagent/events.py`、`miniagent/storage.py`、`miniagent/session.py`、`miniagent/loop.py`、`miniagent/context.py`、`miniagent/provider/events.py`、`miniagent/provider/openai.py`、`miniagent/tools/models.py`、`miniagent/tools/executor.py`，以及对应的 `tests/test_session.py`、`tests/test_loop.py`、`tests/test_domain_context.py`、`tests/provider/test_openai.py`、`tests/tools/test_executor.py`。这些文件是迁移起点，不是目标契约；发现与本文冲突时应修改实现和旧测试，并把差异记录到 Decision Log。

不要求查阅外部博客。Windows byte-range lock、POSIX flock、fsync 和 JSONL 尾行规则的必要知识已在本文的具体步骤中给出；如果执行者为验证标准库行为而阅读 Python 官方文档，必须把影响设计的发现和最小复现实验记录到 Surprises & Discoveries，不能仅留下链接。

## Journal Record Contract

在 `miniagent/journal.py` 定义 `JournalRecordType(StrEnum)`、五个 payload dataclass、不可变 `JournalRecord`、`JournalCorruptionError` 和严格的 `JournalCodec`。统一 envelope 恰好包含 `schema_version`、`record_type`、`session_id`、`run_id`、`occurred_at` 和 `payload`；schema_version 固定为整数 1。顶层和各 payload 的未知字段都拒绝，UUID 必须可解析，occurred_at 必须是带时区的 ISO 8601 时间并规范化为 UTC。编码使用 UTF-8、`ensure_ascii=False` 和紧凑分隔符，每条编码结果最后恰好一个 `\n`。

五种 payload 的目标内容如下。`user_message` 保存完整 user Message；`assistant_message` 保存完整 assistant Message 和可空 finish_reason；`tool_result` 保存只含 ToolResultPart 的完整 tool Message；`context_summary` 保存完整 ContextSummary；`run_terminated` 保存 reason、turn_count、可空 final_message_id 和可空的安全 ErrorInfo。自然身份分别是 Message.message_id、ContextSummary.summary_id 和 envelope.run_id；不增加通用 event_id。

在同一文件定义或在 `miniagent/journal_replay.py` 定义 `JournalReplayState`。它按物理顺序一次扫描并维护已见 message_id、summary_id、tool_use_id、每个 run 的 user message 和终态。验证至少包括：记录 session_id 必须等于目录名；自然身份不得重复；一个 run 只能有一条 user_message 和一条 run_terminated；同一 Session 的 run 不可交叠，上一 run 未终止前不能出现下一 user_message；assistant/tool/summary/terminal 必须属于当前 run；消息 role 必须与 record_type 一致；AssistantMessage 的 continuation/retry 引用只能指向此前已见的 AssistantMessage；每个 ToolUse 的 tool_use_id 在 Session 中唯一；ToolResultPart 必须引用此前已提交 AssistantMessage 内真实存在且尚未得到结果的 ToolUse；一个 ToolUse 只能有一个结果；ContextSummary 的 covers_through_message_id 和非空 resume_from_message_id 必须引用此前消息，且覆盖边界不能倒退；run_terminated.final_message_id 若非空必须引用该 run 已提交的 AssistantMessage。

重放输出 `RecoveredSession`，至少包含 `session_id`、按物理顺序的 `messages`、按创建顺序的 `context_summaries`、各 run 的终态和可空 `interrupted_run`。只有最后一个 run 可以缺少终态；这不是损坏，而是进程中断。恢复绝不调用模型或工具，也不重建 QueuedInput 或 Assistant 草稿。

## Plan of Work

### Milestone 1: 固化窄 Journal 格式和确定性重放

先新增 `miniagent/journal.py` 和 `tests/persistence/test_journal_codec.py`。把 `domain.py` 现有 `message_to_dict()` 与 `message_from_dict()` 收紧或由 codec 包装，使非法 Part、未知字段、错误 role 和缺失字段最终都变成带文件行号与安全原因的 `JournalCorruptionError`，而不是泄漏 KeyError 或静默忽略。不得用 Pydantic 的宽松默认转换把字符串 `"1"` 接受为 schema version 1。

实现纯内存的 `replay_records(records, expected_session_id)`，先测试合法的 user → assistant(tool use) → tool result → assistant → terminal 顺序，再逐项测试未知 record_type、未来 schema、重复自然身份、跨 run 关联、未知 ToolUse、重复 ToolResult、摘要越界和中间损坏。这个里程碑结束时还没有文件锁，但 codec 与状态机已经能从 record 列表恢复与原始 Transcript 相等的值。

运行：

    cd D:\study\MiniAgent
    uv run python -m pytest -q tests/persistence/test_journal_codec.py

预期新测试全部通过；用固定 UUID 和时间编码同一 record 两次应得到完全相同的 bytes，编码字节中不出现 `event_id` 或 `journal_sequence`。

### Milestone 2: 实现耐崩溃文件 Journal 和 SessionRepository

新增 `miniagent/repository.py`，定义 `SessionRepository`、`OpenSession`、`SessionSummary`、`SessionOpenError`、`SessionLockedError` 和 `SessionCorruptError`。Repository 构造函数接收显式 `sessions_root: Path`，不读取全局工作目录。所有阻塞文件 I/O 通过 `asyncio.to_thread` 从异步 API 调用；同步的 codec/replay 函数保持纯函数，便于单元测试。

`OpenSession.append(record)` 必须在单一进程内用锁串行化，先在内存验证下一 record，再编码为一条 bytes，用已经保持打开的二进制 append handle 完整写入，执行 flush 和 `os.fsync(file.fileno())`，成功后才推进内存 replay state。若 write、flush 或 fsync 任一失败，handle 进入 poisoned 状态：后续 append 一律失败，调用方必须关闭并重新打开完整扫描；不得在不知道实际落盘长度时自动重试。提供可注入的低层 writer 或故障点，使测试能分别模拟 write、flush 和 fsync 失败。

私有 `_WriterLock` 打开 `<session>/writer.lock`。Windows 上若文件为空先写入一个固定字节并 flush，然后用 `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` 锁住第一个字节；关闭前用 `LK_UNLCK` 解锁。POSIX 上用 `fcntl.flock(fd, LOCK_EX | LOCK_NB)`，关闭前 `LOCK_UN`。锁的真实性来自打开的 file descriptor；锁文件存在本身不表示占用，禁止用 `Path.exists()` 决定锁状态。进程内也要保留规范化 session path 的 owner 集合，避免某些平台允许同进程重复获取锁而使测试产生假成功。

`create_session(session_id, first_user_record)` 校验 ID 与首 record 后创建目录、获取锁、创建 `message.jsonl` 并提交首条 user_message；只有 fsync 成功才返回 OpenSession。失败时关闭 fd、释放锁，并尽力删除没有任何完整 record 的新目录；清理失败不能遮蔽原异常。既有非空目录不得被覆盖。此 API 是首条消息的唯一创建路径。

`open_session(session_ref)` 先获取 writer lock，再以二进制从头扫描。每个以 `\n` 结束的物理行必须是非空、合法 UTF-8、合法 JSON 和合法 record；任意中间错误都使打开失败。文件最后存在无换行 bytes 时，在持锁状态下截断到最后一个换行并 fsync；截断后重新或继续验证完整前缀。成功返回 OpenSession 和 RecoveredSession。若最后 run 缺少终态，只在恢复结果中标记 interrupted_run；Repository 本身不伪造业务终态。

`list_sessions()` 只在调用时枚举 sessions_root 的直接子目录，不在应用启动或 Repository 构造时扫描。它不获取 writer lock，不创建、截断或修复文件。对每个目录读取完整换行记录并派生 summary；忽略无换行尾部片段，因为 open 时可恢复；中间损坏则返回可见但 `openable=False` 的条目，名称回退为目录名，error_category 使用固定短码。可用条目的名称取首条 UserMessage 中 TextPart 文本，合并空白后截断为 48 个 Unicode code point；created_at 取首条 user_message 时间，last_user_input_at 取最后一条 user_message 时间。结果按 last_user_input_at 倒序，再按 session_id 稳定排序。扫描单个目录的 PermissionError、OSError 或损坏不能中断其他目录。

新增 `tests/persistence/test_repository.py` 和一个子进程锁测试 helper。测试真实 fsync 调用、创建失败清理、空目录不列表、合法列表排序、损坏隔离、尾行修复、中间损坏拒绝、自然身份错误、同进程重复锁与第二进程锁竞争。对锁竞争使用事件或 stdout 握手等待持锁进程就绪，不使用脆弱的固定 sleep。

运行：

    cd D:\study\MiniAgent
    uv run python -m pytest -q tests/persistence/test_journal_codec.py tests/persistence/test_repository.py

预期所有测试通过，并能在 Windows 当前环境证明第二个 opener 得到 `SessionLockedError`；测试退出后第一个 handle 关闭，再次打开成功。

### Milestone 3: 迁移 SessionEngine 和 AgentLoop 的提交边界

修改 `miniagent/session.py`，让 SessionEngine 持有 OpenSession 与从它恢复的 Transcript，不再把所有 SessionEvent 交给 TranscriptStore。定义明确方法 `commit_user(message, run_id)`、`commit_assistant(message, run_id, finish_reason)`、`commit_tool_result(message, run_id)`、`commit_context_summary(summary, run_id)` 和 `finish_run(result, run_id)`；如果现有生命周期重构已经引入等价名字，可保留新名字，但接口必须同样窄。每个方法都按“内存预校验 → OpenSession.append 并 fsync → 更新 Transcript → 发布 SessionUpdate → best-effort Trace”的顺序执行。Journal 失败时 Transcript 不变，不发布已提交更新，Session 进入必须关闭重开的状态。

Assistant 草稿状态和流式 delta 改为只存在于 SessionEngine/SessionUpdate 的内存投影。可在 `miniagent/updates.py` 定义不带 Journal 序列含义的更新类型；不得为了少改 UI 消费者而让 SessionUpdate 再次写入 Journal。`miniagent/events.py` 若仍被其他测试使用，应明确降级为运行时通知兼容层，最终删除 `SessionEvent` 对持久化的参与。旧 `TranscriptStore` 和 `JsonlTranscriptStore` 在所有 composition root 和测试迁移后删除，或保留为标记 deprecated 且无人引用的短期适配器；里程碑结束前用 `rg` 证明生产路径不再导入它们。

修改 `miniagent/loop.py`：`AgentLoop.run()` 接收一条已经提交的 user Message 和窄 `RunCommitter`/SessionEngine 接口，不再 emit UserMessageRecorded；started、delta、discarded 与 ToolUseDetected 只发布 live update 或 Trace；组装出完整 AssistantMessage 后调用 `commit_assistant()`，成功返回后才允许 `ToolExecutor.submit_batch()`；每个工具结果调用 `commit_tool_result()`；结束时调用 `finish_run()`。Journal commit 失败转换为 StopReason.EVENT_COMMIT_FAILED，但不能尝试再向同一个 poisoned handle 追加 run_terminated。

首条运行必须从 `SessionRepository.create_session(... first_user_record ...)` 返回的 OpenSession 启动。后续 QueuedInput 在 worker 出队时调用 `commit_user()`，成功后才冻结 AgentRunEnvironment 并进入 AgentLoop。若完整的 `start/serve/stop` 生命周期尚未实现，本里程碑至少提供一个 `SessionEngine.run_committed_input()` 的唯一调用路径并迁移所有 composition root；禁止公开调用方直接用未提交 user_message 调用 AgentLoop。测试用 spy model 和 spy tool executor 记录调用时间线，断言 `user fsync completed` 严格早于 `model.stream entered`，且 `assistant fsync completed` 严格早于 `tool batch entered`。

打开历史 Session 时，由 SessionEngine 检查 RecoveredSession.interrupted_run。若存在，Engine 在任何新输入和模型/工具动作前追加一次 `run_terminated`，reason 为 PROCESS_INTERRUPTED，turn_count 取该 run 已提交 AssistantMessage 的数量，final_message_id 取最后一条已提交 AssistantMessage 或 None；重复关闭再打开不会追加第二次。恢复后的内存队列和草稿集合始终为空。该 turn_count 是可证明的保守下界，不能用可能缺失的 Trace 补齐。

重写 `tests/test_session.py` 和 `tests/test_loop.py` 中依赖 event_id、sequence、replay_after 和草稿持久化的断言，新增 `tests/persistence/test_session_recovery.py`。保留 UI sink 失败不改变业务的测试，并增加 commit 失败后 Transcript 不变、模型不启动、工具不启动、恢复不重放副作用、PROCESS_INTERRUPTED 仅一次的测试。

运行：

    cd D:\study\MiniAgent
    uv run python -m pytest -q tests/test_session.py tests/test_loop.py tests/persistence/test_session_recovery.py

预期上述测试通过。检查 Journal，每次完整运行只出现五种允许 record type 中实际需要的类型，Assistant delta 文本只出现在最终 assistant_message 内，不出现 started、delta 或 discarded 行。

### Milestone 4: 建立 Span Trace 和 best-effort 文件 sink

新增 `miniagent/trace.py`。定义 `TraceStatus`、`TraceEventType`、写入前的 `TraceEvent`、落盘后的 `TraceRecord`、`TraceContext` 和 `TraceSink` Protocol。业务代码提交的 TraceEvent 含 event_type、关联身份与 payload，不含顺序号；单 writer 在真正追加时生成随机 trace_record_id、分配 trace_sequence 并记录 UTC occurred_at，组成 TraceRecord。落盘 envelope 固定含 `trace_schema_version=1`、trace_record_id、trace_sequence、occurred_at、event_type、trace_id、span_id、可空 parent_span_id、session_id、run_id、可空 message_id 和 payload。`trace_sequence` 只表示该 Trace writer 的追加顺序，绝不能被恢复代码引用。

提供小型 `TraceRecorder` 或 async context manager 来创建四类 Span：`agent.run` 为根；每次真实 ModelCall 建一个 `agent.turn`，其下建 `model.call`；每个 ToolUse 建 `tool.call`。每个 Span 都发 started 和 finished/error 记录，结束记录含耗时和状态。模型 delta 不创建 Span，只在 model.call 结束时发一个 `stream_summary` event，统计 text/reasoning/tool delta 数量与 UTF-8 字节数、首尾时间、间隔摘要、是否收到终态、是否取消和总耗时。

agent.run payload 记录输入 message_id、turn_count、stop reason、final message ID、耗时和安全错误分类。agent.turn 记录 turn 序号、Assistant message ID、continuation/retry 关联、上下文规模、压缩标记、工具数量和模型终态。model.call 记录 provider、model、供应商 request ID、输入规模、GenerationOptions 的非敏感值、finish reason、usage、重试关联和安全错误。tool.call 记录 tool name、tool_use_id、Assistant message ID、批次位置、attempt、耗时、outcome_unknown、is_error 和结果 UTF-8 大小。不得默认记录 prompt、ReasoningPart 内容、ToolUse arguments、工具完整结果、Authorization、API key 或 Python stack。

实现 `sanitize_error(exc_or_info)`：输出固定 category、type、retryable、provider_code、status_code、request_id、cancelled 和最长 512 个字符的 message。message 删除控制字符，并按 key/token/password/authorization/bearer 等模式替换疑似 secret。完整 stack 只允许进入另行显式配置的 debug sink，本计划默认不创建该 sink。

实现 `JsonlTraceSink` 与包裹它的 `BestEffortTraceSink`。文件 sink 使用容量可配置的 `asyncio.Queue`，单后台 task 批量编码和追加；单文件达到默认 8 MiB 后关闭、flush、fsync 并切换到下一个六位编号文件。启动时从现有最高编号之后创建新文件，不修改旧 Trace。`emit()` 只执行非阻塞 `put_nowait`；队列满、编码失败、目录不可写或 writer task 失败时递增 dropped_count/failed_count 并返回。`close(drain_timeout=...)` 在有限时间内 drain，随后取消 writer 并允许丢弃尾部；多次 close 安全。Trace flush 可以提高可见性，但不能成为 Journal 的提交条件。

把 `miniagent/tools/models.py` 中的临时 TraceSink 与 `miniagent/tools/artifacts.py::MemoryTraceSink` 迁移到统一接口。修改 `ToolExecutor._trace()`，让 attempt_started、retry_scheduled 和 tool_finished 带完整 TraceContext；参数校验在 handler 前失败也必须形成 tool.call 终态。修改 AgentLoop 和 Provider 适配边界产生 run/turn/model 记录。若当前 Provider event 尚不暴露 request ID 或 usage，只扩展规范化终态字段并保持默认 None，不从原始响应或异常字符串中猜测。

新增 `tests/observability/test_trace_model.py`、`test_trace_sink.py` 和 `test_trace_integration.py`。验证 Span 父子关系、同一 writer 的 sequence 单调、并发 emit 不交错 JSON 行、轮转、默认内容缺失、脱敏、队列满丢弃、目录失败、writer 异常和 close 超时。故障测试必须同时断言 AgentRunResult 与无故障场景相同，且 Message Journal bytes 相同；不要只断言“没有抛异常”。

运行：

    cd D:\study\MiniAgent
    uv run python -m pytest -q tests/observability tests/tools/test_executor.py tests/test_loop.py

预期全部通过。打开生成的 trace JSONL 可以按 trace_id 得到 agent.run → agent.turn → model.call/tool.call 的父子结构；搜索测试 prompt、reasoning、tool arguments 和 tool result 原文均无匹配。

### Milestone 5: 端到端恢复、故障注入和收尾

新增 `tests/persistence/test_end_to_end.py`，用临时 sessions_root、确定性的 fake model 和有计数器的 fake tool 完成两次 AgentRun。第一次含 ToolUse/ToolResult，第二次正常文本结束。关闭所有 handle 和 Trace sink，再从全新的 Repository 实例打开，断言 Message 与 Part 的 ID、顺序、Reasoning、ToolUse 关联、ToolResult 和两个 run 终态完全相同，队列为空，fake model/tool 计数没有因恢复增加。

在同一测试模块覆盖四个故障场景。第一，首条 user_message 的 fsync 失败，不返回可用 Session，模型和工具计数均为零。第二，AssistantMessage fsync 失败，工具计数为零且重新打开依扫描结果决定该记录是否存在，不自动重试。第三，追加半条尾行模拟进程被杀，重新打开截断并追加 PROCESS_INTERRUPTED，不执行旧动作。第四，把 Trace 队列容量设为 1、阻塞 writer 后连续发送至少两个事件以制造溢出，或让 writer 目录不可写；AgentRun 仍 COMPLETED 且恢复结果相同。注意 `asyncio.Queue(maxsize=0)` 表示无界队列，不能用于队列满测试。

运行全套测试和静态搜索：

    cd D:\study\MiniAgent
    uv run python -m pytest -q
    rg -n "JsonlTranscriptStore|SessionEvent|event_id|journal_sequence|AssistantMessageStarted|AssistantPartDelta|AssistantMessageDiscarded" miniagent tests
    rg -n "prompt|reasoning|arguments|authorization|api[_-]?key|bearer" tests/observability

第一条命令必须至少保持计划编写时的 69 个旧测试并通过所有新增测试。第二条搜索允许在 runtime-only update 类型、兼容说明或明确的负向测试中出现旧词，但生产 Journal 写入路径不得出现。第三条用于人工复核脱敏测试覆盖，不代表简单禁用这些字段；最终还要检查生成的 trace fixture 中没有对应原文。

最后在临时目录运行一个最小演示脚本或端到端测试的 `-s` 版本，记录简短证据：Session 列表摘要、Journal record_type 序列、恢复消息数、锁竞争错误和 Trace 文件名。不得把临时 sessions 数据提交到仓库。

## Concrete Steps

所有命令从 `D:\study\MiniAgent` 运行。开始与每个里程碑结束时先执行：

    git status --short
    uv run python -m pytest -q

当前工作树在计划编写时已有与本任务无关的用户改动，包括 `PLANS.md` 修改、旧计划移入 `docs/design-docs/exec-plans/completed/` 以及若干未跟踪文件。实施者必须保留它们，不得 reset、restore、删除或纳入本任务的机械重写。编辑前用 `git diff -- <path>` 判断目标文件是否也有用户修改；若有重叠，做最小手工合并并在 Surprises & Discoveries 记录。

建议按以下新增文件顺序工作：

    miniagent/journal.py
    miniagent/repository.py
    miniagent/updates.py          # 仅当现有 events.py 无法清晰承载 runtime-only 通知时
    miniagent/trace.py
    tests/persistence/test_journal_codec.py
    tests/persistence/test_repository.py
    tests/persistence/test_session_recovery.py
    tests/persistence/test_end_to_end.py
    tests/observability/test_trace_model.py
    tests/observability/test_trace_sink.py
    tests/observability/test_trace_integration.py

每完成一个红绿循环就运行最窄测试，里程碑结束运行全套测试。不要依靠测试执行顺序、真实用户 Session 目录、网络或真实模型。时间、UUID、故障 writer 和 Trace queue 容量应通过构造参数或小型工厂注入，使测试确定。

## Validation and Acceptance

验收以行为为准，而不是仅检查类是否存在。

1. 创建 Session 时，首条用户消息成功 fsync 后 API 才返回；注入 fsync 失败时不调用模型、不调用工具、不出现可列出的空 Session。
2. 后续 QueuedInput 在出队前不出现在 message.jsonl；真正运行时，测试时间线证明 user record 的 fsync 返回先于 ModelAdapter.stream 进入。
3. AssistantMessage 以一条完整 record 提交。测试时间线证明它的 fsync 返回先于对应 ToolUse 执行；Journal 中没有 started、delta、discarded 或 ToolUseDetected record。
4. 关闭并从新 Repository 实例打开后，完整 Transcript、ContextSummary、自然身份和 run 终态确定恢复，内存队列为空；恢复不增加模型或工具调用计数。
5. 唯一无换行尾部片段可在持锁 open 时截断；中间 JSON/UTF-8/schema/关联损坏拒绝打开；list_sessions 仍返回其他健康 Session 并显示损坏条目。
6. 两个进程竞争同一 Session 时只有一个取得 writer lock。持有者关闭后另一个可以打开；仅遗留 writer.lock 文件不会永久阻止打开。
7. 未终止的最后 run 在首次恢复时追加 PROCESS_INTERRUPTED，第二次恢复不重复；任何时候都不重放模型或工具。
8. Trace 文件能表达 run/turn/model/tool 父子关系和关键元数据，默认不含 prompt、reasoning、工具参数或完整结果；错误消息限长且脱敏。
9. Trace 队列满、目录不可写、writer task 失败和 close 超时都不改变 Journal bytes 或 AgentRunResult；drop/failure 计数可供进程内诊断。
10. `uv run python -m pytest -q` 全部通过，且没有网络依赖或依靠固定 sleep 的并发测试。

## Idempotence and Recovery

Repository API 必须可安全重复调用，但业务追加不是盲目幂等操作。`create_session` 对既有 Session ID 明确失败，不覆盖内容；`open_session` 可重复打开已关闭的 Session，并且对同一个无换行尾部只截断一次。每个自然身份只允许一次，重复 message_id、summary_id、tool_use_id 或 run terminal 视为损坏或调用错误，不通过“发现重复就成功”掩盖未知的首次写入结果。

Journal append 失败后，调用方关闭 poisoned handle并重新 `open_session()` 完整扫描：若完整 record 已落盘，恢复会看到它；若没有，则恢复看不到。两种结果都由磁盘事实决定，不在原 handle 上重试。测试故障注入后必须关闭 fd 和锁，临时目录由 pytest tmp_path 清理。

Trace 可以安全重新启动：每次进程启动使用最高现有编号之后的新文件，永不覆盖旧文件。轮转或 close 中断留下的最后无换行 Trace 记录可以由诊断读取器忽略；因为 Trace 不参与恢复，不得为修 Trace 而修改 Journal。多次调用 sink.close 和 OpenSession.close 都应无害。

实现期间不要对用户已有工作树执行 `git reset --hard`、`git clean` 或整树格式化。若某个里程碑失败，保留已通过的较低层 codec/repository 测试，回退只针对本计划新改的具体文件，并在 Progress 中写明已完成与剩余部分。

## Artifacts and Notes

计划编写时的基线证据：

    PS D:\study\MiniAgent> uv run python -m pytest -q
    .....................................................................    [100%]
    69 passed in 1.08s

目标 Journal 的典型物理内容应类似以下三行；实际实现使用紧凑单行 JSON，这里仅展示字段语义：

    {"schema_version":1,"record_type":"user_message","session_id":"...","run_id":"...","occurred_at":"2026-07-23T00:00:00Z","payload":{"message":{...}}}
    {"schema_version":1,"record_type":"assistant_message","session_id":"...","run_id":"...","occurred_at":"2026-07-23T00:00:01Z","payload":{"message":{...},"finish_reason":"stop"}}
    {"schema_version":1,"record_type":"run_terminated","session_id":"...","run_id":"...","occurred_at":"2026-07-23T00:00:02Z","payload":{"reason":"COMPLETED","turn_count":1,"final_message_id":"...","error":null}}

目标 Trace 关系应可以还原为：

    agent.run
      agent.turn
        model.call
        tool.call

这里的缩进表示 parent_span_id，不表示 Journal 顺序。Trace 中的 session_id、run_id、message_id 和 tool_use_id 用于反查业务对象，但 Trace record 永远不能补充、删除或改写 Journal 事实。

## Interfaces and Dependencies

里程碑结束时，Repository 对外至少提供以下等价接口；类型可以因 Python async 细节微调，但语义不可缩窄：

    class SessionRepository:
        async def list_sessions(self) -> tuple[SessionSummary, ...]: ...
        async def create_session(
            self, session_id: UUID, first_user_record: JournalRecord
        ) -> OpenSession: ...
        async def open_session(self, session_id: UUID) -> OpenSession: ...

    class OpenSession:
        session_id: UUID
        recovered: RecoveredSession
        async def append(self, record: JournalRecord) -> None: ...
        async def close(self) -> None: ...

    @dataclass(frozen=True, slots=True)
    class SessionSummary:
        session_id: str
        name: str
        created_at: datetime | None
        last_user_input_at: datetime | None
        openable: bool
        error_category: str | None

SessionEngine 或供 AgentLoop 使用的协议至少暴露：

    class RunCommitter(Protocol):
        async def commit_assistant(
            self, run_id: UUID, message: Message, finish_reason: str | None
        ) -> None: ...
        async def commit_tool_result(self, run_id: UUID, message: Message) -> None: ...
        async def commit_context_summary(
            self, run_id: UUID, summary: ContextSummary
        ) -> None: ...
        async def finish_run(self, run_id: UUID, result: AgentRunResult) -> None: ...
        async def publish_live(self, update: object) -> None: ...

统一 Trace 边界至少提供：

    class TraceSink(Protocol):
        async def emit(self, event: TraceEvent) -> None: ...
        async def close(self, drain_timeout: float = 1.0) -> None: ...

    @dataclass(frozen=True, slots=True)
    class TraceContext:
        trace_id: UUID
        span_id: UUID
        parent_span_id: UUID | None
        session_id: UUID
        run_id: UUID
        message_id: UUID | None = None

除 Python 3.11 标准库和项目已有依赖外不新增运行时依赖。文件锁使用 `msvcrt`/`fcntl` 平台分支；JSON 使用标准库；异步队列和后台 writer 使用 asyncio。若实现者发现标准库锁在受支持平台无法满足非阻塞独占语义，先写可复现测试并更新 Decision Log，再考虑最小第三方依赖，不能悄悄改变依赖集。

## 修订说明

2026-07-23：创建初版计划。原因是将 `docs/design-docs/persistence-and-observability.md` 的目标设计转化为可逐步执行、可故障注入验证、并与当前 SessionEvent/JsonlTranscriptStore/工具 trace 实现对齐的中文 ExecPlan。同日补充未终止 run 的确定性 turn_count 规则，区分写入前 TraceEvent 与落盘 TraceRecord，并修正 asyncio 无界队列的测试说明，消除实施歧义。

2026-07-23：完成里程碑 1 和 2 后更新 Progress、Surprises & Discoveries 与 Outcomes，记录严格 Journal、Repository、Windows 进程内锁补充以及 `82 passed` 回归证据，使后续执行者可以从当前落点继续迁移 SessionEngine。

2026-07-23：完成里程碑 3 至 5，并根据 Standards/Spec 双轴审查补齐 FIFO 唯一 worker、Journal ErrorInfo 脱敏、Trace 关键元数据、稳定列表排序、缺终态与预取消 span 语义及故障验收。同步更新所有活文档章节和最终 `97 passed` 证据，原因是使本 ExecPlan 准确反映可恢复、可观测且已验证的当前实现。
