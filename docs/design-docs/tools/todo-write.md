# todo_write 工具设计

本文定义内置 `todo_write` 的稳定契约，以及它与应用级 TodoStore、AgentRun 提醒和 UI Projection 的边界。它服从
[`tool-design-guidelines.md`](../tool-design-guidelines.md)；框架执行语义以
[`tool-registry-and-execution.md`](../tool-registry-and-execution.md) 为准；动态提醒以
[`context-management.md`](../context-management.md) 和
[`main-loop.md`](../main-loop.md) 为准。

## 1. 目的与边界

`todo_write` 用一份完整 TodoList 原子替换当前 Session 的进程内任务状态，帮助 Agent 在较长任务中表达当前焦点和后续工作。它不是项目 TODO 文件、Message Journal 事实、历史消息摘要或跨进程任务系统。

Provider-visible description 固定为：

```text
Replace the current session's in-memory todo list with a structured task list.
```

## 2. TodoStore 与生命周期

composition root 创建一个应用级单例 `TodoStore`，内部按 `session_id` 保存不可变 TodoList。它是 TODO 状态的权威来源，通过窄 runtime capability 注入工具和 Session 投影；handler 不导入模块级字典，也不能枚举或修改其他 Session。

TodoStore 的生命周期是当前应用进程：

- 同一进程内切换、关闭并重新打开同一 Session 时保留列表；
- `/clear` 创建的新 Session 使用新的空列表，但旧 Session 的值仍可在本进程重开时取得；
- 应用退出后全部丢失；
- Message Journal 恢复不得从历史 `todo_write` ToolResult 重建 TodoStore；
- 关闭 Session 不自动清理，`todos=[]` 是显式清空方式。

## 3. ToolInput

```python
class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    content: str
    status: Literal["pending", "in_progress", "completed"]

class TodoWriteInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    todos: list[TodoItem] = Field(max_length=100)
```

- id 长度 1–64，只允许 ASCII 字母、数字、`_` 和 `-`，列表内唯一；
- content 长度 1–500 个 Unicode 字符，trim 后不得为空，但保存模型提交的原字符串；
- 最多一个 item 可以是 `in_progress`；允许全部 pending、全部 completed 或空列表；
- 状态可以回退，任务可以重开或重新规划；
- todos 的规范 JSON UTF-8 大小不得超过 32 KiB；超限是 `invalid_arguments`，不截断；
- 数组顺序是业务顺序，TodoStore 和 UI 不自动按 id 或 status 排序。

每次调用提交完整列表并原子替换旧值。`todos=[]` 表示清空，不提供 add/update/delete 操作集合。

## 4. ToolTarget、能力与执行

resolver 产生固定目标：

```text
kind=session_state
capability=write
scope=exact
value=todos
```

`session_state` 隐式绑定 Executor 当前 session_id，模型参数不能指定 Session。Target Authorization 自动允许 Current Session 的精确内部状态目标，不产生文件 permission；runtime capability 仍把访问限制在当前 Session。

classifier 固定返回 `concurrency_safe=False`。ToolSpec timeout 为 5 秒，RetryPolicy 为单次 attempt。TodoStore 的 replace 必须在线性化点一次交换完整不可变列表；验证或能力错误时旧列表不变。

## 5. ToolOutput 与 UI Projection

TodoItem 与 TodoList 是 ToolOutput、SessionSnapshot 和 SessionUpdate 共用的稳定模型：

```python
class TodoWriteMetadata(BaseModel):
    total_count: int
    pending_count: int
    in_progress_count: int
    completed_count: int

class TodoWriteData(BaseModel):
    todos: list[TodoItem]

class TodoWriteOutput(ToolOutput):
    metadata: TodoWriteMetadata
    data: TodoWriteData
```

content 只返回摘要：

```text
Todo list updated: 3 total, 1 in progress, 2 pending, 0 completed.
```

完整列表只出现于 `data.todos`，不在 content 中重复。成功 ToolResult 被 SessionEngine 接受后发布 `TodosChanged(todo_list)`；`SessionSnapshot.todo_list` 从 TodoStore 读取当前值。前端消费类型化数据，不解析 content 或 Tool Presentation。ToolOutput、Update 与 Snapshot 必须复制或共享同一不可变值，不能出现排序或字段差异。

TodoStore 是工具副作用，和文件写入一样发生在 ToolResult 提交之前。若后续 Journal 提交失败，进程内 TodoStore 不回滚；Session 按既有持久化失败规则停止，之后 snapshot 仍以 TodoStore 为准。

## 6. 长任务提醒

AgentLoop 为每个 AgentRun 持有 `model_calls_since_todo_write`，不把计数写入 TodoStore、Journal、Trace 业务字段或下一个 AgentRun：

1. AgentRun 开始时为 0；
2. 每个实际完成的 ModelCall 加 1，压缩模型调用不计数；
3. 成功的 `todo_write` ToolResult 被 SessionEngine 接受后清零；失败、拒绝、取消或未提交结果不清零；
4. 下一次 ModelCall 前若已有 10 次连续 ModelCall 未成功使用 `todo_write`，本轮 ToolView 仍提供该工具，且 TodoList 至少有一个 pending 或 in_progress item，则从第 11 次 ModelCall 开始注入提醒；
5. 阈值满足后每次 ModelCall 都注入，直到成功调用清零；
6. 空列表或全部 completed 时不注入；阈值前不注入 TodoList，依靠 Agent 的短期上下文。

动态 Prompt 固定使用：

```text
The TodoWrite tool hasn't been used recently. If you're working on tasks that
would benefit from tracking progress, consider using the TodoWrite tool...

Here are the existing contents of your todo list:

[1. [in_progress] Add dark mode toggle
2. [pending] Run tests and build]
```

实际列表按 TodoStore 顺序编号并使用英文 status 字面量。提醒由 AgentLoop 依据计数和 TodoStore 投影为不可变 `TodoReminder`，ContextManager 只负责按固定位置格式化；它不是静态工具 Prompt、Tool Recovery 或历史 Message，也不参与上下文压缩。

## 7. Prompt

```python
PROMPT = """Purpose:
Replace the current session's in-memory todo list to track progress on a multi-step task.

Use when:
- The current task has several meaningful steps or needs explicit progress tracking.
- You need to update the current focus, mark work complete, or revise the remaining plan.

Rules:
- Submit the complete desired list on every call; an empty list clears it.
- Keep item ids stable when updating the same task.
- Use at most one `in_progress` item and preserve the intended display order.

Returns:
- A summary and the complete structured todo list used by the session UI.

If it fails:
- Correct duplicate ids, multiple in-progress items, invalid fields, or an oversized list and submit the complete list again.
"""
```

## 8. 验收不变量

- TodoStore 是应用级注入能力，不是模块全局变量或 Journal 投影；
- 同一进程重开 Session 保留，进程重启丢失且不从历史 ToolResult 恢复；
- schema、id/content/status、唯一性、最多一个 in_progress、100 项和 32 KiB 约束准确；
- 每次调用原子替换完整列表，失败时旧值不变，空列表清空；
- session_state target 只绑定 Current Session，调用串行且不重试；
- Output、TodosChanged、Snapshot 使用同一模型和顺序，UI 不解析 content；
- 第 1–10 次 ModelCall 不注入，第 11 次起只在工具可见且列表非空未完成时注入，成功 ToolResult 提交后清零；
- 新 AgentRun 重新计数，压缩调用不计数，全 completed 和空列表不提醒；
- TodoList、runtime capability 和 Session 内存状态不泄露到不相关 Session。
