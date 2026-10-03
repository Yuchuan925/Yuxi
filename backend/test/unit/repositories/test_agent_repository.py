from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yuxi.modules.agents.runtime.agent_backends.chatbot.context import ChatBotContext

from yuxi.modules.agents.presets.subagents.general_purpose import PRESET as GENERAL_PURPOSE
from yuxi.modules.agents.repositories.definitions import (
    AgentRepository,
    DEFAULT_AGENT_DESCRIPTION,
    DEFAULT_SHARE_CONFIG,
    SUB_AGENT_BACKEND_ID,
    merge_agent_config_json,
    user_can_access_agent,
    user_can_manage_agent,
)
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.identity.models import User


class FakeDb:
    def __init__(self):
        self.added = None
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    def add(self, item):
        self.added = item


_MANAGER_USER_SCOPE = {
    "version": 2,
    "read_scope": {"access_level": "user", "user_uids": ["manager"]},
    "manage_scope": {"access_level": "user", "user_uids": ["manager"]},
}


def _agent_for_update(*, slug="shared-bot", name="Shared Bot", created_by="owner", share_config=_MANAGER_USER_SCOPE):
    return SimpleNamespace(
        slug=slug,
        backend_id="ChatbotAgent",
        share_config=share_config,
        created_by=created_by,
        updated_by=None,
        updated_at=None,
        name=name,
        description="",
        icon=None,
        pics=[],
        config_json={},
    )


def test_merge_agent_config_json_preserves_omitted_context_fields_and_hidden_skills():
    """省略字段和不可见 Skill 引用均保持原值。"""
    existing = {
        "context": {
            "model": "provider:model-a",
            "skills": [f"skill-{index}" for index in range(10)],
        },
        "metadata": {"source": "owner"},
    }

    merged = merge_agent_config_json(
        existing,
        {
            "context": {
                "temperature": 0.2,
                "skills": [f"skill-{index}" for index in range(5)],
            }
        },
        resource_access={
            "skills": {
                *(f"skill-{index}" for index in range(5)),
                "visible-extra-a",
                "visible-extra-b",
                "visible-extra-c",
            }
        },
    )

    assert merged == {
        "context": {
            "model": "provider:model-a",
            "temperature": 0.2,
            "skills": [f"skill-{index}" for index in range(10)],
        },
        "metadata": {"source": "owner"},
    }
    assert existing["context"]["skills"] == [f"skill-{index}" for index in range(10)]


def test_merge_agent_config_json_applies_visible_edits_and_preserves_hidden_references():
    """可见增删保留交错引用顺序，新选择追加到末尾。"""
    merged = merge_agent_config_json(
        {"context": {"skills": ["visible-a", "hidden-a", "visible-b", "hidden-b"]}},
        {"context": {"skills": ["visible-b", "visible-c", "hidden-a"]}},
        resource_access={"skills": {"visible-a", "visible-b", "visible-c"}},
    )

    assert merged["context"]["skills"] == ["hidden-a", "visible-b", "hidden-b", "visible-c"]


@pytest.mark.parametrize("strategy", ["all", []])
def test_merge_agent_config_json_replaces_resource_list_for_explicit_strategy_switch(strategy):
    """显式空列表或 all 整体切换资源策略。"""
    merged = merge_agent_config_json(
        {"context": {"skills": ["visible", "hidden"], "subagents": ["visible-subagent", "hidden-subagent"]}},
        {"context": {"skills": strategy, "subagents": strategy}},
        resource_access={"skills": {"visible"}, "subagents": {"visible-subagent"}},
        context_schema=ChatBotContext,
    )

    assert merged["context"]["skills"] == strategy
    assert merged["context"]["subagents"] == strategy


def test_merge_agent_config_json_rejects_new_unauthorized_resource_reference():
    """新增无权引用必须拒绝，不能借既有隐藏引用绕过。"""
    with pytest.raises(ValueError, match="无权新增.*skills.*hidden-new"):
        merge_agent_config_json(
            {"context": {"skills": ["visible", "hidden-existing"]}},
            {"context": {"skills": ["visible", "hidden-existing", "hidden-new"]}},
            resource_access={"skills": {"visible"}},
        )


