"""真实 HTTP 与 PostgreSQL 验证 Session 持久内容分页和隔离。"""

import json
import os
import uuid

import asyncpg
import pytest

from test.integration.api.test_public_agents_key_boundary import delete_created_threads
from test.live_api_cleanup import make_test_session_title

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_session_items_pagination_uses_only_authorized_public_content(test_client, admin_headers):
    """双向分页无重复遗漏，内部审计和其他会话的游标不会混入。"""
    session_ids = []
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        for _ in range(2):
            response = await test_client.post(
                "/api/v1/agents/sessions",
                headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={"agent_id": "default-chatbot", "title": make_test_session_title("session-items")},
            )
            assert response.status_code == 201, response.text
            payload = response.json()
            assert payload["object"] == "agent.session"
            assert isinstance(payload["created_at"], int)
            assert payload["status"] == "idle" and payload["yuxi"]["receipt"]["input_id"] is None
            session_ids.append(payload["id"])
        record_id = await conn.fetchval("SELECT id FROM sessions WHERE thread_id=$1", session_ids[0])
        expected = []
        for text in ["first", "second", "third"]:
            message_id = await conn.fetchval(
                "INSERT INTO messages "
                "(session_record_id, role, content, message_type, extra_metadata, delivery_status, created_at) "
                "VALUES ($1, 'user', $2, 'text', $3::json, 'complete', now()) RETURNING id",
                record_id,
                text,
                json.dumps({"raw_message": {"content": [{"type": "input_text", "text": text}]}}),
            )
            expected.append(f"input_{message_id}")
        await conn.execute(
            "INSERT INTO messages (session_record_id, role, content, message_type, delivery_status, created_at) "
            "VALUES ($1, 'assistant', 'PRIVATE_AUDIT', 'text', 'complete', now())",
            record_id,
        )
        base = f"/api/v1/agents/sessions/{session_ids[0]}/items"
        first = (await test_client.get(base, headers=admin_headers, params={"order": "asc", "limit": 2})).json()
        assert first["object"] == "list" and first["has_more"] is True
        assert [item["id"] for item in first["data"]] == expected[:2]
        second = (
            await test_client.get(
                base, headers=admin_headers, params={"order": "asc", "limit": 2, "after": first["last_id"]}
            )
        ).json()
        assert [item["id"] for item in second["data"]] == expected[2:]
        assert second["has_more"] is False
        descending = (await test_client.get(base, headers=admin_headers)).json()
        assert [item["id"] for item in descending["data"]] == list(reversed(expected))
        assert "PRIVATE_AUDIT" not in str(descending)
        empty = (await test_client.get(base, headers=admin_headers, params={"after": expected[0]})).json()
        assert empty == {
            "object": "list",
            "data": [],
            "first_id": None,
            "last_id": None,
            "has_more": False,
            "yuxi": {"runs": []},
        }
        invalid = await test_client.get(
            f"/api/v1/agents/sessions/{session_ids[1]}/items", headers=admin_headers, params={"after": expected[0]}
        )
        assert invalid.status_code == 400 and "当前 Session" in invalid.json()["detail"]
        old = await test_client.get(f"/api/v1/agents/threads/{session_ids[0]}/items", headers=admin_headers)
        assert old.status_code == 404
        pending_tool = {
            "type": "function_call_output",
            "id": "item-pending-tool",
            "turn_id": None,
            "call_id": "call-pending",
            "status": "in_progress",
            "output": None,
            "error": None,
            "yuxi": {"output_index": 0},
        }
        await conn.execute(
            "INSERT INTO messages "
            "(session_record_id, role, content, message_type, extra_metadata, delivery_status, created_at) "
            "VALUES ($1, 'tool', '', 'tool_audit', $2::json, 'complete', now())",
            record_id,
            json.dumps({"public_items": {"output": pending_tool}}),
        )
        pending = await test_client.get(base, headers=admin_headers, params={"limit": 1})
        assert pending.status_code == 200, pending.text
        assert pending.json()["data"][0] == pending_tool
    finally:
        await conn.close()
        await delete_created_threads(*session_ids)


