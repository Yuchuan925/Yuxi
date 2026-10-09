"""真实 HTTP 验证文件草稿、私有预处理与隔离。"""

import io
import os
import uuid

import asyncpg
import pytest
from PIL import Image

from yuxi.infrastructure.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_draft_upload_does_not_create_session_and_deletes_original(test_client, standard_user, admin_headers):
    """上传无会话副作用，服务端引用守住所有用户读写入口。"""
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    storage = get_minio_client()
    bucket = storage.KB_BUCKETS["documents"]
    file_id = None
    try:
        before = await conn.fetchval("SELECT count(*) FROM sessions WHERE uid=$1", uid)
        uploaded = await test_client.post(
            "/api/v1/agents/files", headers=headers, files={"file": ("draft.txt", b"draft bytes", "text/plain")}
        )
        assert uploaded.status_code == 201, uploaded.text
        file = uploaded.json()
        file_id = file["id"]
        assert file["object"] == "file" and file["status"] == "draft"
        assert file["filename"] == "draft.txt" and file["bytes"] == 11
        assert not {"path", "object_name", "parsed_object_name", "session_id", "parse_methods"} & file.keys()
        assert await conn.fetchval("SELECT count(*) FROM sessions WHERE uid=$1", uid) == before
        record = await conn.fetchrow("SELECT * FROM agent_attachments WHERE id=$1", file_id)
        assert record["uid"] == uid and record["status"] == "draft"
        assert record["input_id"] is None and record["receipt_id"] is None
        assert await storage.adownload_file(bucket, record["object_name"]) == b"draft bytes"
        assert {item["object_name"] for item in await storage.alist_object_metadata(bucket, f"tmp/chat_attachments/{uid}/{file_id}/")} == {
            record["object_name"]
        }
        assert (await test_client.get(f"/api/v1/agents/files/{file_id}", headers=headers)).json() == file
        for method, path, kwargs in (
            ("GET", f"/api/v1/agents/files/{file_id}", {}),
            ("DELETE", f"/api/v1/agents/files/{file_id}", {}),
            ("POST", f"/api/agent/files/{file_id}/parse", {"json": {"parse_method": "disable"}}),
        ):
            response = await test_client.request(method, path, headers=admin_headers, **kwargs)
            assert response.status_code == 404, response.text
        deleted = await test_client.delete(f"/api/v1/agents/files/{file_id}", headers=headers)
        assert deleted.json() == {"id": file_id, "object": "file.deleted", "deleted": True}
        assert await storage.alist_object_metadata(bucket, f"tmp/chat_attachments/{uid}/{file_id}/") == []
    finally:
        if file_id:
            await storage.adelete_objects_by_prefix(bucket, f"tmp/chat_attachments/{uid}/{file_id}/")
        await conn.close()


@pytest.mark.parametrize("level", ["full", "agents"])
async def test_public_keys_cannot_use_private_preprocessing_or_cross_app_drafts(test_client, admin_headers, level):
    """Full Key 也不能绕过产品 JWT 边界；可信 APP 前缀隔离文件。"""
    key_ids = []
    files = []
    try:
        keys = []
        for index in range(2):
            body = {"request_id": str(uuid.uuid4()), "name": "Draft boundary", "access_level": level}
            if level == "agents":
                body["app_id"] = f"draft-app-{uuid.uuid4().hex[:8]}-{index}"
            created = await test_client.post("/api/user/apikey/", headers=admin_headers, json=body)
            assert created.status_code == 200, created.text
            key_ids.append(created.json()["api_key"]["id"])
            keys.append({"Authorization": f"Bearer {created.json()['secret']}"})
        source = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(source, format="PNG")
        uploaded = await test_client.post(
            "/api/v1/agents/files", headers=keys[0], files={"file": ("image.png", source.getvalue(), "image/png")}
        )
        assert uploaded.status_code == 201, uploaded.text
        file_id = uploaded.json()["id"]
        files.append((keys[0], file_id))
        for path, kwargs in (
            ("/api/agent/images", {"files": {"file": ("image.png", source.getvalue(), "image/png")}}),
            (f"/api/agent/files/{file_id}/parse", {"json": {"parse_method": "rapid_ocr"}}),
        ):
            denied = await test_client.post(path, headers=keys[0], **kwargs)
            assert denied.status_code == 403, denied.text
        if level == "agents":
            for headers in (keys[1], admin_headers):
                hidden = await test_client.get(f"/api/v1/agents/files/{file_id}", headers=headers)
                assert hidden.status_code == 404, hidden.text
    finally:
        for headers, file_id in files:
            response = await test_client.delete(f"/api/v1/agents/files/{file_id}", headers=headers)
            assert response.status_code in {200, 404}, response.text
        for key_id in key_ids:
            await test_client.delete(f"/api/user/apikey/{key_id}", headers=admin_headers)