def test_merge_agent_config_json_requires_authorization_result_for_resource_write():
    """缺少权限解析结果的直接资源写入必须拒绝。"""
    with pytest.raises(ValueError, match="skills 未经过权限校验"):
        merge_agent_config_json(
            {"context": {"skills": []}},
            {"context": {"skills": ["new-skill"]}},
            resource_access={},
        )


def test_merge_agent_config_json_validates_preload_skills_independently():
    """预加载选择独立保留不可见引用，不修改 Skill 允许列表。"""
    merged = merge_agent_config_json(
        {
            "context": {
                "skills": ["visible", "hidden"],
                "preload_skills": ["visible", "hidden"],
            }
        },
        {"context": {"preload_skills": ["visible"]}},
        resource_access={"preload_skills": {"visible"}},
    )

    assert merged["context"]["skills"] == ["visible", "hidden"]
    assert merged["context"]["preload_skills"] == ["visible", "hidden"]


@pytest.mark.asyncio
async def test_ensure_default_agent_creates_description(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)

    async def get_by_slug(_slug):
        return None

    monkeypatch.setattr(repo, "get_by_slug", get_by_slug)

    agent = await repo.ensure_default_agent()

    assert agent.description == DEFAULT_AGENT_DESCRIPTION
    assert agent.config_json == {"context": {}}
    assert db.added is agent
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_ensure_default_agent_backfills_missing_description(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)
    agent = SimpleNamespace(
        share_config=DEFAULT_SHARE_CONFIG.copy(),
        is_default=True,
        description=None,
        updated_by=None,
        updated_at=None,
    )

    async def get_by_slug(_slug):
        return agent

    monkeypatch.setattr(repo, "get_by_slug", get_by_slug)

    result = await repo.ensure_default_agent(created_by="admin")

    assert result is agent
    assert agent.description == DEFAULT_AGENT_DESCRIPTION
    assert agent.updated_by == "admin"
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_ensure_preset_creates_empty_config_subagent(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)

    async def get_by_slug(_slug):
        return None

    monkeypatch.setattr(repo, "get_by_slug", get_by_slug)

    agent = await repo.ensure_preset(GENERAL_PURPOSE, created_by="system")

    assert agent.slug == GENERAL_PURPOSE.slug
    assert agent.name == GENERAL_PURPOSE.name
    assert agent.description == GENERAL_PURPOSE.description
    assert agent.backend_id == SUB_AGENT_BACKEND_ID
    assert agent.is_subagent is True
    assert agent.is_default is False
    assert agent.config_json == {"context": {}}
    assert agent.share_config == DEFAULT_SHARE_CONFIG
    assert agent.created_by == "system"
    assert db.added is agent
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(agent)


@pytest.mark.asyncio
async def test_ensure_preset_is_idempotent(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)
    existing = SimpleNamespace(slug=GENERAL_PURPOSE.slug, config_json={"context": {"model": "custom:model"}})

    async def get_by_slug(_slug):
        return existing

    monkeypatch.setattr(repo, "get_by_slug", get_by_slug)

    agent = await repo.ensure_preset(GENERAL_PURPOSE)

    assert agent is existing
    assert db.added is None
    db.commit.assert_not_awaited()
    db.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_agent_defaults_to_private_without_grants(monkeypatch):
    db = FakeDb()
    repo = AgentRepository(db)
    monkeypatch.setattr(repo, "_unique_slug", AsyncMock(return_value="personal-bot"))
    user = User(uid="user", role="user", user_kind="human", is_deleted=0)
    agent = await repo.create(name="Personal Bot", backend_id="ChatbotAgent", created_by="user", creator=user)
    assert agent.visibility == "private"
    assert agent.share_config == {"version": 2, "read_scope": None, "manage_scope": None}
    assert db.added is agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"visibility": "shared"}, "共享"),
        ({"share_config": DEFAULT_SHARE_CONFIG}, "共享授权"),
        ({"backend_id": "SubAgentBackend"}, "SubAgent"),
        ({"created_by": "other"}, "所有者"),
    ],
)
async def test_normal_user_cannot_write_shared_or_subagent_definition(kwargs, reason):
    db = FakeDb()
    user = User(uid="user", role="user", user_kind="human", is_deleted=0)
    args = {"name": "Bot", "backend_id": "ChatbotAgent", "created_by": "user", "creator": user, **kwargs}
    with pytest.raises(ValueError, match=reason):
        await AgentRepository(db).create(**args)
    assert db.added is None
    db.commit.assert_not_awaited()


