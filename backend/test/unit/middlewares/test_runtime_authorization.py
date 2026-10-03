"""已注册工具也必须符合最新授权与 Skill 激活状态。"""

from types import SimpleNamespace

import pytest

from yuxi.modules.agents.runtime.middlewares.authorization import tool_allowed


@pytest.fixture
def context():
    """构图时注册工具、执行时权限独立变化的 Context。"""
    return SimpleNamespace(
        tools=[],
        mcps=[],
        _registered_builtin_tool_names={"skill_tool"},
        _mcp_tool_servers={"remote_tool": "remote"},
        _enabled_mcps={"remote": set()},
        _skill_runtime_snapshot={
            "effective_skills": ["skill"],
            "preloaded_skills": [],
            "runtime_skills": {"skill": {"tools": ["skill_tool"], "mcps": ["remote"]}},
        },
    )


def test_unactivated_skill_cannot_execute_registered_local_or_mcp_dependency(context):
    """直接调用隐藏工具不能代替读取或预加载 Skill。"""
    assert not tool_allowed(context, "skill_tool", {})
    assert not tool_allowed(context, "remote_tool", {})
    assert tool_allowed(context, "skill_tool", {"activated_skills": ["skill"]})
    context._skill_runtime_snapshot["effective_skills"] = []
    assert not tool_allowed(context, "skill_tool", {"activated_skills": ["skill"]})


def test_mcp_single_tool_or_server_disable_rejects_old_registration(context):
    """启停服务器和单工具都不能被既有对象绕过。"""
    state = {"activated_skills": ["skill"]}
    assert tool_allowed(context, "remote_tool", state)
    context._enabled_mcps["remote"] = {"remote_tool"}
    assert not tool_allowed(context, "remote_tool", state)
    context._enabled_mcps.clear()
    assert not tool_allowed(context, "remote_tool", state)
