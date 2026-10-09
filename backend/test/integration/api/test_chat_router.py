"""
Integration tests for chat router endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import asyncpg
import pytest
from PIL import Image

from test.live_api_cleanup import make_test_session_title

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _postgres_dsn() -> str:
    return os.getenv("POSTGRES_URL", "postgresql+asyncpg://postgres:postgres@postgres:5432/yuxi").replace(
        "+asyncpg", ""
    )


async def _upload_project_file(
    test_client,
    headers,
    thread_id: str,
    name: str,
    content: bytes,
    *,
    parent_path: str = "/",
    artifact_path: bool = False,
) -> str:
    response = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": parent_path},
        files={"files": (name, content, "text/plain")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    entry = response.json()["entries"][0]
    if not artifact_path:
        return entry["path"]
    marker = f"/api/v1/agents/sessions/{thread_id}/artifacts/"
    assert entry["artifact_url"].startswith(marker)
    return f"/{entry['artifact_url'][len(marker) :]}"


async def test_public_thread_endpoints_require_authentication(test_client):
    assert (await test_client.get("/api/v1/agents/sessions")).status_code == 401
    assert (await test_client.get(f"/api/v1/agents/sessions/{uuid.uuid4()}/audits")).status_code == 401
    assert (await test_client.get("/api/agent")).status_code == 401


async def test_thread_message_audits_return_persisted_facts_without_leaking_into_history(
    test_client,
    standard_user,
    admin_headers,
):
    standard_headers = standard_user["headers"]
    standard_thread_id = await _create_thread_for_user(test_client, standard_headers)

    message_audit_forbidden = await test_client.get(
        f"/api/v1/agents/sessions/{standard_thread_id}/audits",
        headers=standard_headers,
    )
    assert message_audit_forbidden.status_code == 403, message_audit_forbidden.text

    message_audit_cross_user = await test_client.get(
        f"/api/v1/agents/sessions/{standard_thread_id}/audits",
        headers=admin_headers,
    )
    assert message_audit_cross_user.status_code == 404, message_audit_cross_user.text

    thread_id = await _create_thread_for_user(test_client, admin_headers)
    run_id = f"run-{uuid.uuid4()}"
    failed_run_id = f"run-{uuid.uuid4()}"
    turn_id = f"turn-{uuid.uuid4()}"
    failed_turn_id = f"turn-{uuid.uuid4()}"
    started_at = datetime(2026, 8, 30, 1, 0, 0, tzinfo=UTC)

    conn = await asyncpg.connect(_postgres_dsn())
    try:
        agent_session = await conn.fetchrow(
            "SELECT id, uid, agent_id FROM sessions WHERE thread_id = $1",
            thread_id,
        )
        assert agent_session
        await conn.executemany(
            """
            INSERT INTO agent_turns
                (id, thread_id, uid, status, created_at, finished_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            [
                (turn_id, thread_id, agent_session["uid"], "completed", started_at, started_at + timedelta(seconds=3)),
                (
                    failed_turn_id,
                    thread_id,
                    agent_session["uid"],
                    "failed",
                    started_at + timedelta(seconds=4),
                    started_at + timedelta(seconds=5),
                ),
            ],
        )
        await conn.execute(
            """
            INSERT INTO agent_runs
                (id, thread_id, runtime_scope_id, agent_slug, uid, status,
                 turn_id, source, channel, session_record_id, run_type, input_payload, token_usage,
                 origin_metadata, created_at, started_at, finished_at)
            VALUES ($1, $2, $2, $3, $4, 'completed', $5, 'chat', 'web', $6, 'chat', '{}'::jsonb,
                    '{}'::jsonb, '{}'::jsonb, $7, $8, $9)
            """,
            run_id,
            thread_id,
            agent_session["agent_id"],
            agent_session["uid"],
            turn_id,
            agent_session["id"],
            started_at,
            started_at,
            started_at + timedelta(seconds=3),
        )
        await conn.execute(
            """
            INSERT INTO agent_runs
                (id, thread_id, runtime_scope_id, agent_slug, uid, status,
                 turn_id, source, channel, session_record_id, run_type, input_payload, token_usage,
                 origin_metadata, error_type, created_at, started_at, finished_at)
            VALUES ($1, $2, $2, $3, $4, 'failed', $5, 'chat', 'web', $6, 'chat', '{}'::jsonb,
                    '{}'::jsonb, '{}'::jsonb, 'invalid_input', $7, $7, $8)
            """,
            failed_run_id,
            thread_id,
            agent_session["agent_id"],
            agent_session["uid"],
            failed_turn_id,
            agent_session["id"],
            started_at + timedelta(seconds=4),
            started_at + timedelta(seconds=5),
        )
        await conn.execute(
            """
            INSERT INTO messages
                (session_record_id, role, content, delivery_status, extra_metadata, run_id,
                 turn_id, created_at)
            VALUES ($1, 'user', '会在审计前失败', 'failed', '{}'::jsonb, $2, $3, $4)
            """,
            agent_session["id"],
            failed_run_id,
            failed_turn_id,
            started_at + timedelta(seconds=4),
        )
        await conn.executemany(
            """
            INSERT INTO messages
                (session_record_id, role, content, message_type, delivery_status, extra_metadata, run_id,
                 turn_id, operation_id, started_at, finished_at, duration_ms, sequence,
                 execution_status, usage)
            VALUES ($1, 'assistant', $2, 'model_audit', 'complete', $3::jsonb, $4, $5, $6, $7, $8,
                    $9, $10, 'completed', $11::jsonb)
            """,
            [
                (
                    agent_session["id"],
                    "第二次模型输出",
                    json.dumps(
                        {
                            "finished_sequence": 9,
                            "content": [{"type": "text", "text": "第二次模型输出"}],
                            "private_internal_field": "must-not-leak",
                        },
                        ensure_ascii=False,
                    ),
                    run_id,
                    turn_id,
                    "operation-2",
                    started_at + timedelta(seconds=2),
                    started_at + timedelta(seconds=3),
                    1000,
                    7,
                    json.dumps({"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}),
                ),
                (
                    agent_session["id"],
                    "第一次模型输出",
                    json.dumps(
                        {
                            "finished_sequence": 5,
                            "state_reconciled": True,
                            "private_internal_field": "must-not-leak",
                        }
                    ),
                    run_id,
                    turn_id,
                    "operation-1",
                    started_at,
                    started_at + timedelta(seconds=1),
                    1000,
                    3,
                    json.dumps({"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}),
                ),
            ],
        )
        model_messages = await conn.fetch(
            "SELECT id, operation_id FROM messages WHERE run_id = $1 AND role = 'assistant'",
            run_id,
        )
        message_ids = {row["operation_id"]: row["id"] for row in model_messages}
        await conn.executemany(
            """
            INSERT INTO tool_calls
                (message_id, langgraph_tool_call_id, tool_name, tool_input, tool_output, status)
            VALUES ($1, $2, 'search', '{}'::jsonb, $3, 'success')
            """,
            [
                (message_ids["operation-1"], "compat-call-proven", "safe result"),
                (message_ids["operation-2"], "compat-call-unproven", "must stay hidden"),
            ],
        )
        await conn.execute(
            """
            INSERT INTO messages
                (session_record_id, role, content, message_type, delivery_status, extra_metadata, run_id,
                 turn_id, operation_id, started_at, finished_at, duration_ms, sequence,
                 execution_status, usage)
            VALUES ($1, 'tool', '查询结果', 'tool_audit', 'complete', $2::jsonb, $3, $4, 'call-1',
                    $5, $6, 400, 6, 'completed', NULL)
            """,
            agent_session["id"],
            json.dumps(
                {
                    "tool_call_id": "call-1",
                    "tool_name": "search",
                    "input": {"q": "Yuxi"},
                    "output": {"type": "tool", "content": "查询结果", "status": "success"},
                    "source_model_operation_id": "operation-1",
                    "finished_sequence": 7,
                    "private_internal_field": "must-not-leak",
                },
                ensure_ascii=False,
            ),
            run_id,
            turn_id,
            started_at + timedelta(seconds=1),
            started_at + timedelta(milliseconds=1400),
        )
        await conn.execute(
            """
            INSERT INTO messages
                (session_record_id, role, content, message_type, delivery_status, extra_metadata, run_id,
                 turn_id, operation_id, sequence, execution_status)
            SELECT $1, 'assistant', 'bounded-' || sequence_value, 'model_audit', 'complete', '{}'::jsonb,
                   $2, $3, 'bounded-' || sequence_value, sequence_value, 'completed'
            FROM generate_series(10, 507) AS generated(sequence_value)
            """,
            agent_session["id"],
            run_id,
            turn_id,
        )
    finally:
        await conn.close()

    timeline_response = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/audits", headers=admin_headers)
    assert timeline_response.status_code == 200, timeline_response.text
    timeline_payload = timeline_response.json()
    timeline = timeline_payload["audits"]
    assert timeline_payload["truncated"] is True
    assert timeline_payload["runs_truncated"] is False
    assert timeline_payload["runs"] == [
        {
            "run_id": run_id,
            "status": "completed",
            "timing": {
                "created_at": "2026-08-30T01:00:00Z",
                "started_at": "2026-08-30T01:00:00Z",
                "prepared_at": None,
                "first_model_request_at": None,
                "first_output_at": None,
                "finished_at": "2026-08-30T01:00:03Z",
                "dispatch_latency_ms": 0,
                "preparation_latency_ms": None,
                "first_model_request_latency_ms": None,
                "model_first_output_latency_ms": None,
                "first_output_latency_ms": None,
                "total_latency_ms": 3000,
            },
        },
        {
            "run_id": failed_run_id,
            "status": "failed",
            "timing": {
                "created_at": "2026-08-30T01:00:04Z",
                "started_at": "2026-08-30T01:00:04Z",
                "prepared_at": None,
                "first_model_request_at": None,
                "first_output_at": None,
                "finished_at": "2026-08-30T01:00:05Z",
                "dispatch_latency_ms": 0,
                "preparation_latency_ms": None,
                "first_model_request_latency_ms": None,
                "model_first_output_latency_ms": None,
                "first_output_latency_ms": None,
                "total_latency_ms": 1000,
            },
        },
    ]
    assert len(timeline) == 500
    assert [audit["operation_id"] for audit in timeline[:2]] == ["call-1", "operation-2"]
    assert [audit["type"] for audit in timeline[:2]] == ["tool", "ai"]
    assert timeline[0]["tool_name"] == "search"
    assert timeline[0]["tool_input"] == {"q": "Yuxi"}
    assert timeline[0]["content"] == "查询结果"
    assert timeline[0]["duration_ms"] == 400
    assert timeline[1]["sequence"] == 7
    assert timeline[1]["duration_ms"] == 1000
    assert timeline[1]["started_at"] == "2026-08-30T01:00:02Z"
    assert timeline[1]["finished_at"] == "2026-08-30T01:00:03Z"
    assert timeline[1]["usage"]["total_tokens"] == 10
    assert timeline[1]["content_blocks"] == [{"type": "text", "text": "第二次模型输出"}]
    assert timeline[-1]["operation_id"] == "bounded-507"
    assert timeline[-1]["sequence"] == 507
    assert "private_internal_field" not in timeline_response.text

    retired_response = await test_client.get(
        f"/api/v1/agents/sessions/{thread_id}/model-audits",
        headers=admin_headers,
    )
    assert retired_response.status_code == 404, retired_response.text

    history = await test_client.get(
        f"/api/v1/agents/sessions/{thread_id}/items?order=asc&limit=100", headers=admin_headers
    )
    assert history.status_code == 200, history.text
    history_items = history.json()["data"]
    # 未进入公开输出链路的原始审计不会因 state_reconciled 或 ToolCall 存在而暴露。
    assert len(history_items) == 1
    failed_input = history_items[0]
    assert failed_input["type"] == "message" and failed_input["role"] == "user"
    assert failed_input["yuxi"]["run_id"] == failed_run_id
    assert failed_input["yuxi"]["delivery_status"] == "failed"
    assert "must-not-leak" not in history.text
    assert "must stay hidden" not in history.text


async def test_image_upload_composites_transparent_png_pixels_on_white(test_client, admin_headers):
    image = Image.new("RGBA", (2, 2), (255, 255, 255, 0))
    image.putpixel((0, 0), (50, 87, 244, 0))
    image.putpixel((1, 0), (50, 87, 244, 255))

    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

    response = await test_client.post(
        "/api/agent/images",
        headers=admin_headers,
        files={"file": ("transparent.png", image_bytes, "image/png")},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mime_type"] == "image/png"

    processed_data = base64.b64decode(payload["image_url"].split(";base64,", 1)[1])
    with Image.open(io.BytesIO(processed_data)) as processed_image:
        rgb_image = processed_image.convert("RGB")

    assert rgb_image.getpixel((0, 0)) == (255, 255, 255)
    assert rgb_image.getpixel((1, 0)) == (50, 87, 244)


async def test_legacy_direct_thread_attachment_upload_is_removed(test_client, admin_headers):
    response = await test_client.post(
        f"/api/v1/agents/sessions/{uuid.uuid4()}/attachments",
        headers=admin_headers,
        files={"file": ("legacy.txt", b"legacy", "text/plain")},
    )

    assert response.status_code == 405


async def test_development_thread_file_browse_routes_are_removed(test_client, admin_headers):
    thread_id = await _create_thread_for_user(test_client, admin_headers)
    path = await _upload_project_file(test_client, admin_headers, thread_id, "removed-route.txt", b"content")

    list_response = await test_client.get(
        f"/api/v1/agents/sessions/{thread_id}/files",
        params={"path": "/"},
        headers=admin_headers,
    )
    content_response = await test_client.get(
        f"/api/v1/agents/sessions/{thread_id}/files/content",
        params={"path": path},
        headers=admin_headers,
    )

    assert list_response.status_code == 404
    assert content_response.status_code == 404


async def test_thread_artifact_uses_image_signature_for_content_type(test_client, admin_headers):
    thread_id = await _create_thread_for_user(test_client, admin_headers)
    image = Image.new("RGBA", (2, 2), (255, 255, 255, 0))

    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

    upload_response = await test_client.post(
        "/api/v1/agents/files",
        headers=admin_headers,
        files={"file": ("mislabeled.jpg", image_bytes, "image/jpeg")},
    )

    assert upload_response.status_code == 201, upload_response.text
    uploaded = upload_response.json()
    conn = await asyncpg.connect(_postgres_dsn())
    try:
        await conn.execute("UPDATE sessions SET queue_paused=true WHERE thread_id=$1", thread_id)
    finally:
        await conn.close()
    accepted = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}/events",
        headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "保存附件"}]}],
                    "yuxi": {"mode": "follow_up", "attachment_file_ids": [uploaded["id"]]},
                }
            ]
        },
    )
    assert accepted.status_code == 202, accepted.text
    attachment = (
        await test_client.get(f"/api/v1/agents/sessions/{thread_id}/attachments", headers=admin_headers)
    ).json()["attachments"][0]
    listed = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/attachments", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert any(item["file_id"] == attachment["file_id"] for item in listed.json()["attachments"])

    artifact_response = await test_client.get(attachment["original_artifact_url"], headers=admin_headers)

    assert artifact_response.status_code == 200, artifact_response.text
    assert artifact_response.headers["content-type"].startswith("image/png")
    assert artifact_response.content.startswith(b"\x89PNG\r\n\x1a\n")


