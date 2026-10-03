"""权限收敛的真实 HTTP 与持久状态验收。"""

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
SHARED = {"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": None}


@pytest_asyncio.fixture
async def actors(test_client, admin_headers):
    """创建两个部门的管理员和普通用户，并清理本测试资源。"""
    suffix = uuid.uuid4().hex[:8]
    password = f"Test-{uuid.uuid4().hex}"
    users, departments, agents, keys = [], [], [], []
    db = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    identities = {}
    try:
        for index in range(2):
            uid = f"padmin{index}{suffix}"
            response = await test_client.post(
                "/api/departments",
                headers=admin_headers,
                json={
                    "name": f"Permission {index} {suffix}",
                    "admin_uid": uid,
                    "admin_password": password,
                },
            )
            assert response.status_code == 201, response.text
            dept_id = response.json()["id"]
            departments.append(dept_id)
            token = await test_client.post("/api/auth/token", data={"username": uid, "password": password})
            assert token.status_code == 200, token.text
            headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
            me = (await test_client.get("/api/auth/me", headers=headers)).json()
            users.append(me["id"])
            identities[f"admin{index}"] = {**me, "headers": headers}
            response = await test_client.post(
                "/api/auth/users",
                headers=headers,
                json={
                    "username": f"puser{index}{suffix}",
                    "password": password,
                },
            )
            assert response.status_code == 200, response.text
            user = response.json()
            users.append(user["id"])
            token = await test_client.post("/api/auth/token", data={"username": user["uid"], "password": password})
            assert token.status_code == 200, token.text
            identities[f"user{index}"] = {
                **user,
                "headers": {"Authorization": f"Bearer {token.json()['access_token']}"},
            }
        yield {
            "client": test_client,
            "root": admin_headers,
            "identities": identities,
            "db": db,
            "agents": agents,
            "keys": keys,
            "password": password,
        }
    finally:
        for slug in agents:
            from test.e2e.e2e_helpers import delete_agent

            await delete_agent(test_client, admin_headers, slug)
        for key in keys:
            await test_client.delete(f"/api/user/apikey/{key}", headers=admin_headers)
        for user_id in users:
            response = await test_client.delete(f"/api/auth/users/{user_id}", headers=admin_headers)
            assert response.status_code in {200, 404}, response.text
        for department_id in departments:
            await test_client.delete(f"/api/departments/{department_id}", headers=admin_headers)
        await db.close()


async def create_agent(actors, owner, **payload):
    """创建定义并登记清理目标。"""
    response = await actors["client"].post(
        "/api/agent", headers=owner["headers"], json={"name": "Permission bot", **payload}
    )
    assert response.status_code == 200, response.text
    agent = response.json()["agent"]
    actors["agents"].append(agent["slug"])
    return agent


async def test_department_member_update_rechecks_after_concurrent_promotion(actors):
    """另一事务晋升期间的成员更新，等待锁后必须按最新角色拒绝。"""
    client, db = actors["client"], actors["db"]
    admin, member = (actors["identities"][key] for key in ("admin0", "user0"))
    transaction = db.transaction()
    await transaction.start()
    pending = None
    try:
        await db.execute("UPDATE users SET role='admin' WHERE id=$1", member["id"])
        pending = asyncio.create_task(
            client.put(
                f"/api/auth/users/{member['id']}", headers=admin["headers"], json={"username": "forbidden_race_edit"}
            )
        )
        for _ in range(100):
            await db.execute("SELECT pg_stat_clear_snapshot()")
            if await db.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND query LIKE '%users%' AND pid<>pg_backend_pid())"
            ):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("成员更新没有到达受控的数据库锁")
        await transaction.commit()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 403, response.text
        state = await db.fetchrow("SELECT role,username FROM users WHERE id=$1", member["id"])
        assert state["role"] == "admin" and state["username"] == member["username"]
    finally:
        if transaction._state.name == "STARTED":
            await transaction.rollback()
        if pending is not None and not pending.done():
            await pending