async def test_public_preprocessing_and_confirm_routes_are_removed(test_client, admin_headers):
    """旧入口确实不可调用，OpenAPI 不再公开预处理协议。"""
    for path in (
        "/api/v1/agents/images",
        "/api/v1/agents/attachments/tmp",
        "/api/v1/agents/attachments/tmp/parse",
        f"/api/v1/agents/sessions/{uuid.uuid4()}/attachments/confirm",
    ):
        response = await test_client.post(path, headers=admin_headers, json={})
        assert response.status_code in {404, 405}, response.text
    paths = (await test_client.get("/openapi.json")).json()["paths"]
    assert "/api/v1/agents/files" in paths
    assert "/api/agent/images" in paths
    assert "/api/agent/files/{file_id}/parse" in paths
    assert not any("/attachments/tmp" in path or path.endswith("/attachments/confirm") for path in paths)
    assert "/api/v1/agents/images" not in paths


async def test_expired_draft_cleanup_is_bounded_and_keeps_live_drafts(test_client, admin_headers):
    """行状态决定清理范围，32 行之后的过期草稿下一次继续清理。"""
    from yuxi.modules.agents.services.attachments import _tmp_attachment_owner

    app_id = f"cleanup-{uuid.uuid4().hex}"
    issued = await test_client.post(
        "/api/user/apikey/",
        headers=admin_headers,
        json={"request_id": str(uuid.uuid4()), "name": "Draft row cleanup", "access_level": "agents", "app_id": app_id},
    )
    assert issued.status_code == 200, issued.text
    headers = {"Authorization": f"Bearer {issued.json()['secret']}"}
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    storage = get_minio_client()
    bucket = storage.KB_BUCKETS["documents"]
    ids = []
    prefix = None
    try:
        anchor = await test_client.post("/api/v1/agents/files", headers=headers, files={"file": ("anchor.txt", b"live", "text/plain")})
        assert anchor.status_code == 201, anchor.text
        ids.append(anchor.json()["id"])
        row = await conn.fetchrow("SELECT uid,app_id FROM agent_attachments WHERE id=$1", anchor.json()["id"])
        uid = row["uid"]
        assert row["app_id"] == app_id
        prefix = f"tmp/chat_attachments/{_tmp_attachment_owner(uid, app_id)}/"
        for index in range(33):
            file_id = uuid.uuid4().hex
            ids.append(file_id)
            object_name = f"{prefix}{file_id}/original.txt"
            await storage.aupload_file(bucket, object_name, b"expired", content_type="text/plain")
            await conn.execute(
                "INSERT INTO agent_attachments "
                "(id,uid,app_id,filename,mime_type,size_bytes,created_at,expires_at,status,object_name) "
                "VALUES ($1,$2,$3,'expired.txt','text/plain',7,now(),now()-interval '1 day','draft',$4)",
                file_id,
                uid,
                app_id,
                object_name,
            )
        for remaining in [1, 0]:
            uploaded = await test_client.post("/api/v1/agents/files", headers=headers, files={"file": ("live.txt", b"live", "text/plain")})
            assert uploaded.status_code == 201, uploaded.text
            ids.append(uploaded.json()["id"])
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_attachments WHERE uid=$1 AND app_id=$2 AND expires_at < now()",
                    uid,
                    app_id,
                )
                == remaining
            )
        assert await conn.fetchval("SELECT count(*) FROM agent_attachments WHERE uid=$1 AND app_id=$2 AND status='draft'", uid, app_id) == 3
        names = {item["object_name"] for item in await storage.alist_object_metadata(bucket, prefix)}
        assert len(names) == 3 and all("manifest" not in name for name in names)
    finally:
        if prefix:
            await storage.adelete_objects_by_prefix(bucket, prefix)
        if ids:
            await conn.execute("DELETE FROM agent_attachments WHERE id=ANY($1::varchar[])", ids)
        await conn.close()
        await test_client.delete(f"/api/user/apikey/{issued.json()['api_key']['id']}", headers=admin_headers)
