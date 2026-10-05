"""一次创建的真实 HTTP、PostgreSQL 与文件提交证据。"""

import json
import os
import uuid
from io import BytesIO
from zipfile import ZipFile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.infrastructure.runtime_settings import get_skill_data_dir
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.services.definitions import create_agent_definition
from yuxi.modules.extensions.mcp.models import MCPServer
from yuxi.modules.extensions.skills.models import Skill
from yuxi.modules.identity.models import User

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def sessions():
    """独立连接读取已提交结果，避免把同一 session 的对象当事实。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def bundle(*, mcp=None, invalid=False):
    """构造根指南与辅助文件，依赖由实际包 parser 解析。"""
    if invalid:
        return b"not-a-zip"
    result = BytesIO()
    dependencies = f"mcp_dependencies:\n- {mcp}\n" if mcp else ""
    with ZipFile(result, "w") as archive:
        archive.writestr(
            "guide/SKILL.md",
            "---\nslug: guide\nname: Guide\ndescription: imported\n" + dependencies + "---\n# Imported guide\n",
        )
        archive.writestr("guide/references/note.txt", "CREATION_REFERENCE")
    return result.getvalue()


def remote_config(slug):
    """远程资源使用保留域名与占位请求头。"""
    return {
        "slug": slug,
        "name": slug,
        "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "headers": {"X-Test": "fixture-only"},
    }


async def assert_absent(sessions, agent_slug, mcp_slug):
    """提交失败后重新查询 PostgreSQL 的所有关联结果。"""
    async with sessions() as db:
        assert await db.scalar(select(Agent.id).where(Agent.slug == agent_slug)) is None
        assert await db.scalar(select(MCPServer.id).where(MCPServer.slug == mcp_slug)) is None
        assert (
            await db.scalar(
                select(Skill.id).join(Agent, Skill.bound_agent_id == Agent.id).where(Agent.slug == agent_slug)
            )
            is None
        )


async def test_create_with_skill_and_imported_mcp(test_client, admin_headers, standard_user, sessions):
    """回读完整包、显式 MCP 选择和脱敏结果，普通用户可复用已启用 MCP。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    suffix = uuid.uuid4().hex[:10]
    slug, mcp_slug = f"pytest-create-{suffix}", f"pytest-mcp-{suffix}"
    other_slug = f"pytest-other-{suffix}"
    try:
        response = await test_client.post(
            "/api/agent/with-skill",
            headers=admin_headers,
            data={"agent": json.dumps({"slug": slug, "name": "Guide Agent", "mcp_servers": [remote_config(mcp_slug)]})},
            files={"file": ("guide.zip", bundle(mcp=mcp_slug), "application/zip")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["agent"]["agent_id"] == slug
        read = await test_client.get(f"/api/agent/{slug}", headers=admin_headers)
        assert read.json()["agent"]["config_json"]["context"]["mcps"] == [mcp_slug]
        async with sessions() as db:
            agent = await db.scalar(select(Agent).where(Agent.slug == slug))
            skill = await db.scalar(select(Skill).where(Skill.bound_agent_id == agent.id))
            server = await db.scalar(select(MCPServer).where(MCPServer.slug == mcp_slug))
            assert server.enabled == 1 and server.headers == {"X-Test": "fixture-only"}
            assert server.created_by == server.updated_by == me.json()["username"]
            assert skill.mcp_dependencies == [mcp_slug]
            assert (get_skill_data_dir() / skill.dir_path / "references/note.txt").read_text() == "CREATION_REFERENCE"
            assert f"slug: {skill.slug}" in (get_skill_data_dir() / skill.dir_path / "SKILL.md").read_text()
        public = await test_client.get("/api/system/mcp-servers", headers=standard_user["headers"])
        server = next(item for item in public.json()["data"] if item["slug"] == mcp_slug)
        assert "headers" not in server and "url" not in server
        ordinary = await test_client.post(
            "/api/agent",
            headers=standard_user["headers"],
            json={"slug": other_slug, "name": "Existing MCP", "config_json": {"context": {"mcps": [mcp_slug]}}},
        )
        assert ordinary.status_code == 200, ordinary.text
        read = await test_client.get(f"/api/agent/{other_slug}", headers=standard_user["headers"])
        assert read.json()["agent"]["config_json"]["context"]["mcps"] == [mcp_slug]
        assert (await test_client.get(f"/api/agent/{other_slug}/self-skill", headers=standard_user["headers"])).json()[
            "skill"
        ] is None
    finally:
        for agent_slug in (slug, other_slug):
            result = await test_client.delete(f"/api/agent/{agent_slug}", headers=admin_headers)
            assert result.status_code in {200, 404}, result.text
        result = await test_client.delete(f"/api/system/mcp-servers/{mcp_slug}", headers=admin_headers)
        assert result.status_code in {200, 404}, result.text


async def test_json_creation_imports_mcp_without_creating_empty_skill(test_client, admin_headers, sessions):
    """没有 ZIP 的 JSON 路径提交 MCP，省略配置保留默认值。"""
    suffix = uuid.uuid4().hex[:10]
    slug, mcp_slug = f"pytest-json-{suffix}", f"pytest-json-mcp-{suffix}"
    try:
        result = await test_client.post(
            "/api/agent",
            headers=admin_headers,
            json={"slug": slug, "name": slug, "mcp_servers": [remote_config(mcp_slug)]},
        )
        assert result.status_code == 200, result.text
        read = await test_client.get(f"/api/agent/{slug}", headers=admin_headers)
        context = read.json()["agent"]["config_json"]["context"]
        assert context["mcps"] == [mcp_slug] and "tools" not in context
        assert read.json()["agent"]["configurable_items"]["tools"]["default"] == "all"
        async with sessions() as db:
            agent = await db.scalar(select(Agent).where(Agent.slug == slug))
            assert await db.scalar(select(Skill.id).where(Skill.bound_agent_id == agent.id)) is None
            assert await db.scalar(select(MCPServer.id).where(MCPServer.slug == mcp_slug)) is not None
    finally:
        await test_client.delete(f"/api/agent/{slug}", headers=admin_headers)
        await test_client.delete(f"/api/system/mcp-servers/{mcp_slug}", headers=admin_headers)


@pytest.mark.parametrize("disabled", [False, True])
async def test_existing_mcp_is_not_overwritten_or_selected_when_disabled(
    test_client, admin_headers, sessions, disabled
):
    """禁止覆盖已有配置和选择禁用项，并回滚新的 MCP。"""
    suffix = uuid.uuid4().hex[:10]
    existing, slug, fresh = f"pytest-existing-{suffix}", f"pytest-existing-agent-{suffix}", f"pytest-fresh-{suffix}"
    result = await test_client.post("/api/system/mcp-servers", headers=admin_headers, json=remote_config(existing))
    assert result.status_code == 200, result.text
    try:
        payload = {"name": slug, "slug": slug, "mcp_servers": [remote_config(fresh)]}
        if disabled:
            result = await test_client.put(
                f"/api/system/mcp-servers/{existing}/status", headers=admin_headers, json={"enabled": False}
            )
            assert result.status_code == 200, result.text
            payload["config_json"] = {"context": {"mcps": [existing]}}
        else:
            payload["mcp_servers"].append({**remote_config(existing), "url": "https://example.com/changed"})
        rejected = await test_client.post("/api/agent", headers=admin_headers, json=payload)
        assert rejected.status_code == 422, rejected.text
        await assert_absent(sessions, slug, fresh)
        async with sessions() as db:
            row = await db.scalar(select(MCPServer).where(MCPServer.slug == existing))
            assert row.url == "https://example.com/mcp" and row.headers == {"X-Test": "fixture-only"}
            assert bool(row.enabled) is not disabled
    finally:
        await test_client.delete(f"/api/system/mcp-servers/{existing}", headers=admin_headers)


@pytest.mark.parametrize(
    "failure", ["zip", "dependency", "config", "config-shape", "duplicate", "builtin", "stdio", "oversize", "payload"]
)
async def test_invalid_creation_leaves_no_partial_resources(test_client, admin_headers, sessions, failure):
    """所有实际输入失败都拒绝部分创建，已有 MCP 配置保持原值。"""
    root = get_skill_data_dir() / "packages"
    before = set(root.glob("*")) | set(root.glob("*/*"))
    suffix = uuid.uuid4().hex[:10]
    slug, mcp_slug = f"pytest-reject-{suffix}", f"pytest-reject-mcp-{suffix}"
    configs = [remote_config(mcp_slug)]
    payload = {"slug": slug, "name": slug, "mcp_servers": configs}
    if failure == "config":
        payload["config_json"] = {"context": {"mcps": ["missing-server"]}}
    if failure == "config-shape":
        payload["config_json"] = {"context": "invalid"}
    if failure == "duplicate":
        configs.append(remote_config(mcp_slug))
    if failure == "builtin":
        configs.append(remote_config("deepwiki-official"))
    if failure == "stdio":
        configs[0]["transport"] = "stdio"
    package = (
        b"x" * (10 * 1024 * 1024 + 1)
        if failure == "oversize"
        else bundle(mcp="missing-server" if failure == "dependency" else None, invalid=failure == "zip")
    )
    response = await test_client.post(
        "/api/agent/with-skill",
        headers=admin_headers,
        data={"agent": "not-json" if failure == "payload" else json.dumps(payload)},
        files={"file": ("guide.zip", package, "application/zip")},
    )
    assert response.status_code == (413 if failure == "oversize" else 422), response.text
    await assert_absent(sessions, slug, mcp_slug)
    assert set(root.glob("*")) | set(root.glob("*/*")) == before


@pytest.mark.parametrize("role", ["user", "admin"])
async def test_creation_cannot_bypass_mcp_management_role(test_client, admin_headers, standard_user, sessions, role):
    """创建的替代入口沿用超级管理员权限，重新读取实际角色。"""
    if role == "admin":
        result = await test_client.put(
            f"/api/auth/users/{standard_user['user']['id']}", headers=admin_headers, json={"role": "admin"}
        )
        assert result.status_code == 200, result.text
    suffix = uuid.uuid4().hex[:10]
    slug, mcp_slug = f"pytest-role-{suffix}", f"pytest-role-mcp-{suffix}"
    response = await test_client.post(
        "/api/agent",
        headers=standard_user["headers"],
        json={"slug": slug, "name": slug, "mcp_servers": [remote_config(mcp_slug)]},
    )
    assert response.status_code == 403, response.text
    await assert_absent(sessions, slug, mcp_slug)


@pytest.mark.parametrize("with_skill", [False, True])
async def test_final_commit_failure_rolls_back_all_creation(
    test_client, admin_headers, sessions, monkeypatch, with_skill
):
    """最终提交时数据库内必须已有全部准备结果，失败回读无行且没有新文件包。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    suffix = uuid.uuid4().hex[:10]
    slug, mcp_slug = f"pytest-atomic-{suffix}", f"pytest-atomic-mcp-{suffix}"
    root = get_skill_data_dir() / "packages"
    before = set(root.glob("*")) | set(root.glob("*/*"))
    async with sessions() as db:
        operator = await db.scalar(select(User).where(User.uid == me.json()["uid"]))

        async def fail_final_commit():
            """拒绝提前提交，并在全部行已 flush 的真实事务注入故障。"""
            agent = await db.scalar(select(Agent).where(Agent.slug == slug))
            assert agent is not None
            assert await db.scalar(select(MCPServer.id).where(MCPServer.slug == mcp_slug)) is not None
            skill = await db.scalar(select(Skill).where(Skill.bound_agent_id == agent.id))
            assert (skill is not None) == with_skill
            raise RuntimeError("injected final commit failure")

        monkeypatch.setattr(db, "commit", fail_final_commit)
        with pytest.raises(RuntimeError, match="final commit failure"):
            await create_agent_definition(
                db,
                operator=operator,
                backend_id="ChatbotAgent",
                slug=slug,
                name=slug,
                mcp_servers=[remote_config(mcp_slug)],
                skill_upload=("guide.zip", bundle(mcp=mcp_slug)) if with_skill else None,
            )
    await assert_absent(sessions, slug, mcp_slug)
    assert set(root.glob("*")) | set(root.glob("*/*")) == before
