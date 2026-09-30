"""真实 HTTP 上传后回读工作区文件与 MinIO 对象。"""

from io import BytesIO
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import pytest
from PIL import Image

from yuxi.infrastructure.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_workspace_upload_preserves_bytes_and_existing_file(test_client, standard_user):
    """超过 spool 阈值的文件按流落盘，同名重传不得覆盖。"""
    headers = standard_user["headers"]
    filename = f"pytest-upload-{uuid4().hex}.bin"
    path = f"/{filename}"
    content = b"\x00\xff\x01\x02" * (1024 * 1024)
    try:
        uploaded = await test_client.post(
            "/api/workspace/upload",
            data={"parent_path": "/"},
            files={"files": (filename, content, "application/octet-stream")},
            headers=headers,
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["entries"][0]["size"] == len(content)
        collision = await test_client.post(
            "/api/workspace/upload",
            data={"parent_path": "/"},
            files={"files": (filename, b"replacement", "application/octet-stream")},
            headers=headers,
        )
        assert collision.status_code == 400, collision.text
        downloaded = await test_client.get("/api/workspace/download", params={"path": path}, headers=headers)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == content
    finally:
        removed = await test_client.delete("/api/workspace/file", params={"path": path}, headers=headers)
        assert removed.status_code in {200, 404}, removed.text


async def test_attachment_upload_preserves_metadata_and_minio_bytes(test_client, standard_user):
    """协议读取不能丢失附件文件名、类型、大小和对象字节。"""
    storage = get_minio_client()
    bucket = storage.KB_BUCKETS["documents"]
    content = b"attachment bytes\x00\xff"
    object_name = None
    try:
        response = await test_client.post(
            "/api/v1/agents/attachments/tmp",
            headers=standard_user["headers"],
            files={"file": ("source.txt", content, "text/plain")},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        object_name = payload["object_name"]
        assert payload["file_name"] == "source.txt"
        assert payload["file_type"] == "text/plain"
        assert payload["file_size"] == len(content)
        assert object_name.startswith(f"tmp/chat_attachments/{standard_user['user']['uid']}/")
        assert await storage.adownload_file(bucket, object_name) == content
    finally:
        if object_name:
            await storage.adelete_file(bucket, object_name)


@pytest.mark.parametrize(
    "endpoint,url_key", [("/api/user/upload-image", "image_url"), ("/api/auth/upload-avatar", "avatar_url")]
)
async def test_image_upload_preserves_minio_bytes_and_rejects_invalid_content(
    test_client, standard_user, endpoint, url_key
):
    """图片与头像端点消费实际图片字节，并返回可定位的存储结果。"""
    source = BytesIO()
    Image.new("RGB", (2, 2), "red").save(source, format="PNG")
    content = source.getvalue()
    storage = get_minio_client()
    object_name = None
    try:
        response = await test_client.post(
            endpoint,
            headers=standard_user["headers"],
            files={"file": ("not-an-extension.txt", content, "text/plain")},
        )
        assert response.status_code == 200, response.text
        url = response.json()[url_key]
        object_name = unquote(urlsplit(url).path.split("/public/", 1)[1])
        assert object_name.endswith(".png")
        assert await storage.adownload_file("public", object_name) == content
        if url_key == "avatar_url":
            me = await test_client.get("/api/auth/me", headers=standard_user["headers"])
            assert me.status_code == 200, me.text
            assert me.json()["avatar"] == url
        invalid = await test_client.post(
            endpoint,
            headers=standard_user["headers"],
            files={"file": ("fake.png", b"not an image", "image/png")},
        )
        assert invalid.status_code == 400, invalid.text
    finally:
        if object_name:
            await storage.adelete_file("public", object_name)