async def test_thread_artifact_preview_http_preserves_raw_download(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id = await _create_thread_for_user(test_client, headers)
    content = b"# artifact preview\n"
    artifact_path = await _upload_project_file(
        test_client,
        headers,
        thread_id,
        f"preview-{uuid.uuid4().hex[:8]}.md",
        content,
        artifact_path=True,
    )
    artifact_url = f"/api/v1/agents/sessions/{thread_id}/artifacts/{artifact_path.lstrip('/')}"

    preview_response = await test_client.get(
        artifact_url,
        params={"preview": "true"},
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    assert preview_response.headers["content-type"].startswith("application/json")
    assert preview_response.json() == {
        "content": content.decode(),
        "preview_type": "markdown",
        "supported": True,
        "message": None,
        "truncated": False,
        "limit": 250_000,
    }

    raw_response = await test_client.get(artifact_url, headers=headers)
    assert raw_response.status_code == 200, raw_response.text
    assert raw_response.content == content
    assert raw_response.headers["content-type"].startswith("text/markdown")


async def _create_thread_for_user(test_client, headers: dict[str, str]) -> str:
    agents_resp = await test_client.get("/api/agent", headers=headers)
    assert agents_resp.status_code == 200, agents_resp.text
    agents = agents_resp.json().get("agents", [])
    assert agents, "Chat router integration requires at least one visible Agent."

    agent_id = agents[0].get("agent_id") or agents[0].get("slug")
    assert agent_id, f"Agent payload missing identifier: {agents[0]}"

    create_resp = await test_client.post(
        "/api/v1/agents/sessions",
        json={
            "agent_id": agent_id,
            "title": make_test_session_title("chat-router"),
        },
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_resp.status_code == 201, create_resp.text
    thread_id = create_resp.json()["id"]
    assert thread_id
    return thread_id


async def test_turn_pages_include_empty_turns_and_keep_viewed_explicit(test_client, admin_headers, standard_user):
    """分页发现零消息的轮次，读取不标记已读且不跨用户泄露。"""
    thread_id = await _create_thread_for_user(test_client, admin_headers)
    conn = await asyncpg.connect(_postgres_dsn())
    prefix = uuid.uuid4().hex
    started_at = datetime(2026, 9, 5, 0, 0, 0, tzinfo=UTC)
    try:
        empty = await test_client.get(f"/api/v1/agents/sessions/{thread_id}", headers=admin_headers)
        assert empty.status_code == 200, empty.text
        assert empty.json()["id"] == thread_id
        assert empty.json()["status"] == "idle"

        agent_session = await conn.fetchrow("SELECT * FROM sessions WHERE thread_id = $1", thread_id)
        marker = agent_session["last_viewed_run_id"]
        # 超过审计窗口，验证普通历史不会静默截掉较早或零消息的 Run。
        await conn.executemany(
            """
            INSERT INTO agent_turns
                (id, thread_id, uid, status, created_at, finished_at)
            VALUES ($1, $2, $3, 'cancelled', $4, $5)
            """,
            [
                (
                    f"turn-{prefix}-{index}",
                    thread_id,
                    agent_session["uid"],
                    started_at + timedelta(seconds=index * 2),
                    started_at + timedelta(seconds=index * 2 + 1),
                )
                for index in range(501)
            ],
        )
        await conn.executemany(
            """
            INSERT INTO agent_runs
                (id, thread_id, runtime_scope_id, agent_slug, uid, status,
                 turn_id, source, channel, session_record_id, run_type, input_payload, token_usage,
                 origin_metadata, created_at, finished_at)
            VALUES ($1, $2, $2, $3, $4, 'cancelled', $5, 'chat', 'web', $6, 'chat',
                    '{"private_input":"must-not-leak"}'::jsonb, '{}'::jsonb, '{}'::jsonb, $7, $8)
            """,
            [
                (
                    f"{prefix}-{index:03}",
                    thread_id,
                    agent_session["agent_id"],
                    agent_session["uid"],
                    f"turn-{prefix}-{index}",
                    agent_session["id"],
                    started_at + timedelta(seconds=index * 2),
                    started_at + timedelta(seconds=index * 2 + 1),
                )
                for index in range(501)
            ],
        )
        await conn.execute(
            "UPDATE agent_turns t SET current_run_id=r.id FROM agent_runs r WHERE t.id=r.turn_id AND t.thread_id=$1",
            thread_id,
        )
        await conn.execute(
            """
            INSERT INTO messages
                (session_record_id, role, content, delivery_status, extra_metadata, run_id, turn_id, created_at)
            VALUES ($1, 'assistant', '历史回答', 'complete', '{}'::jsonb, $2, $4, $3),
                   ($1, 'assistant', '没有 Run 的旧回答', 'complete', '{}'::jsonb, NULL, NULL, $3)
            """,
            agent_session["id"],
            f"{prefix}-000",
            started_at,
            f"turn-{prefix}-0",
        )
        page_url = f"/api/v1/agents/sessions/{thread_id}/turns"
        turns, after = [], None
        while True:
            response = await test_client.get(
                page_url,
                headers=admin_headers,
                params={"order": "asc", "limit": 100, **({"after": after} if after else {})},
            )
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["data"]) <= 100
            assert "must-not-leak" not in response.text
            turns.extend(page["data"])
            if not page["has_more"]:
                break
            after = page["last_id"]
        assert [turn["id"] for turn in turns] == [f"turn-{prefix}-{index}" for index in range(501)]
        assert all(turn["status"] == "cancelled" for turn in turns)
        items = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/items", headers=admin_headers)
        assert items.json()["data"] == [], "没有公开身份的内部输出不应进入历史"
        assert await conn.fetchval("SELECT last_viewed_run_id FROM sessions WHERE thread_id = $1", thread_id) == marker

        denied = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/turns", headers=standard_user["headers"])
        assert denied.status_code == 404
        assert prefix not in denied.text
        viewed = await test_client.post(f"/api/v1/agents/sessions/{thread_id}/viewed", headers=admin_headers)
        assert viewed.status_code == 200, viewed.text
        assert viewed.json()["yuxi"]["unread"] is False
        assert (
            await conn.fetchval("SELECT last_viewed_run_id FROM sessions WHERE thread_id = $1", thread_id)
            == f"{prefix}-500"
        )
        reread = await test_client.get(f"/api/v1/agents/sessions/{thread_id}", headers=admin_headers)
        assert reread.json()["yuxi"]["unread"] is False
        deleted = await test_client.delete(f"/api/v1/agents/sessions/{thread_id}", headers=admin_headers)
        assert deleted.status_code == 405
        archived = await test_client.post(f"/api/v1/agents/sessions/{thread_id}/archive", headers=admin_headers)
        assert archived.status_code == 200, archived.text
        assert archived.json()["yuxi"]["archived"] is True
        archived_history = await test_client.get(
            f"/api/v1/agents/sessions/{thread_id}/turns?limit=100", headers=admin_headers
        )
        assert archived_history.status_code == 200, archived_history.text
        assert len(archived_history.json()["data"]) == 100 and archived_history.json()["has_more"]
    finally:
        await conn.close()


async def test_thread_tool_approval_mode_is_saved_in_session_snapshot(test_client, admin_headers):
    thread_id = await _create_thread_for_user(test_client, admin_headers)

    update_response = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}",
        headers=admin_headers,
        json={"yuxi": {"tool_approval_mode": "always_trust"}},
    )

    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["yuxi"]["tool_approval_mode"] == "always_trust"

    list_response = await test_client.get("/api/v1/agents/sessions", headers=admin_headers)
    assert list_response.status_code == 200, list_response.text
    thread = next(item for item in list_response.json()["data"] if item["id"] == thread_id)
    assert thread["yuxi"]["tool_approval_mode"] == "always_trust"


