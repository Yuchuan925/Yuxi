"""Public Agent 对话入口的认证与缺失资源边界。"""

import os
import uuid

import asyncpg
import pytest
from test.live_api_cleanup import make_test_session_title
from yuxi.infrastructure.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_public_thread_endpoints_require_authentication(test_client):
    """创建、读取和取消都由 Public 认证边界保护。"""
    thread_id = str(uuid.uuid4())
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    created = await test_client.post(
        "/api/v1/agents/sessions",
        json={"agent_id": "default-chatbot"},
        headers=headers,
    )
    assert created.status_code == 401
    assert (await test_client.get(f"/api/v1/agents/sessions/{thread_id}")).status_code == 401
    cancelled = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}/events",
        json={"events": [{"type": "agent.session.input.cancel", "yuxi": {"turn_id": str(uuid.uuid4())}}]},
        headers=headers,
    )
    assert cancelled.status_code == 401


async def test_public_create_rejects_empty_input(test_client, admin_headers):
    """空输入不能伪装成第一轮工作。"""
    response = await test_client.post(
        "/api/v1/agents/sessions",
        json={"agent_id": "default-chatbot", "input": []},
        headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422


async def test_public_missing_thread_and_turn_return_not_found(test_client, admin_headers):
    """未知 Thread 与 Turn 不会退回相邻执行结果。"""
    thread_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    assert (await test_client.get(f"/api/v1/agents/sessions/{thread_id}", headers=admin_headers)).status_code == 404
    turn = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/turns/{turn_id}", headers=admin_headers)
    assert turn.status_code == 404


async def test_removed_agent_session_entrypoints_are_unreachable(test_client, admin_headers):
    """旧聊天、Channel 和 Invocation 入口不再接收执行请求。"""
    requests = (
        ("/api/chat/thread", {"agent_id": "default-chatbot"}),
        ("/api/agent/runs", {"query": "hello", "agent_slug": "default-chatbot"}),
        ("/api/agent-invocation/channel/messages", {"message": {"type": "text", "text": "hello"}}),
        ("/api/agent-invocation/eval/runs", {"query": "hello"}),
        ("/api/agent-invocation/agent-call/runs", {"messages": []}),
    )
    for path, body in requests:
        response = await test_client.post(path, json=body, headers=admin_headers)
        assert response.status_code in {404, 405}, (path, response.text)


@pytest.mark.parametrize("attachment_kind", ["unknown", "bound_elsewhere"])
async def test_public_input_rejects_unbound_attachment_without_persisting_receipt(
    test_client, admin_headers, attachment_kind
):
    """显式附件 ID 无法属于本次 Input 时，不能确认接收或投递 Run。"""
    directory = await test_client.get("/api/v1/agents", headers=admin_headers)
    assert directory.status_code == 200, directory.text
    agent = directory.json()["data"][0]
    slug = agent.get("id") or agent.get("slug") or agent["agent_id"]
    created = await test_client.post(
        "/api/v1/agents/sessions",
        json={"agent_id": slug, "title": make_test_session_title("attachment-input")},
        headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 201, created.text
    thread_id = created.json()["id"]
    file_id = uuid.uuid4().hex
    accepted = None
    uploaded = None
    event_key = str(uuid.uuid4())
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    response = None
    try:
        await conn.execute("UPDATE sessions SET queue_paused = TRUE WHERE thread_id = $1", thread_id)
        body = {
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "read attachment"}]}],
                    "yuxi": {"mode": "follow_up", "attachment_file_ids": [file_id]},
                }
            ]
        }
        if attachment_kind == "bound_elsewhere":
            uploaded = await test_client.post(
                "/api/v1/agents/files", files={"file": ("source.txt", b"original", "text/plain")}, headers=admin_headers
            )
            assert uploaded.status_code == 201, uploaded.text
            file_id = uploaded.json()["id"]
            body["events"][0]["yuxi"]["attachment_file_ids"] = [file_id]
            accepted = await test_client.post(
                f"/api/v1/agents/sessions/{thread_id}/events",
                json=body,
                headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
            )
            assert accepted.status_code == 202, accepted.text

        response = await test_client.post(
            f"/api/v1/agents/sessions/{thread_id}/events",
            json=body,
            headers={**admin_headers, "Idempotency-Key": event_key},
        )
        assert response.status_code == (409 if accepted else 404), response.text
        facts = await conn.fetchrow(
            "SELECT "
            "(SELECT COUNT(*) FROM agent_input_receipts WHERE thread_id = $1 "
            "AND idempotency_key = $2) AS receipts, "
            "(SELECT COUNT(*) FROM agent_inputs WHERE thread_id = $1) AS inputs, "
            "(SELECT COUNT(*) FROM agent_runs WHERE thread_id = $1) AS runs, "
            "(SELECT COUNT(*) FROM messages WHERE session_record_id = "
            "(SELECT id FROM sessions WHERE thread_id = $1)) AS messages",
            thread_id,
            event_key,
        )
        assert dict(facts) == {
            "receipts": 0,
            "inputs": int(accepted is not None),
            "runs": 0,
            "messages": int(accepted is not None),
        }
    finally:
        if accepted is not None:
            input_id = accepted.json().get("input_id")
            if input_id:
                cancelled = await test_client.post(
                    f"/api/v1/agents/sessions/{thread_id}/events",
                    json={"events": [{"type": "yuxi.session.input.cancel_input", "input_id": input_id}]},
                    headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
                )
                assert cancelled.status_code == 202, cancelled.text
        archived = await test_client.post(f"/api/v1/agents/sessions/{thread_id}/archive", headers=admin_headers)
        assert archived.status_code == 200, archived.text
        await conn.close()
        if uploaded is not None and uploaded.status_code == 201:
            uid = str((await test_client.get("/api/auth/me", headers=admin_headers)).json()["uid"])
            minio = get_minio_client()
            await minio.adelete_objects_by_prefix(
                minio.KB_BUCKETS["documents"], f"tmp/chat_attachments/{uid}/{file_id}/"
            )
