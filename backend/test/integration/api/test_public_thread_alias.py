"""真实 HTTP 和 PostgreSQL 验证 Thread/Session 共用生命周期。"""

from __future__ import annotations

import os
import asyncio
import json
import uuid

import asyncpg
import pytest
from test.live_api_cleanup import make_test_conversation_title

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_thread_session_creation_replays_one_receipt_and_archives(test_client, admin_headers):
    """两种协议名称只创建一份空 Thread、回执和归档历史。"""
    directory = await test_client.get("/api/v1/agents", headers=admin_headers)
    assert directory.status_code == 200, directory.text
    agent = directory.json()["data"][0]
    agent_slug = agent.get("id") or agent.get("slug") or agent["agent_id"]
    key = str(uuid.uuid4())
    headers = {**admin_headers, "Idempotency-Key": key}
    body = {"agent_id": agent_slug, "title": make_test_conversation_title("public-alias")}

    thread = await test_client.post("/api/v1/agents/threads", headers=headers, json=body)
    assert thread.status_code == 200, thread.text
    thread_id = thread.json()["thread_id"]
    assert thread.json()["id"] == thread_id
    assert thread.json()["title"] == body["title"]
    assert thread.json()["project_id"]
    assert thread.json()["status"] == "accepted"

    session = await test_client.post("/api/v1/agents/sessions", headers=headers, json=body)
    assert session.status_code == 200, session.text
    assert session.json()["id"] == session.json()["session_id"] == thread_id
    assert session.json()["event_id"] == thread.json()["event_id"]
    assert session.json()["title"] == thread.json()["title"]
    assert session.json()["project_id"] == thread.json()["project_id"]
    assert session.json()["status"] == "accepted"

    for resource in ("threads", "sessions"):
        async with test_client.stream(
            "GET",
            f"/api/v1/agents/{resource}/{thread_id}/events",
            headers={**admin_headers, "Last-Event-ID": "invalid-cursor"},
        ) as invalid_cursor:
            assert invalid_cursor.status_code == 422, (resource, invalid_cursor.status_code)

    changed = await test_client.post(
        "/api/v1/agents/sessions", headers=headers, json={**body, "title": "changed intent"}
    )
    assert changed.status_code == 409, changed.text

    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        counts = await conn.fetchrow(
            "SELECT "
            "(SELECT COUNT(*) FROM conversations WHERE thread_id = $1) AS threads, "
            "(SELECT COUNT(*) FROM agent_input_receipts WHERE conversation_thread_id = $1) AS receipts, "
            "(SELECT COUNT(*) FROM agent_inputs WHERE conversation_thread_id = $1) AS inputs, "
            "(SELECT COUNT(*) FROM agent_turns WHERE conversation_thread_id = $1) AS turns",
            thread_id,
        )
        assert dict(counts) == {"threads": 1, "receipts": 1, "inputs": 0, "turns": 0}
        persisted = await conn.fetchrow("SELECT title, project_id FROM conversations WHERE thread_id = $1", thread_id)
        assert persisted["title"] == thread.json()["title"]
        assert persisted["project_id"] == thread.json()["project_id"]
    finally:
        await conn.close()

    archived = await test_client.post(f"/api/v1/agents/threads/{thread_id}/archive", headers=admin_headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    session_read = await test_client.get(f"/api/v1/agents/sessions/{thread_id}", headers=admin_headers)
    assert session_read.status_code == 200, session_read.text
    assert session_read.json()["status"] == "archived"
    assert session_read.json()["session_id"] == thread_id

    old_delete = await test_client.delete(f"/api/chat/thread/{thread_id}", headers=admin_headers)
    assert old_delete.status_code == 404, old_delete.text


async def test_public_resume_accepts_web_multiselect_and_other_answers(test_client, admin_headers):
    """两种 Web 答案经 HTTP 消费等待点，并在 PG 归属原 Turn 与新 Run。"""
    suffix = uuid.uuid4().hex[:8]
    provider_id = f"ci-resume-wire-{suffix}"
    agent_slug = f"ci-resume-wire-{suffix}"
    model_spec = f"{provider_id}:deterministic-chat"
    thread_id = turn_id = None
    provider = await test_client.post(
        "/api/system/model-providers",
        headers=admin_headers,
        json={
            "provider_id": provider_id,
            "display_name": "CI resume wire",
            "provider_type": "openai",
            "base_url": "http://api:8765/v1",
            "api_key": "ci-replay-key",
            "capabilities": ["chat"],
            "enabled_models": [
                {
                    "id": "deterministic-chat",
                    "display_name": "Deterministic chat",
                    "type": "chat",
                    "source": "manual",
                }
            ],
            "is_enabled": True,
        },
    )
    assert provider.status_code == 200, provider.text
    try:
        me = await test_client.get("/api/auth/me", headers=admin_headers)
        assert me.status_code == 200, me.text
        agent = await test_client.post(
            "/api/agent",
            headers=admin_headers,
            json={
                "name": f"Resume wire {suffix}",
                "slug": agent_slug,
                "backend_id": "ChatbotAgent",
                "description": "Public answer wire integration",
                "config_json": {
                    "context": {
                        "model": model_spec,
                        "system_prompt": "不要调用工具，只输出 DETERMINISTIC_AGENT_E2E_OK。",
                        "tools": ["ask_user_question"],
                        "knowledges": [],
                        "mcps": [],
                        "skills": ["image-gen"],
                        "preload_skills": ["image-gen"],
                        "subagents": [],
                    }
                },
                "share_config": {
                    "version": 2,
                    "read_scope": {
                        "access_level": "user",
                        "department_ids": [],
                        "user_uids": [str(me.json()["uid"])],
                    },
                    "manage_scope": None,
                },
            },
        )
        assert agent.status_code == 200, agent.text
        created = await test_client.post(
            "/api/v1/agents/threads",
            headers={**admin_headers, "Idempotency-Key": f"resume-wire-{suffix}"},
            json={
                "agent_id": agent_slug,
                "title": make_test_conversation_title("resume-wire"),
                "model_spec": model_spec,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "DETERMINISTIC_AGENT_E2E_OK DETERMINISTIC_ASK_USER",
                            }
                        ],
                    }
                ],
            },
        )
        assert created.status_code == 200, created.text
        initial = created.json()
        thread_id, turn_id = initial["thread_id"], initial["turn_id"]
        turn_url = f"/api/v1/agents/threads/{thread_id}/turns/{turn_id}"
        for _ in range(100):
            waiting_response = await test_client.get(turn_url, headers=admin_headers)
            assert waiting_response.status_code == 200, waiting_response.text
            waiting = waiting_response.json()
            if waiting["status"] == "waiting":
                break
            assert waiting["status"] not in {"completed", "failed", "cancelled"}, waiting
            await asyncio.sleep(0.2)
        else:
            pytest.fail("Turn 未进入等待点")

        waitpoint = waiting["waitpoint"]
        assert [question["question_id"] for question in waitpoint["questions"]] == ["q-1", "q-2"]
        answer_response = {
            "type": "answer",
            "answers": [
                {"question_id": "q-1", "answer": ["杭州", "上海"]},
                {
                    "question_id": "q-2",
                    "answer": {
                        "type": "other",
                        "text": "苏州",
                        "selected": ["杭州"],
                    },
                },
            ],
        }
        resumed = await test_client.post(
            f"/api/v1/agents/threads/{thread_id}/events",
            headers={**admin_headers, "Idempotency-Key": f"resume-answer-{suffix}"},
            json={
                "events": [
                    {
                        "type": "yuxi.thread.input.resume",
                        "turn_id": turn_id,
                        "waitpoint_id": waitpoint["id"],
                        "response": answer_response,
                    }
                ]
            },
        )
        assert resumed.status_code == 202, resumed.text
        resume_run_id = resumed.json()["run_id"]
        assert resume_run_id and resume_run_id != initial["run_id"]

        conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
        try:
            turn = await conn.fetchrow(
                "SELECT status, current_run_id, waitpoint FROM agent_turns WHERE id = $1", turn_id
            )
            assert turn["status"] in {"running", "completed"}
            assert turn["current_run_id"] == resume_run_id
            stored_waitpoint = turn["waitpoint"]
            if isinstance(stored_waitpoint, str):
                stored_waitpoint = json.loads(stored_waitpoint)
            assert stored_waitpoint is None
            runs = await conn.fetch(
                "SELECT id, turn_id, resume_from_run_id FROM agent_runs WHERE turn_id = $1 ORDER BY execution_seq",
                turn_id,
            )
            assert [run["id"] for run in runs] == [initial["run_id"], resume_run_id]
            assert all(run["turn_id"] == turn_id for run in runs)
            assert runs[1]["resume_from_run_id"] == initial["run_id"]
            content = await conn.fetchval(
                "SELECT content FROM messages WHERE turn_id = $1 AND run_id = $2 AND message_type = 'resume'",
                turn_id,
                resume_run_id,
            )
            assert json.loads(content) == answer_response
        finally:
            await conn.close()

        for _ in range(100):
            final_response = await test_client.get(turn_url, headers=admin_headers)
            assert final_response.status_code == 200, final_response.text
            final = final_response.json()
            if final["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.2)
        else:
            pytest.fail("恢复 Run 未在 20 秒内结束")
        assert final["status"] == "completed", final
        assert final["result_run_id"] == resume_run_id
        assert final["waitpoint"] is None
    finally:
        if thread_id and turn_id:
            current = await test_client.get(
                f"/api/v1/agents/threads/{thread_id}/turns/{turn_id}", headers=admin_headers
            )
            if current.status_code == 200 and current.json()["status"] not in {"completed", "failed", "cancelled"}:
                await test_client.post(
                    f"/api/v1/agents/threads/{thread_id}/events",
                    headers={**admin_headers, "Idempotency-Key": f"resume-cleanup-{suffix}"},
                    json={"events": [{"type": "yuxi.thread.input.cancel", "turn_id": turn_id}]},
                )
            await test_client.post(f"/api/v1/agents/threads/{thread_id}/archive", headers=admin_headers)
        await test_client.delete(f"/api/agent/{agent_slug}", headers=admin_headers)
        await test_client.delete(f"/api/system/model-providers/{provider_id}", headers=admin_headers)
