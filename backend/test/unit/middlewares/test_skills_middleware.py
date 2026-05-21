from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

import yuxi.agents.middlewares.skills_middleware as skills_middleware
from yuxi.agents.middlewares.skills_middleware import SkillsMiddleware, resolve_runtime_skills_for_context


@pytest.mark.asyncio
async def test_resolve_runtime_skills_derives_prompt_and_readable_closure(monkeypatch):
    async def fake_get_dependency_map(db=None):
        del db
        return {
            "alpha": {"tools": [], "mcps": [], "skills": ["beta"]},
            "beta": {"tools": [], "mcps": [], "skills": []},
        }

    monkeypatch.setattr(skills_middleware, "get_dependency_map", fake_get_dependency_map)

    context = SimpleNamespace(skills=["alpha", "missing"])

    scope = await resolve_runtime_skills_for_context(context)

    assert scope == {
        "context_skills": ["alpha"],
        "prompt_skills": ["alpha", "beta"],
        "readable_skills": ["alpha", "beta"],
    }


@pytest.mark.asyncio
async def test_skills_prompt_uses_prepared_prompt_skills(monkeypatch):
    async def fake_get_prompt_metadata(db=None):
        del db
        return {
            "alpha": {
                "name": "Alpha",
                "description": "alpha desc",
                "path": "/home/gem/skills/alpha/SKILL.md",
            },
            "configured-only": {
                "name": "Configured Only",
                "description": "should not appear",
                "path": "/home/gem/skills/configured-only/SKILL.md",
            },
        }

    monkeypatch.setattr(skills_middleware, "get_prompt_metadata", fake_get_prompt_metadata)

    context = SimpleNamespace(
        system_prompt="base",
        skills=["configured-only"],
        _prompt_skills=["alpha"],
    )

    await SkillsMiddleware().abefore_agent({}, SimpleNamespace(context=context))

    assert "base" in context.system_prompt
    assert "Alpha" in context.system_prompt
    assert "Configured Only" not in context.system_prompt
    assert getattr(context, "_skills_prompt_injected") is True
    assert not hasattr(context, "_visible_skills")


@pytest.mark.asyncio
async def test_awrap_model_call_mounts_dependencies_only_for_readable_activated_skills(monkeypatch):
    async def fake_get_dependency_map(db=None):
        del db
        return {
            "alpha": {"tools": ["tool-a"], "mcps": [], "skills": []},
            "beta": {"tools": ["tool-b"], "mcps": [], "skills": []},
        }

    monkeypatch.setattr(skills_middleware, "get_dependency_map", fake_get_dependency_map)
    monkeypatch.setattr(
        skills_middleware,
        "get_all_tool_instances",
        lambda: [SimpleNamespace(name="tool-a"), SimpleNamespace(name="tool-b")],
    )

    class FakeRequest:
        def __init__(self, tools=None):
            self.runtime = SimpleNamespace(context=SimpleNamespace(_readable_skills=["alpha"], mcps=[]))
            self.state = {"activated_skills": ["alpha", "beta"]}
            self.tools = tools or []

        def override(self, *, tools):
            new_request = FakeRequest(tools=tools)
            new_request.runtime = self.runtime
            new_request.state = self.state
            return new_request

    captured = {}

    async def handler(request):
        captured["tools"] = [tool.name for tool in request.tools]
        return "ok"

    result = await SkillsMiddleware().awrap_model_call(FakeRequest(), handler)

    assert result == "ok"
    assert captured["tools"] == ["tool-a"]


def test_read_file_activates_only_readable_skill() -> None:
    middleware = SkillsMiddleware()
    result = ToolMessage(content="ok", tool_call_id="tool-1", name="read_file")
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=SimpleNamespace(_readable_skills=["alpha"])),
        tool_call={"name": "read_file", "args": {"file_path": "/home/gem/skills/alpha/SKILL.md"}},
    )

    updated = middleware._process_tool_call_result(result, request)

    assert isinstance(updated, Command)
    assert updated.update["activated_skills"] == ["alpha"]


def test_read_file_denies_skill_outside_readable_scope() -> None:
    middleware = SkillsMiddleware()
    result = ToolMessage(content="ok", tool_call_id="tool-1", name="read_file")
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=SimpleNamespace(_readable_skills=["alpha"])),
        tool_call={"name": "read_file", "args": {"file_path": "/home/gem/skills/beta/SKILL.md"}},
    )

    updated = middleware._process_tool_call_result(result, request)

    assert updated is result
