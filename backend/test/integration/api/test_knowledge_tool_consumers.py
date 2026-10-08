"""Public 工具接口承接 CLI 文件搜索与资源隔离。"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_restricted_database(test_client, admin_headers):
    """创建仅所有者可读的知识库。"""
    profile_response = await test_client.get("/api/auth/me", headers=admin_headers)
    assert profile_response.status_code == 200, profile_response.text
    owner_uid = profile_response.json()["uid"]

    response = await test_client.post(
        "/api/knowledge/databases",
        json={
            "database_name": f"pytest_tool_consumer_{uuid.uuid4().hex[:8]}",
            "description": "Public tool consumer test",
            "embedding_model_spec": "siliconflow-cn:Pro/BAAI/bge-m3",
            "kb_type": "milvus",
            "additional_params": {},
            "share_config": {
                "version": 2,
                "read_scope": {
                    "access_level": "user",
                    "department_ids": [],
                    "user_uids": [owner_uid],
                },
                "manage_scope": None,
            },
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _delete_database(test_client, admin_headers, kb_id):
    """清理用例创建的知识库。"""
    response = await test_client.delete(f"/api/knowledge/databases/{kb_id}", headers=admin_headers)
    assert response.status_code in (200, 404), response.text


async def test_tool_file_search_is_restricted_to_owner(test_client, admin_headers, standard_user):
    """JWT 与 Knowledge Key 均不能按 ID 搜索他人私有库。"""
    database = await _create_restricted_database(test_client, admin_headers)
    kb_id = database["kb_id"]
    key_id = None
    try:
        owner_response = await test_client.get(
            "/api/v1/knowledge/tools/list_kbs",
            headers=admin_headers,
        )
        assert owner_response.status_code == 200
        assert any(db.get("kb_id") == kb_id for db in owner_response.json())

        other_response = await test_client.get(
            "/api/v1/knowledge/tools/list_kbs",
            headers=standard_user["headers"],
        )
        assert other_response.status_code == 200
        assert all(db.get("kb_id") != kb_id for db in other_response.json())

        forbidden = await test_client.post(
            "/api/v1/knowledge/tools/search_file",
            json={"kb_id": kb_id},
            headers=standard_user["headers"],
        )
        assert forbidden.status_code == 404

        key_created = await test_client.post(
            "/api/user/apikey/",
            json={
                "request_id": str(uuid.uuid4()),
                "name": "Restricted tool test",
                "access_level": "knowledge",
            },
            headers=standard_user["headers"],
        )
        assert key_created.status_code == 200, key_created.text
        key_id = key_created.json()["api_key"]["id"]
        key_headers = {"Authorization": f"Bearer {key_created.json()['secret']}"}
        key_list = await test_client.get("/api/v1/knowledge/tools/list_kbs", headers=key_headers)
        assert key_list.status_code == 200, key_list.text
        assert all(db.get("kb_id") != kb_id for db in key_list.json())
        key_forbidden = await test_client.post(
            "/api/v1/knowledge/tools/search_file",
            json={"kb_id": kb_id},
            headers=key_headers,
        )
        assert key_forbidden.status_code == 404, key_forbidden.text
    finally:
        if key_id is not None:
            await test_client.delete(f"/api/user/apikey/{key_id}", headers=standard_user["headers"])
        await _delete_database(test_client, admin_headers, kb_id)


async def test_tool_file_search_by_id_preserves_pagination(test_client, admin_headers, knowledge_database):
    """按 ID 列文件可省略查询词，并保留分页与未知资源拒绝。"""
    kb_id = knowledge_database["kb_id"]
    response = await test_client.post(
        "/api/v1/knowledge/tools/search_file",
        json={"kb_id": kb_id, "offset": 0, "limit": 100},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert isinstance(response.json()["files"], list)
    assert response.json()["offset"] == 0
    assert response.json()["limit"] == 100
    missing = await test_client.post(
        "/api/v1/knowledge/tools/search_file",
        json={"kb_id": "missing-kb", "query": "anything"},
        headers=admin_headers,
    )
    assert missing.status_code == 404, missing.text