async def test_thread_tool_approval_mode_rejects_unknown_value(test_client, admin_headers):
    thread_id = await _create_thread_for_user(test_client, admin_headers)

    response = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}",
        headers=admin_headers,
        json={"yuxi": {"tool_approval_mode": "unknown"}},
    )

    assert response.status_code == 422, response.text


async def test_thread_list_exposes_work_status_and_unread(test_client, admin_headers):
    thread_id = await _create_thread_for_user(test_client, admin_headers)

    list_response = await test_client.get("/api/v1/agents/sessions", headers=admin_headers)
    assert list_response.status_code == 200, list_response.text
    thread = next(item for item in list_response.json()["data"] if item["id"] == thread_id)
    assert thread["status"] == "idle" and thread["yuxi"]["unread"] is False


async def test_mark_thread_viewed_returns_work_status_and_unread(test_client, admin_headers):
    thread_id = await _create_thread_for_user(test_client, admin_headers)

    response = await test_client.post(f"/api/v1/agents/sessions/{thread_id}/viewed", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "idle" and payload["yuxi"]["unread"] is False


async def test_mark_thread_viewed_requires_ownership(test_client, standard_user, admin_headers):
    headers = standard_user["headers"]
    thread_id = await _create_thread_for_user(test_client, headers)

    response = await test_client.post(f"/api/v1/agents/sessions/{thread_id}/viewed", headers=admin_headers)
    assert response.status_code == 404, response.text


async def test_admin_can_read_default_agent(test_client, admin_headers):
    response = await test_client.get("/api/agent/default", headers=admin_headers)
    assert response.status_code == 200, response.text
    agent = response.json()["agent"]
    assert agent["is_default"] is True
    assert agent["agent_id"]


async def test_agent_detail_filters_configurable_items_by_role(
    test_client,
    admin_headers,
    standard_user,
):
    agents_response = await test_client.get("/api/agent", headers=standard_user["headers"])
    assert agents_response.status_code == 200, agents_response.text
    agents = agents_response.json().get("agents", [])
    if not agents:
        pytest.skip("No agents are registered in the system.")

    agent_id = agents[0].get("agent_id") or agents[0].get("slug")
    if not agent_id:
        pytest.skip("Agent payload missing slug field.")

    user_agent_response = await test_client.get(f"/api/agent/{agent_id}", headers=standard_user["headers"])
    assert user_agent_response.status_code == 200, user_agent_response.text
    user_items = user_agent_response.json()["agent"].get("configurable_items", {})
    assert "summary_threshold" not in user_items
    assert "summary_keep_messages" not in user_items
    assert "summary_prompt" not in user_items
    assert "summary_tool_result_token_limit" not in user_items
    assert "max_execution_steps" not in user_items

    admin_agent_response = await test_client.get(f"/api/agent/{agent_id}", headers=admin_headers)
    assert admin_agent_response.status_code == 200, admin_agent_response.text
    admin_items = admin_agent_response.json()["agent"].get("configurable_items", {})
    assert "summary_threshold" in admin_items
    assert "summary_keep_messages" in admin_items
    assert "summary_prompt" in admin_items
    assert "summary_tool_result_token_limit" in admin_items
    assert "max_execution_steps" in admin_items


async def test_setting_default_agent_requires_admin(test_client, admin_headers, standard_user):
    agents_response = await test_client.get("/api/agent", headers=admin_headers)
    assert agents_response.status_code == 200, agents_response.text
    agents = agents_response.json().get("agents", [])

    if not agents:
        pytest.skip("No agents are registered in the system.")

    candidate_agent_id = agents[0].get("agent_id") or agents[0].get("slug")
    if not candidate_agent_id:
        pytest.skip("Agent payload missing slug field.")

    forbidden_response = await test_client.post(
        f"/api/agent/{candidate_agent_id}/set_default",
        headers=standard_user["headers"],
    )
    assert forbidden_response.status_code == 403

    update_response = await test_client.post(
        f"/api/agent/{candidate_agent_id}/set_default",
        headers=admin_headers,
    )
    assert update_response.status_code == 200, update_response.text
    agent = update_response.json()["agent"]
    assert agent["agent_id"] == candidate_agent_id
    assert agent["is_default"] is True


async def test_save_thread_artifact_to_workspace_copies_output_file(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id = await _create_thread_for_user(test_client, headers)
    filename = f"artifact-{uuid.uuid4().hex[:8]}.md"
    source_path = await _upload_project_file(
        test_client,
        headers,
        thread_id,
        filename,
        b"# artifact\n",
        artifact_path=True,
    )

    response = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}/artifacts/save",
        json={"path": source_path, "destination_path": "/saved_artifacts"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["name"] == filename
    assert payload["source_path"] == source_path
    assert payload["saved_path"] == f"/home/gem/user-data/saved_artifacts/{filename}"

    download_response = await test_client.get(payload["saved_artifact_url"], headers=headers)
    assert download_response.status_code == 200, download_response.text
    assert download_response.text == "# artifact\n"


async def test_save_thread_artifact_to_selected_workspace_directory(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id = await _create_thread_for_user(test_client, headers)
    filename = f"artifact-{uuid.uuid4().hex[:8]}.md"
    source_path = await _upload_project_file(
        test_client,
        headers,
        thread_id,
        filename,
        b"# selected destination\n",
        artifact_path=True,
    )
    destination_name = f"exports-{uuid.uuid4().hex[:8]}"
    directory = await test_client.post(
        "/api/workspace/directory",
        json={"parent_path": "/", "name": destination_name},
        headers=headers,
    )
    assert directory.status_code == 200, directory.text

    response = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}/artifacts/save",
        json={"path": source_path, "destination_path": f"/{destination_name}"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved_path"] == f"/home/gem/user-data/{destination_name}/{filename}"

    download_response = await test_client.get(payload["saved_artifact_url"], headers=headers)
    assert download_response.status_code == 200, download_response.text
    assert download_response.text == "# selected destination\n"


async def test_save_thread_artifact_to_workspace_auto_renames_conflicts(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id = await _create_thread_for_user(test_client, headers)
    filename = f"artifact-{uuid.uuid4().hex[:8]}.txt"
    renamed_filename = filename.replace(".txt", " (1).txt")

    source_path = await _upload_project_file(
        test_client,
        headers,
        thread_id,
        filename,
        b"first\n",
        artifact_path=True,
    )

    directory = await test_client.post(
        "/api/viewer/filesystem/directory",
        json={"thread_id": thread_id, "parent_path": "/", "name": "second-source"},
        headers=headers,
    )
    assert directory.status_code == 200, directory.text
    second_source_path = await _upload_project_file(
        test_client,
        headers,
        thread_id,
        filename,
        b"second\n",
        parent_path=directory.json()["entry"]["path"],
        artifact_path=True,
    )
    save_url = f"/api/v1/agents/sessions/{thread_id}/artifacts/save"
    first_response, second_response = await asyncio.gather(
        test_client.post(save_url, json={"path": source_path}, headers=headers),
        test_client.post(save_url, json={"path": second_source_path}, headers=headers),
    )
    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text

    first_payload = first_response.json()
    second_payload = second_response.json()
    assert {first_payload["saved_path"], second_payload["saved_path"]} == {
        f"/home/gem/user-data/saved_artifacts/{filename}",
        f"/home/gem/user-data/saved_artifacts/{renamed_filename}",
    }

    first_download = await test_client.get(first_payload["saved_artifact_url"], headers=headers)
    second_download = await test_client.get(second_payload["saved_artifact_url"], headers=headers)
    assert {first_download.content, second_download.content} == {b"first\n", b"second\n"}


async def test_save_thread_artifact_to_workspace_rejects_invalid_paths(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id = await _create_thread_for_user(test_client, headers)

    invalid_response = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}/artifacts/save",
        json={"path": "/home/gem/user-data/not-allowed/demo.txt"},
        headers=headers,
    )
    assert invalid_response.status_code == 404, invalid_response.text

    directory = await test_client.post(
        "/api/viewer/filesystem/directory",
        json={"thread_id": thread_id, "parent_path": "/", "name": "nested-dir"},
        headers=headers,
    )
    assert directory.status_code == 200, directory.text
    child_path = await _upload_project_file(
        test_client,
        headers,
        thread_id,
        "child.txt",
        b"child",
        parent_path=directory.json()["entry"]["path"],
        artifact_path=True,
    )
    directory_path = str(PurePosixPath(child_path).parent)
    directory_response = await test_client.post(
        f"/api/v1/agents/sessions/{thread_id}/artifacts/save",
        json={"path": directory_path},
        headers=headers,
    )
    assert directory_response.status_code == 400, directory_response.text


async def test_standard_user_restores_visible_function_items_without_internal_audit(
    test_client, admin_headers, standard_user
):
    """真实 worker 生成工具过程；普通用户回读可见 item，同时仍无审计权限。"""
    from test.e2e.test_agent_lifecycle_extended_e2e import (
        _agent,
        _provider,
        _delete_provider,
        _terminal_turn,
        MODEL,
        OUTPUT,
        TOOL_RESULT,
    )
    from test.e2e.e2e_helpers import archive_public_thread, delete_agent

    headers = standard_user["headers"]
    uid = str((await test_client.get("/api/auth/me", headers=headers)).json()["uid"])
    provider = await _provider(test_client, admin_headers)
    slug = await _agent(test_client, admin_headers, uid)
    thread_id = turn_id = None
    try:
        created = await test_client.post(
            "/api/v1/agents/sessions",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "agent_id": slug,
                "agent": {"model": MODEL},
                "tool_approval_mode": "always_trust",
                "title": make_test_session_title("standard-visible-items"),
                "input": [{"role": "user", "content": [{"type": "input_text", "text": OUTPUT}]}],
            },
        )
        assert created.status_code == 201, created.text
        thread_id, turn_id = created.json()["id"], created.json()["yuxi"]["receipt"]["turn_id"]
        assert (await _terminal_turn(test_client, headers, thread_id, turn_id))["status"] == "completed"
        history = await test_client.get(
            f"/api/v1/agents/sessions/{thread_id}/items?order=asc&limit=100", headers=headers
        )
        assert history.status_code == 200, history.text
        items = history.json()["data"]
        call = next(item for item in items if item["type"] == "function_call")
        output = next(item for item in items if item["type"] == "function_call_output")
        assert call["name"] == "present_artifacts" and call["arguments"] == {"filepaths": []}
        assert call["status"] == output["status"] == "completed" and call["call_id"] == output["call_id"]
        assert TOOL_RESULT in output["output"]
        audits = await test_client.get(f"/api/v1/agents/sessions/{thread_id}/audits", headers=headers)
        assert audits.status_code == 403
        assert all(
            field not in history.text
            for field in ("system_prompt", "checkpoint", "manifest_fingerprint", "source_model_operation_id")
        )
    finally:
        if thread_id:
            await archive_public_thread(test_client, headers, thread_id, turn_id=turn_id)
        await delete_agent(test_client, admin_headers, slug)
        await _delete_provider(test_client, admin_headers, provider)
