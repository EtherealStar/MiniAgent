import pytest

from miniagent.hooks import HookRegistry, HookRegistryError
from miniagent.hooks import FastToolValidationHook


async def hook(context):
    return None


def test_registry_freezes_order_and_reuses_view():
    registry = HookRegistry()
    registry.register_pre_model_call(hook)
    registry.register_pre_model_call(hook)

    view = registry.freeze()

    assert view.pre_model_call == (hook, hook)
    assert registry.freeze() is view
    with pytest.raises(HookRegistryError, match="冻结"):
        registry.register_pre_model_call(hook)


def test_registry_rejects_non_async_hook_and_allows_empty_view():
    registry = HookRegistry()
    with pytest.raises(HookRegistryError, match="异步"):
        registry.register_pre_tool_use(lambda context: None)

    view = registry.freeze()
    assert view.pre_model_call == ()
    assert view.assistant_message_completed == ()
    assert view.pre_tool_use == ()
    assert view.post_tool_use == ()


def test_registry_accepts_async_callable_instances():
    registry = HookRegistry()
    registry.register_pre_tool_use(FastToolValidationHook())
    assert registry.freeze().pre_tool_use[0].__class__ is FastToolValidationHook
