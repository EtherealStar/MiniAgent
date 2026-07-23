# MiniAgent

MiniAgent 是一个使用 Textual 构建的本地终端 Agent Runtime。

## 开发与验证

项目要求 Python 3.11，并使用 `uv` 管理环境。从仓库根目录运行：

```powershell
uv run python -m compileall miniagent tests main.py
uv run python -m pytest -q
uv run python -m miniagent.ui
```

测试不访问真实模型、网络、凭据或用户 Session 数据。

## 生命周期 Hook

Hook 必须在 composition root 启动阶段按生命周期分别注册，然后显式冻结：

```python
from miniagent.hooks import FastToolValidationHook, HookDispatcher, HookRegistry

registry = HookRegistry()
registry.register_pre_tool_use(FastToolValidationHook())
dispatcher = HookDispatcher(registry.freeze())
```

冻结后的注册顺序稳定且不能再修改。四类 Hook Context 都是不可变快照，不提供
SessionEngine、Repository 或 ToolExecutor 的可变能力。`PreModelCall` 与
`PreToolUse` 可以返回强类型控制决定；`AssistantMessageCompleted` 与
`PostToolUse` 是提交后的通知，异常只写入非权威 Trace，不回滚已经接受的事实。
空 Registry 合法，等同于所有控制点继续且没有通知。
