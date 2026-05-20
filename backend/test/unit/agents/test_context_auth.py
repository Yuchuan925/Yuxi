from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest


def _load_context_module():
    context_path = Path(__file__).resolve().parents[3] / "package/yuxi/agents/context.py"
    previous_yuxi = sys.modules.get("yuxi")
    sys.modules["yuxi"] = types.SimpleNamespace(config=types.SimpleNamespace(default_model="test:model"))
    try:
        spec = importlib.util.spec_from_file_location("test_yuxi_agents_context", context_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_yuxi is None:
            sys.modules.pop("yuxi", None)
        else:
            sys.modules["yuxi"] = previous_yuxi


context_module = _load_context_module()
BaseContext = context_module.BaseContext
filter_config_by_role = context_module.filter_config_by_role
normalize_agent_context_config = context_module.normalize_agent_context_config


@dataclass
class SuperAdminOnlyContext(BaseContext):
    secret_setting: str = field(default="hidden", metadata={"name": "Secret", "auth": "superadmin"})


def test_get_configurable_items_filters_admin_fields_for_user():
    items = BaseContext.get_configurable_items(user_role="user")

    assert "system_prompt" in items
    assert "summary_threshold" not in items


def test_get_configurable_items_allows_admin_and_superadmin_fields():
    admin_items = BaseContext.get_configurable_items(user_role="admin")
    superadmin_items = SuperAdminOnlyContext.get_configurable_items(user_role="superadmin")

    assert "summary_threshold" in admin_items
    assert "secret_setting" in superadmin_items


def test_filter_config_by_role_removes_unauthorized_context_values():
    config_json = {
        "context": {
            "system_prompt": "visible",
            "summary_threshold": 10,
            "secret_setting": "nope",
        },
        "other": {"keep": True},
    }

    filtered = filter_config_by_role(config_json, "user", context_schema=SuperAdminOnlyContext)

    assert filtered == {"context": {"system_prompt": "visible"}, "other": {"keep": True}}
    assert config_json["context"]["summary_threshold"] == 10


def test_filter_config_by_role_keeps_admin_context_values_for_admin():
    filtered = filter_config_by_role(
        {"context": {"summary_threshold": 10, "secret_setting": "nope"}},
        "admin",
        context_schema=SuperAdminOnlyContext,
    )

    assert filtered == {"context": {"summary_threshold": 10}}


@pytest.mark.asyncio
async def test_normalize_agent_context_config_expands_null_and_filters_explicit_lists(monkeypatch):
    async def fake_get_databases_by_user(_user):
        return {"databases": [{"db_id": "kb-a"}, {"db_id": "kb-b"}]}

    async def fake_get_enabled_mcp_server_names(db=None):
        del db
        return ["mcp-a", "mcp-b"]

    async def fake_list_skill_slugs(_db):
        return ["skill-a", "skill-b"]

    async def fake_get_enabled_subagent_names(_db=None):
        return ["research-agent", "critique-agent"]

    monkeypatch.setitem(
        sys.modules,
        "yuxi.agents.toolkits",
        types.SimpleNamespace(
            get_all_tool_instances=lambda: [
                types.SimpleNamespace(name="ask_user_question"),
                types.SimpleNamespace(name="tavily_search"),
            ]
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "yuxi.knowledge",
        types.SimpleNamespace(knowledge_base=types.SimpleNamespace(get_databases_by_user=fake_get_databases_by_user)),
    )
    monkeypatch.setitem(
        sys.modules,
        "yuxi.services.mcp_service",
        types.SimpleNamespace(get_enabled_mcp_server_names=fake_get_enabled_mcp_server_names),
    )
    monkeypatch.setitem(
        sys.modules,
        "yuxi.services.skill_service",
        types.SimpleNamespace(list_skill_slugs=fake_list_skill_slugs),
    )
    monkeypatch.setitem(
        sys.modules,
        "yuxi.services.subagent_service",
        types.SimpleNamespace(get_enabled_subagent_names=fake_get_enabled_subagent_names),
    )

    normalized = await normalize_agent_context_config(
        {
            "tools": None,
            "knowledges": ["kb-b", "missing", "kb-b"],
            "mcps": None,
            "skills": [],
            "subagents": ["research-agent", "missing"],
            "summary_threshold": 10,
        },
        db=object(),
        user=types.SimpleNamespace(role="user", uid="u1", department_id=None),
    )

    assert normalized["tools"] == ["ask_user_question", "tavily_search"]
    assert normalized["knowledges"] == ["kb-b"]
    assert normalized["mcps"] == ["mcp-a", "mcp-b"]
    assert normalized["skills"] == []
    assert normalized["subagents"] == ["research-agent"]
    assert "summary_threshold" not in normalized
