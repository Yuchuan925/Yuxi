"""真实 worker 验证消息附件归属、共享文件和图片 wire 内容。"""

import asyncio
import base64
import json
import uuid
from io import BytesIO

import asyncpg
import httpx
import pytest
from PIL import Image

from test.e2e.e2e_helpers import archive_public_thread, delete_agent, postgres_dsn
from test.e2e.test_agent_lifecycle_e2e import OUTPUT, _agent, _message, _provider, _turn, output_text
from test.live_api_cleanup import make_test_session_title
from yuxi.infrastructure.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(240)]


async def test_draft_initial_send_queue_cancel_and_model_attachment_isolation(e2e_client, e2e_headers):
    """独立模型 oracle 拒绝未来附件或 JPEG 包装，最终文件与结果从持久边界回读。"""
    await _provider(e2e_client, e2e_headers)
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    slug = await _agent(e2e_client, e2e_headers, uid)
    files = []
    session_id = turn_id = queued_id = second_session = None
    gate = str(uuid.uuid4())
    conn = await asyncpg.connect(postgres_dsn())
    async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
        try:

            async def upload(name, content):
                """上传未关联会话的原文件。"""
                response = await e2e_client.post(
                    "/api/v1/agents/files", headers=e2e_headers, files={"file": (name, content, "text/plain")}
                )
                assert response.status_code == 201, response.text
                file = response.json()
                files.append(file)
                return file

            first = await upload("first.txt", b"first input bytes")
            assert await conn.fetchval("SELECT count(*) FROM sessions WHERE agent_id=$1", slug) == 0
            png = BytesIO()
            Image.new("RGB", (16, 12), "red").save(png, format="PNG")
            image_url = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
            initial = _message(f"{OUTPUT} DETERMINISTIC_PNG_INPUT DETERMINISTIC_BLOCK_BEFORE_RESPONSE:{gate}")
            initial["content"].append({"type": "input_image", "image_url": image_url})
            body = {
                "agent_id": slug,
                "title": make_test_session_title("draft-send"),
                "input": [initial],
                "attachment_file_ids": [first["id"]],
            }
            headers = {**e2e_headers, "Idempotency-Key": str(uuid.uuid4())}
            created = await e2e_client.post("/api/v1/agents/sessions", headers=headers, json=body)
            assert created.status_code == 201, created.text
            session = created.json()
            session_id = session["id"]
            receipt = session["yuxi"]["receipt"]
            turn_id = receipt["turn_id"]
            assert turn_id and receipt["run_id"]
            repeated = await e2e_client.post("/api/v1/agents/sessions", headers=headers, json=body)
            assert repeated.status_code == 201, repeated.text
            assert repeated.json()["yuxi"]["receipt"] == receipt
            async with asyncio.timeout(30):
                while not (await replay.get("/blocking-started", params={"token": gate})).json()["started"]:
                    await asyncio.sleep(0.1)

            future = await upload("future.txt", b"future queued bytes")
            queued = await e2e_client.post(
                f"/api/v1/agents/sessions/{session_id}/events",
                headers={**e2e_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "events": [
                        {
                            "type": "agent.session.input.message",
                            "input": [_message(OUTPUT)],
                            "yuxi": {"mode": "follow_up", "attachment_file_ids": [future["id"]]},
                        }
                    ]
                },
            )
            assert queued.status_code == 202, queued.text
            queued_id = queued.json()["input_id"]
            assert queued.json()["turn_id"] is None
            steer = await e2e_client.post(
                f"/api/v1/agents/sessions/{session_id}/events",
                headers={**e2e_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "events": [
                        {
                            "type": "agent.session.input.message",
                            "input": [_message(f"{OUTPUT} DETERMINISTIC_ATTACHMENT_ABSENT:future.txt")],
                            "yuxi": {"mode": "steer"},
                        }
                    ]
                },
            )
            assert steer.status_code == 202, steer.text
            cancelled = await e2e_client.post(
                f"/api/v1/agents/sessions/{session_id}/events",
                headers={**e2e_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={"events": [{"type": "yuxi.session.input.cancel_input", "input_id": queued_id}]},
            )
            assert cancelled.status_code == 202, cancelled.text
            await replay.get("/release-blocking", params={"token": gate})
            result = await _turn(e2e_client, e2e_headers, session_id, turn_id)
            assert result["status"] == "completed", result
            assert output_text(result["yuxi"]["output"]) == OUTPUT
            refs_response = await e2e_client.get(
                f"/api/v1/agents/sessions/{session_id}/attachments", headers=e2e_headers
            )
            assert refs_response.status_code == 200, refs_response.text
            refs = {record["file_id"]: record for record in refs_response.json()["attachments"]}
            assert set(refs) == {first["id"], future["id"]}
            for file, expected in ((first, b"first input bytes"), (future, b"future queued bytes")):
                content = await e2e_client.get(refs[file["id"]]["artifact_url"], headers=e2e_headers)
                assert content.status_code == 200 and content.content == expected
            assert await conn.fetchval("SELECT status FROM agent_inputs WHERE id=$1", queued_id) == "cancelled"
            assert await conn.fetchval("SELECT count(*) FROM agent_runs WHERE input_id=$1", queued_id) == 0
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_input_receipts WHERE idempotency_key=$1", headers["Idempotency-Key"]
                )
                == 1
            )
            payload = json.loads(
                await conn.fetchval("SELECT input_payload FROM agent_inputs WHERE id=$1", receipt["input_id"])
            )
            assert not {"attachments_ready", "attachment_drafts", "attachment_error"} & payload.keys()
            rows = await conn.fetch(
                "SELECT status,object_name,parsed_source,input_id,receipt_id FROM agent_attachments "
                "WHERE id=ANY($1::varchar[])",
                [first["id"], future["id"]],
            )
            assert len(rows) == 2 and all(row["status"] == "ready" for row in rows)
            assert all(row["object_name"] is None and row["parsed_source"] is None for row in rows)
            assert {row["input_id"] for row in rows} == {receipt["input_id"], queued_id}
            items = await e2e_client.get(
                f"/api/v1/agents/sessions/{session_id}/items", headers=e2e_headers, params={"order": "asc"}
            )
            assert items.status_code == 200, items.text
            user_item = next(
                item
                for item in items.json()["data"]
                if any(part.get("type") == "input_image" for part in item.get("content", []))
            )
            assert user_item["content"][1]["image_url"] == image_url

            project_id = await conn.fetchval("SELECT project_id FROM sessions WHERE thread_id=$1", session_id)
            sibling = await e2e_client.post(
                "/api/v1/agents/sessions",
                headers={**e2e_headers, "Idempotency-Key": str(uuid.uuid4())},
                json={"agent_id": slug, "project_id": project_id, "title": make_test_session_title("shared-files")},
            )
            assert sibling.status_code == 201, sibling.text
            second_session = sibling.json()["id"]
            shared = await e2e_client.get(
                refs[first["id"]]["artifact_url"].replace(session_id, second_session), headers=e2e_headers
            )
            assert shared.status_code == 200 and shared.content == b"first input bytes"
        finally:
            await replay.get("/release-blocking", params={"token": gate})
            if session_id:
                if queued_id:
                    await e2e_client.post(
                        f"/api/v1/agents/sessions/{session_id}/events",
                        headers={**e2e_headers, "Idempotency-Key": str(uuid.uuid4())},
                        json={"events": [{"type": "yuxi.session.input.cancel_input", "input_id": queued_id}]},
                    )
                await archive_public_thread(e2e_client, e2e_headers, session_id, turn_id=turn_id)
            if second_session:
                await archive_public_thread(e2e_client, e2e_headers, second_session)
            await conn.close()
            await delete_agent(e2e_client, e2e_headers, slug)
            minio = get_minio_client()
            for file in files:
                await minio.adelete_objects_by_prefix(
                    minio.KB_BUCKETS["documents"], f"tmp/chat_attachments/{uid}/{file['id']}/"
                )
