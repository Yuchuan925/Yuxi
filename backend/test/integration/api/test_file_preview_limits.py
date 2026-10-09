"""真实对象读取与 HTTP 预览的原文件预算。"""

from hashlib import sha256
from uuid import uuid4

import pytest

from yuxi.infrastructure.minio import ObjectSizeLimitError, get_minio_client
from yuxi.shared.files import MAX_FILE_PREVIEW_BYTES

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_minio_download_enforces_actual_bytes_without_changing_object():
    """真实 MinIO 对象在上限内完整返回，超限拒绝且保留源字节。"""
    storage = get_minio_client()
    bucket = storage.KB_BUCKETS["documents"]
    object_name = f"pytest-preview-limits/{uuid4().hex}/source.bin"
    content = b"123456789"
    try:
        await storage.aupload_file(bucket, object_name, content)
        with pytest.raises(ObjectSizeLimitError):
            await storage.adownload_file(bucket, object_name, max_bytes=8)
        assert await storage.adownload_file(bucket, object_name, max_bytes=9) == content
        assert await storage.adownload_file(bucket, object_name) == content
    finally:
        await storage.adelete_file(bucket, object_name)


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".pptx"])
async def test_knowledge_preview_rejects_large_object_despite_understated_metadata(test_client, admin_headers, knowledge_database, suffix):
    """持久元数据低估大小时，HTTP 仍拒绝实际超限源文件且不生成缓存。"""
    storage = get_minio_client()
    bucket = storage.KB_BUCKETS["documents"]
    kb_id = knowledge_database["kb_id"]
    object_name = f"{kb_id}/upload/pytest-preview-{uuid4().hex}{suffix}"
    source = f"minio://{bucket}/{object_name}"
    content = b"x" * (MAX_FILE_PREVIEW_BYTES + 1)
    file_id = None
    try:
        await storage.aupload_file(bucket, object_name, content)
        added = await test_client.post(
            f"/api/knowledge/databases/{kb_id}/documents/add",
            json={
                "items": [source],
                "params": {"content_hashes": {source: sha256(content).hexdigest()}, "file_sizes": {source: 0}},
            },
            headers=admin_headers,
        )
        assert added.status_code == 200, added.text
        added_payload = added.json()
        assert added_payload["status"] == "success", added_payload
        file_id = added_payload["items"][0]["file_id"]

        basic = await test_client.get(f"/api/knowledge/databases/{kb_id}/documents/{file_id}/basic", headers=admin_headers)
        assert basic.status_code == 200, basic.text
        assert basic.json()["size"] == 0
        assert await storage.astat_file(bucket, object_name) == len(content)

        preview = await test_client.get("/api/workspace/knowledge/file", params={"kb_id": kb_id, "file_id": file_id}, headers=admin_headers)
        assert preview.status_code == 200, preview.text
        payload = preview.json()
        assert payload["content"] is None
        assert payload["preview_type"] == "unsupported"
        assert payload["supported"] is False
        assert payload["limit"] == MAX_FILE_PREVIEW_BYTES
        assert payload["message"] == "文件过大，当前仅支持 30 MB 以内的文件预览"
        assert await storage.astat_file(storage.KB_BUCKETS["parsed"], f"{kb_id}/preview/{file_id}.pdf") is None
        assert await storage.adownload_file(bucket, object_name) == content
    finally:
        if file_id:
            removed = await test_client.delete(f"/api/knowledge/databases/{kb_id}/documents/{file_id}", headers=admin_headers)
            assert removed.status_code in {200, 404}, removed.text
        await storage.adelete_file(bucket, object_name)
