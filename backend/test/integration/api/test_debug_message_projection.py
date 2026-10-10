"""真实 HTTP 与 PostgreSQL 验证调试面板的持久消息顺序。"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from test.integration.api.test_public_agents_key_boundary import delete_created_threads
from test.live_api_cleanup import make_test_session_title

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_debug_messages_follow_database_sequence_and_include_unassigned_inputs(test_client, admin_headers):
    """插入顺序与时间冲突时遵循 sequence，未关联输入和系统消息仍可核对。"""
    created = await test_client.post(
        "/api/v1/agents/sessions",
        headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"agent_id": "default-chatbot", "title": make_test_session_title("debug-projection")},
    )
    assert created.status_code == 201, created.text
    thread_id = created.json()["id"]
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        session = await conn.fetchrow("SELECT id,uid,agent_id FROM sessions WHERE thread_id=$1", thread_id)
        turn_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        start = datetime(2026, 10, 10, tzinfo=UTC)
        await conn.execute(
            "INSERT INTO agent_turns (id,thread_id,uid,status,created_at) VALUES ($1,$2,$3,'completed',$4)",
            turn_id,
            thread_id,
            session["uid"],
            start,
        )
        await conn.execute(
            "INSERT INTO agent_runs "
            "(id,thread_id,runtime_scope_id,agent_slug,uid,turn_id,status,session_record_id,"
            "run_type,source,channel,input_payload,token_usage,origin_metadata,created_at) "
            "VALUES ($1,$2,$2,$3,$4,$5,'completed',$6,'chat','public_api','api','{}','{}','{}',$7)",
            run_id,
            thread_id,
            session["agent_id"],
            session["uid"],
            turn_id,
            session["id"],
            start,
        )
        ids = {}
        for role, content, sequence, operation in [
            ("assistant", "最终模型输出", 9, "model-final"),
            ("tool", "工具结果", 6, "tool-call"),
            ("assistant", "第一模型输出", 3, "model-first"),
            ("user", "持久用户输入", None, None),
        ]:
            ids[content] = await conn.fetchval(
                "INSERT INTO messages "
                "(session_record_id,role,content,run_id,turn_id,sequence,operation_id,created_at,"
                "delivery_status,extra_metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'complete',$9::json) RETURNING id",
                session["id"],
                role,
                content,
                run_id,
                turn_id,
                sequence,
                operation,
                start + timedelta(seconds=20 - (sequence or 0)),
                json.dumps({"input_id": "input-test"} if role == "user" else {"tool_name": "search"}),
            )
        for role, content in [("system", "系统记录"), ("user", "未关联输入")]:
            ids[content] = await conn.fetchval(
                "INSERT INTO messages (session_record_id,role,content,created_at,delivery_status,extra_metadata) "
                "VALUES ($1,$2,$3,$4,'queued','{}') RETURNING id",
                session["id"],
                role,
                content,
                start + timedelta(seconds=30),
            )
        error_id = await conn.fetchval(
            "INSERT INTO messages "
            "(session_record_id,role,content,run_id,turn_id,created_at,delivery_status,extra_metadata) "
            "VALUES ($1,'assistant','',$2,$3,$4,'complete',$5::json) RETURNING id",
            session["id"],
            run_id,
            turn_id,
            start,
            json.dumps({"error_type": "provider_error", "error_message": "模型服务不可用", "is_error": True}),
        )
        response = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/audits", headers=admin_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        expected = ["持久用户输入", "第一模型输出", "工具结果", "最终模型输出", "系统记录", "未关联输入"]
        assert [row["id"] for row in payload["audits"]] == [
            *[ids[content] for content in expected[:4]],
            error_id,
            *[ids[content] for content in expected[4:]],
        ]
        assert [row["type"] for row in payload["audits"]] == ["human", "ai", "tool", "ai", "ai", "system", "human"]
        assert payload["audits"][0]["input_id"] == "input-test"
        assert payload["audits"][4]["error_type"] == "provider_error"
        assert payload["audits"][4]["error_message"] == "模型服务不可用"
        assert payload["audits"][4]["execution_status"] is None
        assert payload["audits"][2]["tool_name"] == "search"
        assert payload["audits"][-1]["run_id"] is None
        assert payload["audits"][-1]["delivery_status"] == "queued"
        assert payload["truncated"] is False
        rows = await conn.fetch("SELECT id,content,sequence FROM messages WHERE session_record_id=$1", session["id"])
        persisted = {row["id"]: row for row in rows}
        for projected in payload["audits"]:
            assert projected["content"] == persisted[projected["id"]]["content"]
            assert projected["sequence"] == persisted[projected["id"]]["sequence"]
    finally:
        await conn.close()
        await delete_created_threads(thread_id)
