"""真实 HTTP 验证受限 Key 的 API 面与来源边界。"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress

import asyncpg
import pytest
from yuxi.modules.agents.runtime.sandbox.paths import runtime_user_data_path
from yuxi.modules.workspace.filesystem import Workspace
from test.live_api_cleanup import delete_test_conversation_resources, validate_test_runs_terminal

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _delete_created_threads(*thread_ids: str | None) -> None:
    """仅清理本测试创建的隐式 Project、Thread 和 Workdir。"""

    targets = {thread_id for thread_id in thread_ids if thread_id}
    if not targets:
        return
    await validate_test_runs_terminal(targets)
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT c.thread_id, c.uid, c.project_id, p.workdir_path, "
            "p.directory_mode, p.selection_status, u.user_kind, u.end_user_id "
            "FROM conversations c JOIN projects p ON p.id = c.project_id AND p.uid = c.uid "
            "JOIN users u ON u.uid = c.uid "
            "WHERE c.thread_id = ANY($1::text[])",
            sorted(targets),
        )
    finally:
        await conn.close()
    if {row["thread_id"] for row in rows} != targets:
        raise RuntimeError("Test Thread cleanup cannot verify all created resources")
    if any(row["directory_mode"] != "managed" or row["selection_status"] != "implicit" for row in rows):
        raise RuntimeError("Test Thread cleanup refuses a non-implicit Project")
    workdirs = {(row["uid"], row["workdir_path"]): {row["project_id"]} for row in rows}
    await delete_test_conversation_resources(workdirs, targets, {row["project_id"] for row in rows})
    public_uids = {
        row["uid"] for row in rows if row["user_kind"] == "end_user" and row["end_user_id"] == "__default__"
    }
    if public_uids:
        conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
        try:
            await conn.execute(
                "DELETE FROM users WHERE uid = ANY($1::text[]) AND user_kind = 'end_user' "
                "AND end_user_id = '__default__' "
                "AND NOT EXISTS (SELECT 1 FROM conversations WHERE conversations.uid = users.uid) "
                "AND NOT EXISTS (SELECT 1 FROM projects WHERE projects.uid = users.uid)",
                sorted(public_uids),
            )
        finally:
            await conn.close()


async def test_unbound_full_key_uses_owners_product_thread_scope(test_client, admin_headers):
    """CLI 登录签发的完整 Key 可创建产品 Thread，且不能伪造终端用户。"""
    created_key = await test_client.post(
        "/api/user/apikey/",
        json={"request_id": str(uuid.uuid4()), "name": "CLI product scope", "access_level": "full"},
        headers=admin_headers,
    )
    assert created_key.status_code == 200, created_key.text
    key_id = created_key.json()["api_key"]["id"]
    assert created_key.json()["api_key"]["app_id"] is None
    key_headers = {"Authorization": f"Bearer {created_key.json()['secret']}"}
    thread_id = None
    try:
        created = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": "default-chatbot"},
            headers={**key_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert created.status_code == 200, created.text
        thread_id = created.json()["thread_id"]
        from_jwt = await test_client.get(f"/api/v1/agents/threads/{thread_id}", headers=admin_headers)
        assert from_jwt.status_code == 200, from_jwt.text
        assert from_jwt.json()["thread_id"] == thread_id

        forged = await test_client.get(
            f"/api/v1/agents/threads/{thread_id}",
            headers={**key_headers, "X-End-User-Id": "other-user"},
        )
        assert forged.status_code == 403, forged.text
    finally:
        await _delete_created_threads(thread_id)
        await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)


async def test_agents_key_cannot_use_product_routes_or_spoof_source(test_client, admin_headers):
    """受限 Key 可读公开目录，但不能跨 APP 或使用管理接口。"""
    payload = {
        "request_id": str(uuid.uuid4()),
        "name": "Public API boundary test",
        "access_level": "agents",
        "app_id": "integration-app",
    }
    created = await test_client.post("/api/user/apikey/", json=payload, headers=admin_headers)
    assert created.status_code == 200, created.text
    key_id = created.json()["api_key"]["id"]
    headers = {
        "Authorization": f"Bearer {created.json()['secret']}",
        "X-App-Id": "forged-source",
    }
    product_thread_id = None
    public_thread_id = None
    try:
        directory = await test_client.get("/api/v1/agents", headers=headers)
        assert directory.status_code == 200, directory.text
        assert directory.headers["X-App-Id"] == "integration-app"
        assert isinstance(directory.json()["data"], list)

        for path in ("/api/agent", "/api/user/apikey/"):
            blocked = await test_client.get(path, headers=headers)
            assert blocked.status_code == 403, (path, blocked.text)
            assert blocked.headers["X-App-Id"] == "integration-app"
        old_chat = await test_client.get("/api/chat/threads", headers=headers)
        assert old_chat.status_code == 404, old_chat.text

        agents = await test_client.get("/api/agent", headers=admin_headers)
        assert agents.status_code == 200, agents.text
        agent = agents.json()["agents"][0]
        agent_slug = agent.get("agent_id") or agent["slug"]
        forged_thread = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": agent_slug, "app_id": "integration-app"},
            headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert forged_thread.status_code == 422, forged_thread.text

        product_thread = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": agent_slug},
            headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert product_thread.status_code == 200, product_thread.text
        product_thread_id = product_thread.json().get("thread_id") or product_thread.json()["id"]
        conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
        try:
            stored = await conn.fetchrow(
                "UPDATE conversations SET extra_metadata = "
                "jsonb_set(extra_metadata::jsonb, '{app_id}', to_jsonb($1::text))::json "
                "WHERE thread_id = $2 RETURNING app_id, extra_metadata",
                "integration-app",
                product_thread_id,
            )
            assert stored is not None and stored["app_id"] is None
        finally:
            await conn.close()
        isolated = await test_client.get(f"/api/v1/agents/threads/{product_thread_id}", headers=headers)
        assert isolated.status_code == 404, isolated.text

        public_thread = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": agent_slug},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert public_thread.status_code == 200, public_thread.text
        public_thread_id = public_thread.json()["thread_id"]
        own_thread = await test_client.get(f"/api/v1/agents/threads/{public_thread_id}", headers=headers)
        assert own_thread.status_code == 200, own_thread.text
        jwt_isolated = await test_client.get(
            f"/api/v1/agents/threads/{public_thread_id}", headers=admin_headers
        )
        assert jwt_isolated.status_code == 404, jwt_isolated.text

        replay = await test_client.post("/api/user/apikey/", json=payload, headers=admin_headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["api_key"]["id"] == key_id

        for changed in ({"app_id": "other-app"}, {"access_level": "full"}):
            conflict = await test_client.post("/api/user/apikey/", json={**payload, **changed}, headers=admin_headers)
            assert conflict.status_code == 409, conflict.text

        widened = await test_client.put(
            f"/api/user/apikey/{key_id}", json={"access_level": "full"}, headers=admin_headers
        )
        assert widened.status_code == 200, widened.text
        restored = await test_client.get("/api/agent", headers=headers)
        assert restored.status_code == 200, restored.text
    finally:
        if public_thread_id is not None:
            await test_client.post(f"/api/v1/agents/threads/{public_thread_id}/archive", headers=headers)
        if product_thread_id is not None:
            await test_client.post(f"/api/v1/agents/threads/{product_thread_id}/archive", headers=admin_headers)
        try:
            await _delete_created_threads(public_thread_id, product_thread_id)
        finally:
            await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)


async def test_public_api_accepts_product_jwt_without_end_user_impersonation(test_client, admin_headers):
    """产品 JWT 可使用 Public，但不能声明 APP 的终端用户。"""
    missing = await test_client.post(
        "/api/user/apikey/",
        json={"request_id": str(uuid.uuid4()), "name": "No app", "access_level": "agents"},
        headers=admin_headers,
    )
    assert missing.status_code == 422, missing.text

    jwt = await test_client.get("/api/v1/agents", headers=admin_headers)
    assert jwt.status_code == 200, jwt.text
    assert "X-App-Id" not in jwt.headers

    spoofed = await test_client.get(
        "/api/v1/agents", headers={**admin_headers, "X-End-User-Id": "another-user"}
    )
    assert spoofed.status_code == 403, spoofed.text


async def test_key_without_end_user_id_cannot_reach_product_project_files(test_client, admin_headers):
    """无终端用户 Header 的 APP Key 仍使用独立 UID 与 Workspace。"""
    created_key = await test_client.post(
        "/api/user/apikey/",
        json={
            "request_id": str(uuid.uuid4()),
            "name": "Default APP user isolation",
            "access_level": "agents",
            "app_id": f"boundary-{uuid.uuid4()}",
        },
        headers=admin_headers,
    )
    assert created_key.status_code == 200, created_key.text
    key_id = created_key.json()["api_key"]["id"]
    key_headers = {"Authorization": f"Bearer {created_key.json()['secret']}"}
    product_thread_id = None
    app_thread_id = None
    product_workspace = None
    app_workspace = None
    product_file = None
    app_file = None
    product_saved_file = None
    try:
        agents = await test_client.get("/api/v1/agents", headers=admin_headers)
        assert agents.status_code == 200, agents.text
        agent_slug = agents.json()["data"][0]["id"]
        product_thread = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": agent_slug},
            headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert product_thread.status_code == 200, product_thread.text
        product_thread_id = product_thread.json()["thread_id"]
        app_thread = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": agent_slug},
            headers={**key_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert app_thread.status_code == 200, app_thread.text
        app_thread_id = app_thread.json()["thread_id"]

        conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
        try:
            rows = await conn.fetch(
                "SELECT c.thread_id, c.uid, c.project_id, c.app_id, p.workdir_path, "
                "u.user_kind, u.end_user_id FROM conversations c "
                "JOIN projects p ON p.id = c.project_id "
                "JOIN users u ON u.uid = c.uid WHERE c.thread_id = ANY($1::text[])",
                [product_thread_id, app_thread_id],
            )
        finally:
            await conn.close()
        bindings = {row["thread_id"]: row for row in rows}
        product = bindings[product_thread_id]
        app = bindings[app_thread_id]
        assert product["uid"] != app["uid"]
        assert product["app_id"] is None
        assert app["app_id"] == created_key.json()["api_key"]["app_id"]
        assert app["user_kind"] == "end_user"
        assert app["end_user_id"] == "__default__"

        product_workspace = Workspace(product["uid"])
        app_workspace = Workspace(app["uid"])
        product_file = f"/{product['workdir_path'].strip('/')}/product-only.txt"
        app_file = f"/{app['workdir_path'].strip('/')}/app-source.txt"
        product_saved_file = f"/{product['workdir_path'].strip('/')}/app-source.txt"
        product_workspace.replace_authorized_file(product_file, b"product private file")
        app_workspace.replace_authorized_file(app_file, b"app file")

        foreign_project = await test_client.post(
            "/api/v1/agents/threads",
            json={"agent_id": agent_slug, "project_id": product["project_id"]},
            headers={**key_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert foreign_project.status_code == 404, foreign_project.text
        product_runtime_path = runtime_user_data_path(product_file)
        foreign_read = await test_client.get(
            f"/api/v1/agents/threads/{app_thread_id}/artifacts/{product_runtime_path.lstrip('/')}",
            headers=key_headers,
        )
        assert foreign_read.status_code in {403, 404}, foreign_read.text
        foreign_write = await test_client.post(
            f"/api/v1/agents/threads/{app_thread_id}/artifacts/save",
            json={
                "path": runtime_user_data_path(app_file),
                "destination_path": f"/{product['workdir_path'].strip('/')}",
            },
            headers=key_headers,
        )
        assert foreign_write.status_code in {403, 404}, foreign_write.text
        with pytest.raises(FileNotFoundError):
            product_workspace.stat_authorized_path(product_saved_file, root="/")
        assert product_workspace.read_authorized_file(product_file, 100) == b"product private file"
    finally:
        if product_workspace is not None:
            for path in (product_file, product_saved_file):
                if path:
                    with suppress(FileNotFoundError):
                        product_workspace.delete_authorized_path(path, root="/")
        if app_workspace is not None and app_file:
            with suppress(FileNotFoundError):
                app_workspace.delete_authorized_path(app_file, root="/")
        if app_thread_id is not None:
            await test_client.post(f"/api/v1/agents/threads/{app_thread_id}/archive", headers=key_headers)
        if product_thread_id is not None:
            await test_client.post(f"/api/v1/agents/threads/{product_thread_id}/archive", headers=admin_headers)
        try:
            await _delete_created_threads(app_thread_id, product_thread_id)
        finally:
            await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/agents/threads",
        "/api/v1/agents/threads/thread-id/events",
    ],
)
async def test_public_message_preflight_allows_idempotency_key(test_client, path):
    """明确允许跨源浏览器提交 Public API 必需的幂等请求头。"""
    origin = os.getenv("YUXI_CORS_ORIGINS", "http://localhost:5173").split(",")[0]
    response = await test_client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key,x-end-user-id",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == origin
    assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()
    assert "x-end-user-id" in response.headers["access-control-allow-headers"].lower()