def test_user_shared_agent_is_read_only_even_when_management_scope_matches():
    user = User(uid="user", role="user", department_id=1)
    agent = Agent(
        slug="shared-bot",
        visibility="shared",
        created_by="other",
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        },
    )
    assert user_can_access_agent(user, agent)
    assert not user_can_manage_agent(user, agent)


@pytest.mark.asyncio
async def test_delegated_admin_update_preserves_shared_agent_acl():
    db = FakeDb()
    agent = _agent_for_update()
    agent.id = 1
    agent.visibility = "shared"
    db.scalar = AsyncMock(side_effect=[agent, None])
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: ["manager"]))
    user = User(uid="manager", role="admin", user_kind="human", is_deleted=0)
    await AgentRepository(db).update(agent, share_config=_MANAGER_USER_SCOPE, updater=user)
    assert agent.share_config["manage_scope"]["user_uids"] == ["manager"]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_user_cannot_update_private_agent_grants():
    db = FakeDb()
    agent = _agent_for_update(created_by="manager")
    agent.id = 1
    agent.visibility = "private"
    db.scalar = AsyncMock(return_value=agent)
    user = User(uid="manager", role="user", user_kind="human", is_deleted=0)
    with pytest.raises(ValueError, match="私有智能体不接受共享授权"):
        await AgentRepository(db).update(agent, share_config=_MANAGER_USER_SCOPE, updater=user)
    assert agent.share_config == _MANAGER_USER_SCOPE
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("field", ["tools", "knowledges", "skills", "subagents", "mcps", "preload_skills"])
@pytest.mark.parametrize("invalid", [None, "full", ["ok", 1], [""], {"mode": "all"}])
def test_resource_write_rejects_invalid_selection(field, invalid):
    """替代写入路径同样拒绝非法资源配置。"""
    with pytest.raises(ValueError, match=field):
        merge_agent_config_json({}, {"context": {field: invalid}}, resource_access={}, context_schema=ChatBotContext)


def test_all_selection_is_not_a_previous_reference_list():
    """all 不授予新增不可见引用的权限。"""
    with pytest.raises(ValueError, match="无权新增"):
        merge_agent_config_json(
            {"context": {"skills": "all"}}, {"context": {"skills": ["a"]}}, resource_access={"skills": set()}
        )
    merged = merge_agent_config_json(
        {"context": {"skills": "all", "tools": "all"}},
        {"context": {"skills": ["a"]}},
        resource_access={"skills": {"a"}},
    )
    assert merged == {"context": {"skills": ["a"], "tools": "all"}}


@pytest.mark.parametrize("field", ["mcps", "preload_skills"])
def test_opt_in_resource_selection_preserves_all_intent(field):
    """默认关闭的资源同样可显式选择全部，且不展开持久化。"""
    merged = merge_agent_config_json({}, {"context": {field: "all"}}, resource_access={})
    assert merged == {"context": {field: "all"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_id,is_subagent", [("ChatbotAgent", False), ("SubAgentBackend", True)])
async def test_serialize_agent_omits_capabilities(backend_id, is_subagent):
    """主子智能体响应保留权限与元信息且不再声明静态能力。"""
    agent = Agent(
        slug="test-agent",
        name="测试智能体",
        backend_id=backend_id,
        is_subagent=is_subagent,
        created_by="owner",
        visibility="shared" if is_subagent else "private",
        config_json={"context": {}},
        share_config=DEFAULT_SHARE_CONFIG.copy(),
    )
    user = User(uid="owner", username="owner", role="user", password_hash="test")

    result = await AgentRepository(FakeDb()).serialize(agent, user=user)

    assert result["backend_id"] == backend_id
    assert result["is_subagent"] is is_subagent
    assert result["name"] == "测试智能体"
    assert result["can_manage"] is (not is_subagent)
    assert "metadata" in result
    assert "capabilities" not in result