async def test_shared_transfer_requires_superadmin_and_active_admin_recipient(actors):
    """共享配置不能改所有者，专用转移路径保留定义身份并拒绝普通接收者。"""
    client, db = actors["client"], actors["db"]
    first, second, user = (actors["identities"][key] for key in ("admin0", "admin1", "user0"))
    agent = await create_agent(
        actors, first, visibility="shared", share_config={"version": 2, "read_scope": None, "manage_scope": None}
    )
    path = f"/api/system/resources/agent/{agent['slug']}/owner"
    for headers, recipient, status in [(first["headers"], second["uid"], 403), (actors["root"], user["uid"], 422)]:
        response = await client.put(path, headers=headers, json={"owner_uid": recipient})
        assert response.status_code == status, response.text
        assert await db.fetchval("SELECT created_by FROM agents WHERE slug=$1", agent["slug"]) == first["uid"]
    response = await client.put(path, headers=actors["root"], json={"owner_uid": second["uid"]})
    assert response.status_code == 200, response.text
    state = await db.fetchrow("SELECT id,created_by,visibility FROM agents WHERE slug=$1", agent["slug"])
    assert state["id"] == agent["id"] and state["created_by"] == second["uid"] and state["visibility"] == "shared"
    assert (await client.get(f"/api/agent/{agent['slug']}", headers=first["headers"])).status_code == 404
    response = await client.get(f"/api/agent/{agent['slug']}", headers=second["headers"])
    assert response.status_code == 200 and response.json()["agent"]["can_manage"]


async def test_ordinary_shared_knowledge_and_skill_creation_is_rejected_without_records(actors):
    """普通用户的共享建设写入失败后没有数据库定义。"""
    client, db = actors["client"], actors["db"]
    user = actors["identities"]["user0"]
    name = f"permission-rejected-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/knowledge/databases",
        headers=user["headers"],
        json={"database_name": name, "description": "forbidden", "share_config": SHARED},
    )
    assert response.status_code == 403, response.text
    assert await db.fetchval("SELECT count(*) FROM knowledge_bases WHERE name=$1", name) == 0
    prepared = await client.post(
        "/api/skills/import/prepare",
        headers=user["headers"],
        files={
            "file": ("SKILL.md", f"---\nname: {name}\ndescription: rejected\n---\nTest\n".encode(), "text/markdown")
        },
    )
    assert prepared.status_code == 200, prepared.text
    draft = prepared.json()["data"]["draft_id"]
    try:
        response = await client.post(
            f"/api/skills/install-drafts/{draft}/confirm",
            headers=user["headers"],
            json={"slugs": [name], "share_config": SHARED},
        )
        assert response.status_code in {403, 422}, response.text
        assert await db.fetchval("SELECT count(*) FROM skills WHERE slug=$1", name) == 0
    finally:
        await client.delete(f"/api/skills/install-drafts/{draft}", headers=user["headers"])


