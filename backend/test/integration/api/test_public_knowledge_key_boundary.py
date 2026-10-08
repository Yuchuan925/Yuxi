"""Knowledge API Key 在真实 HTTP 边界仅能访问Public 工具查询。"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_knowledge_key_is_limited_to_public_knowledge_api(test_client, admin_headers):
    """knowledge Key 可查询 Public 工具接口，不能越界到管理或其他 API。"""
    created = await test_client.post(
        "/api/user/apikey/",
        json={
            "request_id": str(uuid.uuid4()),
            "name": "Knowledge API boundary test",
            "access_level": "knowledge",
        },
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    key_id = created.json()["api_key"]["id"]
    headers = {"Authorization": f"Bearer {created.json()['secret']}"}
    try:
        listed = await test_client.get("/api/v1/knowledge/tools/list_kbs", headers=headers)
        assert listed.status_code == 200, listed.text
        assert isinstance(listed.json(), list)

        for path in (
            "/api/knowledge/databases",
            "/api/knowledge/databases/external",
            "/api/v1/agents",
            "/api/user/apikey/",
            "/api/graph/list",
            "/api/evaluation/databases/unused/datasets",
        ):
            blocked = await test_client.get(path, headers=headers)
            assert blocked.status_code == 403, (path, blocked.text)
    finally:
        await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)


async def test_agents_key_cannot_access_public_knowledge_api(test_client, admin_headers):
    """Agents 与 Knowledge 两种受限 Key 的 API 面互不包含。"""
    created = await test_client.post(
        "/api/user/apikey/",
        json={
            "request_id": str(uuid.uuid4()),
            "name": "Agents API boundary test",
            "access_level": "agents",
            "app_id": "knowledge-boundary-test",
        },
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    key_id = created.json()["api_key"]["id"]
    try:
        tool_response = await test_client.get(
            "/api/v1/knowledge/tools/list_kbs",
            headers={"Authorization": f"Bearer {created.json()['secret']}"},
        )
        assert tool_response.status_code == 403, tool_response.text
    finally:
        await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)


async def test_public_knowledge_does_not_expose_management_routes(test_client, admin_headers):
    """版本化知识域仅注册工具查询，不迁入管理路由。"""
    response = await test_client.get("/api/v1/knowledge/databases", headers=admin_headers)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("prefix", ["/api/v1/knowledge", "/api/knowledge"])
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", ""),
        ("GET", "/missing/files"),
        ("POST", "/missing/retrieve"),
        ("GET", "/missing/files/missing/open"),
        ("POST", "/missing/files/missing/find"),
    ],
)
async def test_external_operations_are_removed(test_client, admin_headers, prefix, method, suffix):
    """两种 external 前缀均不再提供查询操作或兼容转发。"""
    response = await test_client.request(
        method, f"{prefix}/databases/external{suffix}", headers=admin_headers
    )
    assert response.status_code == 404, response.text