async def test_items_page_bounds_database_rows_and_splits_one_message(test_client, admin_headers):
    """隐藏审计不计入页，同一消息的公开调用按索引跨页且数据库只返回有限行。"""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from yuxi.modules.agents.repositories.public_items import PublicItemRepository

    created = await test_client.post(
        "/api/v1/agents/sessions",
        headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"agent_id": "default-chatbot", "title": make_test_session_title("bounded-items")},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        record = await conn.fetchrow("SELECT id,uid,app_id FROM sessions WHERE thread_id=$1", session_id)
        await conn.executemany(
            "INSERT INTO messages (session_record_id,role,content,extra_metadata,created_at,delivery_status) "
            "VALUES ($1,'assistant','PRIVATE_AUDIT','{}'::json,now(),'complete')",
            [(record["id"],)] * 250,
        )
        public = {
            str(index): {
                "id": f"visible-{session_id}-{index}",
                "type": "function_call",
                "turn_id": None,
                "call_id": f"call-{index}",
                "name": "search",
                "arguments": {"q": str(index)},
                "status": "completed",
                "yuxi": {"output_index": index},
            }
            for index in [2, 0, 1]
        }
        await conn.execute(
            "INSERT INTO messages (session_record_id,role,content,extra_metadata,created_at,delivery_status) "
            "VALUES ($1,'assistant','PRIVATE_AUDIT',$2::json,now(),'complete')",
            record["id"],
            json.dumps({"public_items": public}),
        )
        expected = [public[str(index)]["id"] for index in range(3)]
        for order in ("asc", "desc"):
            seen, after = [], None
            while True:
                response = await test_client.get(
                    f"/api/v1/agents/sessions/{session_id}/items",
                    headers=admin_headers,
                    params={"limit": 1, "order": order, **({"after": after} if after else {})},
                )
                assert response.status_code == 200, response.text
                page = response.json()
                assert len(page["data"]) == 1 and "PRIVATE_AUDIT" not in response.text
                seen.append(page["last_id"])
                if not page["has_more"]:
                    break
                after = page["last_id"]
            assert seen == (expected if order == "asc" else list(reversed(expected)))
        statements = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
        )
        async with async_sessionmaker(engine)() as db:
            rows = await PublicItemRepository(db).list_page(
                thread_id=session_id,
                uid=record["uid"],
                app_id=record["app_id"],
                turn_id=None,
                after=None,
                limit=1,
                order="asc",
            )
            assert len(rows) == 2
        assert len(statements) == 1 and "LIMIT" in statements[0].upper()
        old = await test_client.get(f"/api/v1/agents/sessions/{session_id}/history", headers=admin_headers)
        assert old.status_code == 404
    finally:
        await engine.dispose()
        await conn.close()
        await delete_created_threads(session_id)


async def test_receipt_recovery_is_scoped_and_preserves_queued_input(test_client, admin_headers):
    """相同原键找回同一排队 Input；其他 APP 和变更意图不能冒用回执。"""
    keys, sessions, headers = [], [], []
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    original_key = f"retry/with spaces-{uuid.uuid4()}"
    try:
        for _ in range(2):
            issued = await test_client.post(
                "/api/user/apikey/",
                headers=admin_headers,
                json={
                    "request_id": str(uuid.uuid4()),
                    "name": "Receipt recovery test",
                    "access_level": "agents",
                    "app_id": f"receipt-{uuid.uuid4()}",
                },
            )
            assert issued.status_code == 200, issued.text
            keys.append(issued.json()["api_key"]["id"])
            headers.append({"Authorization": f"Bearer {issued.json()['secret']}"})
            created = await test_client.post(
                "/api/v1/agents/sessions",
                headers={**headers[-1], "Idempotency-Key": original_key},
                json={"agent_id": "default-chatbot", "title": make_test_session_title("receipt")},
            )
            assert created.status_code == 201, created.text
            sessions.append(created.json()["id"])
        session_id = sessions[0]
        await conn.execute("UPDATE sessions SET queue_paused=true WHERE thread_id=$1", session_id)
        url = f"/api/v1/agents/sessions/{session_id}"
        body = {
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "queued original"}]}],
                    "yuxi": {"mode": "follow_up"},
                }
            ]
        }
        send_key = f"send-{original_key}"
        accepted = await test_client.post(
            f"{url}/events", headers={**headers[0], "Idempotency-Key": send_key}, json=body
        )
        assert accepted.status_code == 202, accepted.text
        receipt = await test_client.get(f"{url}/receipt", headers=headers[0], params={"idempotency_key": send_key})
        assert receipt.status_code == 200 and receipt.json() == accepted.json()
        assert receipt.json()["turn_id"] is None and receipt.json()["run_id"] is None
        assert (
            await test_client.get(f"{url}/receipt", headers=headers[0], params={"idempotency_key": "missing"})
        ).status_code == 404
        assert (
            await test_client.get(f"{url}/receipt", headers=headers[1], params={"idempotency_key": send_key})
        ).status_code == 404
        replay = await test_client.post(f"{url}/events", headers={**headers[0], "Idempotency-Key": send_key}, json=body)
        assert replay.json() == receipt.json()
        changed = await test_client.post(
            f"{url}/events",
            headers={**headers[0], "Idempotency-Key": send_key},
            json={
                "events": [
                    {
                        "type": "agent.session.input.message",
                        "input": [{"role": "user", "content": [{"type": "input_text", "text": "changed"}]}],
                    }
                ]
            },
        )
        assert changed.status_code == 409
        assert await conn.fetchval("SELECT count(*) FROM agent_inputs WHERE thread_id=$1", session_id) == 1
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM messages m JOIN sessions s ON s.id=m.session_record_id WHERE s.thread_id=$1",
                session_id,
            )
            == 1
        )
    finally:
        await conn.close()
        await delete_created_threads(*sessions)
        for key_id in keys:
            await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)