async def test_private_agent_crud_isolation_governance_and_app_default(actors):
    client, db = actors["client"], actors["db"]
    owner = actors["identities"]["user0"]
    agent = await create_agent(actors, owner, config_json={"context": {"system_prompt": "private prompt"}})
    slug = agent["slug"]
    assert agent["visibility"] == "private" and agent["can_manage"] and agent["can_run"]
    assert await db.fetchval("SELECT visibility FROM agents WHERE slug=$1", slug) == "private"
    for actor in (actors["identities"]["user1"], actors["identities"]["admin0"], actors["identities"]["admin1"]):
        listing = await client.get("/api/agent", headers=actor["headers"])
        assert slug not in {item["slug"] for item in listing.json()["agents"]}
        for method in (client.get, client.put, client.delete):
            kwargs = {"json": {"name": "forged"}} if method == client.put else {}
            response = await method(f"/api/agent/{slug}", headers=actor["headers"], **kwargs)
            assert response.status_code == 404, response.text
        response = await client.post(
            "/api/v1/agents/threads",
            headers={**actor["headers"], "Idempotency-Key": str(uuid.uuid4())},
            json={"agent_id": slug},
        )
        assert response.status_code in {403, 404}, response.text
    response = await client.put(f"/api/agent/{slug}", headers=owner["headers"], json={"name": "Owner edit"})
    assert response.status_code == 200, response.text
    assert await db.fetchval("SELECT name FROM agents WHERE slug=$1", slug) == "Owner edit"
    governed = await client.get(f"/api/agent/{slug}", headers=actors["root"])
    assert governed.status_code == 200 and governed.json()["agent"]["can_manage"]
    assert not governed.json()["agent"]["can_run"]
    run = await client.post(
        "/api/v1/agents/threads",
        headers={**actors["root"], "Idempotency-Key": str(uuid.uuid4())},
        json={"agent_id": slug},
    )
    assert run.status_code in {403, 404}, run.text
    key = await client.post(
        "/api/user/apikey/",
        headers=owner["headers"],
        json={
            "name": "Permission APP",
            "request_id": str(uuid.uuid4()),
            "access_level": "agents",
            "app_id": uuid.uuid4().hex,
        },
    )
    assert key.status_code == 200, key.text
    actors["keys"].append(key.json()["api_key"]["id"])
    for identity in (None, "explicit-end-user"):
        headers = {"Authorization": f"Bearer {key.json()['secret']}"}
        if identity:
            headers["X-End-User-Id"] = identity
        listing = await client.get("/api/v1/agents", headers=headers)
        assert listing.status_code == 200 and slug not in {item["id"] for item in listing.json()["data"]}
        assert (await client.get(f"/api/v1/agents/{slug}", headers=headers)).status_code == 404
        run = await client.post(
            "/api/v1/agents/threads", headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, json={"agent_id": slug}
        )
        assert run.status_code in {403, 404}, run.text
    deleted = await client.delete(f"/api/agent/{slug}", headers=owner["headers"])
    assert deleted.status_code == 200
    assert await db.fetchval("SELECT id FROM agents WHERE slug=$1", slug) is None


async def test_user_write_guards_and_publication_are_explicit_and_atomic(actors):
    client, db = actors["client"], actors["db"]
    user = actors["identities"]["user0"]
    agent = await create_agent(actors, user)
    path = f"/api/agent/{agent['slug']}"
    for payload in (
        {"visibility": "shared"},
        {"share_config": SHARED},
        {"created_by": "forged"},
        {"backend_id": "SubAgentBackend"},
        {"config_json": {"context": {"max_execution_steps": 999}}},
    ):
        response = await client.post("/api/agent", headers=user["headers"], json={"name": "forged", **payload})
        assert response.status_code == 422, response.text
    for payload in (
        {"visibility": "shared"},
        {"share_config": SHARED},
        {"created_by": "forged"},
        {"is_subagent": True},
    ):
        response = await client.put(path, headers=user["headers"], json=payload)
        assert response.status_code == 422, response.text
    for headers in (user["headers"], actors["root"], actors["identities"]["admin0"]["headers"]):
        response = await client.post(path + "/publish", headers=headers, json={"share_config": SHARED})
        assert response.status_code == 403, response.text
    assert await db.fetchval("SELECT visibility FROM agents WHERE slug=$1", agent["slug"]) == "private"
    promotion = await client.put(f"/api/auth/users/{user['id']}", headers=actors["root"], json={"role": "admin"})
    assert promotion.status_code == 200, promotion.text
    published = await client.post(path + "/publish", headers=user["headers"], json={"share_config": SHARED})
    assert published.status_code == 200, published.text
    row = await db.fetchrow("SELECT id, created_by, visibility FROM agents WHERE slug=$1", agent["slug"])
    assert row["id"] == agent["id"] and row["created_by"] == user["uid"] and row["visibility"] == "shared"
    assert (await client.put(path, headers=user["headers"], json={"visibility": "private"})).status_code == 422
    assert (
        await client.post(path + "/publish", headers=user["headers"], json={"share_config": SHARED})
    ).status_code == 422


async def test_shared_grants_role_ceiling_and_system_configuration(actors):
    client, db = actors["client"], actors["db"]
    owner, manager, user = (actors["identities"][name] for name in ("admin0", "admin1", "user1"))
    grants = {**SHARED, "manage_scope": {"access_level": "global"}}
    agent = await create_agent(actors, owner, visibility="shared", share_config=grants)
    path = f"/api/agent/{agent['slug']}"
    readable = await client.get(path, headers=user["headers"])
    assert readable.status_code == 200 and not readable.json()["agent"]["can_manage"]
    assert (await client.put(path, headers=user["headers"], json={"name": "forged"})).status_code == 403
    valid = {**SHARED, "manage_scope": {"access_level": "user", "user_uids": [manager["uid"]]}}
    response = await client.put(path, headers=manager["headers"], json={"share_config": valid})
    assert response.status_code == 200, response.text
    saved = await db.fetchval("SELECT share_config FROM agents WHERE slug=$1", agent["slug"])
    for invalid in (
        {"version": 2, "read_scope": None, "manage_scope": {"access_level": "global"}},
        {
            "version": 2,
            "read_scope": {"access_level": "department", "department_ids": [owner["department_id"]]},
            "manage_scope": {"access_level": "user", "user_uids": [owner["uid"]]},
        },
        {**SHARED, "manage_scope": {"access_level": "user", "user_uids": [user["uid"]]}},
    ):
        response = await client.put(path, headers=owner["headers"], json={"share_config": invalid})
        assert response.status_code == 422, response.text
        assert await db.fetchval("SELECT share_config FROM agents WHERE slug=$1", agent["slug"]) == saved
    for route, payload in (
        ("/api/system/mcp-servers", {"slug": "forged"}),
        ("/api/system/model-providers", {"provider_id": "forged"}),
        ("/api/system/config/update", {}),
    ):
        for actor in (user, manager):
            response = await client.post(route, headers=actor["headers"], json=payload)
            assert response.status_code == 403, response.text
    default = await client.put("/api/agent/default-chatbot", headers=manager["headers"], json={"name": "forged"})
    assert default.status_code == 403, default.text


async def test_department_admin_limits_role_promotion_and_deleted_owner_retention(actors):
    client, db = actors["client"], actors["db"]
    admin, user, other = (actors["identities"][name] for name in ("admin0", "user0", "user1"))
    for payload in ({"password": actors["password"]}, {"role": "admin"}, {"department_id": other["department_id"]}):
        response = await client.put(f"/api/auth/users/{user['id']}", headers=admin["headers"], json=payload)
        assert response.status_code == 403, response.text
    assert (
        await client.put(f"/api/auth/users/{other['id']}", headers=admin["headers"], json={"username": "forged"})
    ).status_code == 403
    for old, target in (("admin", "user"), ("superadmin", "admin"), ("superadmin", "user")):
        if old == "superadmin":
            response = await client.put(
                f"/api/auth/users/{admin['id']}", headers=actors["root"], json={"role": "superadmin"}
            )
            assert response.status_code == 200, response.text
        response = await client.put(f"/api/auth/users/{admin['id']}", headers=actors["root"], json={"role": target})
        assert response.status_code == 422, response.text
        assert await db.fetchval("SELECT role FROM users WHERE id=$1", admin["id"]) == old
    private = await create_agent(actors, user)
    shared = await create_agent(actors, admin, visibility="shared", share_config=SHARED)
    for identity in (user, admin):
        response = await client.delete(f"/api/auth/users/{identity['id']}", headers=actors["root"])
        assert response.status_code == 200, response.text
        assert await db.fetchval("SELECT is_deleted FROM users WHERE id=$1", identity["id"]) == 1
    for agent, visibility in ((private, "private"), (shared, "shared")):
        row = await db.fetchrow("SELECT visibility, created_by FROM agents WHERE slug=$1", agent["slug"])
        assert row["visibility"] == visibility and row["created_by"] == agent["created_by"]
    governed = await client.put(f"/api/agent/{private['slug']}", headers=actors["root"], json={"name": "governed"})
    assert governed.status_code == 200, governed.text
    transfer = await client.put(
        f"/api/system/resources/agent/{private['slug']}/owner",
        headers=actors["root"],
        json={"owner_uid": actors["identities"]["admin1"]["uid"]},
    )
    assert transfer.status_code == 422, transfer.text
