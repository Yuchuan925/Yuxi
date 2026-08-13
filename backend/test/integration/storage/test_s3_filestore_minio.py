from __future__ import annotations

import os
import tempfile
import uuid

import aioboto3
import pytest
from fastapi import UploadFile
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
from botocore.config import Config
from botocore.exceptions import ClientError

from yuxi.agents.toolkits.buildin import tools as buildin_tools
from yuxi.agents.toolkits.kbs import tools as kb_tools
from yuxi.agents.skills import service as skill_service
from yuxi.storage.filestore import S3FileStore, thread_output_key, thread_upload_key
from yuxi.storage.postgres.models_business import Skill, User
from yuxi.services import thread_files_service
from yuxi.services import workspace_service
from yuxi.services import mention_search_service


class _Conversation:
    uid = "user-1"


async def _fake_require_user_conversation(_repo, _thread_id: str, _current_uid: str):
    return _Conversation()


class _RedisLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Redis:
    def __init__(self):
        self.values = {}

    def lock(self, *_args, **_kwargs):
        return _RedisLock()

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **_kwargs):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_s3_filestore_round_trip_against_minio():
    endpoint = os.getenv("FILESTORE_S3_ENDPOINT") or os.getenv("MINIO_URI") or "http://minio:9000"
    access_key = os.getenv("FILESTORE_S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("FILESTORE_S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY") or "minioadmin"
    bucket = f"yuxi-filestore-test-{uuid.uuid4().hex}"
    region = os.getenv("FILESTORE_S3_REGION") or "us-east-1"
    client_options = {
        "endpoint_url": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
        "config": Config(s3={"addressing_style": "path"}),
    }
    store = S3FileStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=region,
    )
    prefix = f"pytest/{uuid.uuid4()}"
    key = f"{prefix}/nested/file.txt"

    try:
        session = aioboto3.Session()
        async with session.client("s3", **client_options) as client:
            with pytest.raises(ClientError):
                await client.head_bucket(Bucket=bucket)

        written = await store.put(key, b"minio-round-trip", content_type="text/plain")
        loaded = await store.read(key)
        streamed = b"".join([chunk async for chunk in store.stream(key, chunk_size=4)])
        listed = await store.list(f"{prefix}/")

        assert written.key == key
        assert loaded.data == b"minio-round-trip"
        assert streamed == b"minio-round-trip"
        assert [(item.key, item.size, item.content_type) for item in listed] == [(key, 16, "text/plain")]
    finally:
        await store.delete_prefix(f"{prefix}/")
        session = aioboto3.Session()
        async with session.client("s3", **client_options) as client:
            await client.delete_bucket(Bucket=bucket)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_thread_upload_output_main_flow_against_minio(monkeypatch):
    endpoint = os.getenv("FILESTORE_S3_ENDPOINT") or os.getenv("MINIO_URI") or "http://minio:9000"
    access_key = os.getenv("FILESTORE_S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("FILESTORE_S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY") or "minioadmin"
    bucket = f"yuxi-thread-files-test-{uuid.uuid4().hex}"
    region = os.getenv("FILESTORE_S3_REGION") or "us-east-1"
    client_options = {
        "endpoint_url": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
        "config": Config(s3={"addressing_style": "path"}),
    }
    store = S3FileStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=region,
    )
    thread_id = f"thread-{uuid.uuid4().hex}"
    monkeypatch.setattr(thread_files_service, "get_file_store", lambda: store)
    monkeypatch.setattr(thread_files_service, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(thread_files_service, "ConversationRepository", lambda _db: object())

    try:
        await store.put(f"threads/{thread_id}/uploads/source.txt", b"source", content_type="text/plain")
        await store.put(f"threads/{thread_id}/outputs/reports/.keep", b"")
        await store.put(f"threads/{thread_id}/outputs/reports/result.md", b"# result", content_type="text/markdown")

        root_entries = await thread_files_service.list_thread_object_entries(
            thread_id,
            "/home/gem/user-data/outputs",
        )
        artifact = await thread_files_service.resolve_thread_artifact_view(
            thread_id=thread_id,
            current_uid="user-1",
            db=None,
            path="/home/gem/user-data/outputs/reports/result.md",
        )

        assert [(entry["name"], entry["is_dir"]) for entry in root_entries] == [("reports", True)]
        assert artifact.name == "result.md"
        assert artifact.media_type == "text/markdown"
        assert b"".join([chunk async for chunk in artifact.stream]) == b"# result"
    finally:
        await store.delete_prefix(f"threads/{thread_id}/")
        session = aioboto3.Session()
        async with session.client("s3", **client_options) as client:
            await client.delete_bucket(Bucket=bucket)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mention_index_reads_workspace_and_thread_objects_from_minio(monkeypatch):
    endpoint = os.getenv("FILESTORE_S3_ENDPOINT") or os.getenv("MINIO_URI") or "http://minio:9000"
    access_key = os.getenv("FILESTORE_S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("FILESTORE_S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY") or "minioadmin"
    bucket = f"yuxi-mention-test-{uuid.uuid4().hex}"
    region = os.getenv("FILESTORE_S3_REGION") or "us-east-1"
    client_options = {
        "endpoint_url": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
        "config": Config(s3={"addressing_style": "path"}),
    }
    store = S3FileStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=region,
    )
    redis = _Redis()

    async def get_redis():
        return redis

    monkeypatch.setattr(mention_search_service, "get_redis_client", get_redis)
    monkeypatch.setattr(workspace_service, "get_file_store", lambda: store)
    monkeypatch.setattr(thread_files_service, "get_file_store", lambda: store)

    try:
        await store.put("users/user-1/workspace/docs/guide.md", b"guide", content_type="text/markdown")
        await store.put("threads/thread-1/uploads/input.csv", b"input", content_type="text/csv")
        await store.put("threads/thread-1/outputs/reports/result.md", b"result", content_type="text/markdown")

        results = await mention_search_service.search_mention_files_in_index("thread-1", "user-1", "result")

        assert results == [
            {
                "name": "result.md",
                "path": "/home/gem/user-data/outputs/reports/result.md",
                "is_dir": False,
                "source": "thread",
            }
        ]
        assert f"{mention_search_service.THREAD_CACHE_PREFIX}thread-1" in redis.values
        assert f"{mention_search_service.WORKSPACE_CACHE_PREFIX}user-1" in redis.values
    finally:
        await store.delete_prefix("users/user-1/workspace/")
        await store.delete_prefix("threads/thread-1/")
        session = aioboto3.Session()
        async with session.client("s3", **client_options) as client:
            await client.delete_bucket(Bucket=bucket)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_file_tools_round_trip_against_minio(monkeypatch):
    endpoint = os.getenv("FILESTORE_S3_ENDPOINT") or os.getenv("MINIO_URI") or "http://minio:9000"
    access_key = os.getenv("FILESTORE_S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("FILESTORE_S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY") or "minioadmin"
    bucket = f"yuxi-agent-tools-test-{uuid.uuid4().hex}"
    region = os.getenv("FILESTORE_S3_REGION") or "us-east-1"
    client_options = {
        "endpoint_url": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
        "config": Config(s3={"addressing_style": "path"}),
    }
    store = S3FileStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=region,
    )
    thread_id = f"thread-{uuid.uuid4().hex}"
    runtime = SimpleNamespace(context=SimpleNamespace(file_thread_id=thread_id, uid="user-1"))
    monkeypatch.setattr(buildin_tools, "get_file_store", lambda: store)
    monkeypatch.setattr(kb_tools, "get_file_store", lambda: store)

    async def fake_parse_document(source: str, params=None, db=None):
        del db
        assert Path(source).read_bytes() == b"minio source"
        assert params == {"ocr_engine": "disable"}
        return "# parsed from MinIO"

    async def fake_visible_kbs(runtime):
        del runtime
        return [{"kb_id": "kb-1", "name": "KB", "kb_type": "milvus"}]

    async def fake_download(kb_id: str, file_id: str, variant: str):
        assert (kb_id, file_id, variant) == ("kb-1", "file-1", "original")
        return {"filename": "source.bin", "content": b"kb original", "media_type": "application/octet-stream"}

    monkeypatch.setattr("yuxi.services.ocr_service.parse_document", fake_parse_document)
    monkeypatch.setattr(kb_tools, "_resolve_visible_knowledge_bases_for_query", fake_visible_kbs)
    monkeypatch.setattr(kb_tools, "_get_knowledge_base", lambda: SimpleNamespace(get_file_download=fake_download))

    try:
        await store.put(thread_upload_key(thread_id, "source.pdf"), b"minio source")
        ocr_result = await buildin_tools.ocr_parse_file.coroutine(
            file_path="/home/gem/user-data/uploads/source.pdf",
            ocr_engine="disable",
            runtime=runtime,
        )
        artifact_result = await buildin_tools.present_artifacts.coroutine(
            filepaths=[ocr_result["parsed_path"]],
            runtime=runtime,
            tool_call_id="call-minio",
        )
        download_result = await kb_tools.download_kb_file.coroutine(
            kb_id="kb-1",
            file_id="file-1",
            runtime=runtime,
        )

        assert (await store.read(thread_output_key(thread_id, "ocr/source.md"))).data == b"# parsed from MinIO"
        assert artifact_result.update["artifacts"] == ["/home/gem/user-data/outputs/ocr/source.md"]
        assert download_result["virtual_path"] == "/home/gem/user-data/outputs/source.bin"
        assert (await store.read(thread_output_key(thread_id, "source.bin"))).data == b"kb original"
    finally:
        await store.delete_prefix(f"threads/{thread_id}/")
        session = aioboto3.Session()
        async with session.client("s3", **client_options) as client:
            await client.delete_bucket(Bucket=bucket)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workspace_crud_and_thread_artifact_save_against_minio(monkeypatch):
    endpoint = os.getenv("FILESTORE_S3_ENDPOINT") or os.getenv("MINIO_URI") or "http://minio:9000"
    access_key = os.getenv("FILESTORE_S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("FILESTORE_S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY") or "minioadmin"
    bucket = f"yuxi-workspace-test-{uuid.uuid4().hex}"
    region = os.getenv("FILESTORE_S3_REGION") or "us-east-1"
    client_options = {
        "endpoint_url": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
        "config": Config(s3={"addressing_style": "path"}),
    }
    store = S3FileStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=region,
    )
    user = SimpleNamespace(uid="user-1")
    thread_id = f"thread-{uuid.uuid4().hex}"
    monkeypatch.setattr(workspace_service, "get_file_store", lambda: store)
    monkeypatch.setattr(thread_files_service, "get_file_store", lambda: store)
    monkeypatch.setattr(thread_files_service, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(thread_files_service, "ConversationRepository", lambda _db: object())

    try:
        await workspace_service.create_workspace_directory(parent_path="/", name="docs", current_user=user)
        await workspace_service.upload_workspace_files(
            parent_path="/docs",
            files=[UploadFile(filename="note.md", file=BytesIO(b"# note"))],
            current_user=user,
        )
        await workspace_service.write_workspace_file_content(
            path="/docs/note.md", content="# updated", current_user=user
        )
        tree = await workspace_service.list_workspace_tree(path="/", recursive=True, current_user=user)
        await store.put(f"threads/{thread_id}/outputs/report.md", b"# report", content_type="text/markdown")
        saved = await thread_files_service.save_thread_artifact_to_workspace_view(
            thread_id=thread_id,
            current_uid="user-1",
            db=None,
            path="/home/gem/user-data/outputs/report.md",
        )

        assert {entry["path"] for entry in tree["entries"]} >= {"/docs/", "/docs/note.md"}
        assert (await store.read("users/user-1/workspace/docs/note.md")).data == b"# updated"
        assert saved["saved_path"] == "/home/gem/user-data/workspace/saved_artifacts/report.md"
        assert (await store.read("users/user-1/workspace/saved_artifacts/report.md")).data == b"# report"

        await workspace_service.delete_workspace_path(path="/docs", current_user=user)
        assert await store.list("users/user-1/workspace/docs/") == []
    finally:
        await store.delete_prefix("users/user-1/workspace/")
        await store.delete_prefix(f"threads/{thread_id}/")
        session = aioboto3.Session()
        async with session.client("s3", **client_options) as client:
            await client.delete_bucket(Bucket=bucket)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skill_shared_thread_and_personal_flows_against_minio(monkeypatch):
    endpoint = os.getenv("FILESTORE_S3_ENDPOINT") or os.getenv("MINIO_URI") or "http://minio:9000"
    access_key = os.getenv("FILESTORE_S3_ACCESS_KEY") or os.getenv("MINIO_ACCESS_KEY") or "minioadmin"
    secret_key = os.getenv("FILESTORE_S3_SECRET_KEY") or os.getenv("MINIO_SECRET_KEY") or "minioadmin"
    bucket = f"yuxi-skills-test-{uuid.uuid4().hex}"
    region = os.getenv("FILESTORE_S3_REGION") or "us-east-1"
    client_options = {
        "endpoint_url": endpoint,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
        "config": Config(s3={"addressing_style": "path"}),
    }
    store = S3FileStore(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region_name=region,
    )
    redis = _Redis()
    shared_items: dict[str, Skill] = {}

    class FakeRepo:
        def __init__(self, _db):
            pass

        async def exists_slug(self, slug: str):
            return slug in shared_items

        async def create(self, **kwargs):
            item = Skill(**kwargs, updated_by=kwargs["created_by"])
            shared_items[item.slug] = item
            return item

        async def list_enabled(self):
            return list(shared_items.values())

    async def get_redis():
        return redis

    monkeypatch.setattr(skill_service, "get_file_store", lambda: store)
    monkeypatch.setattr(skill_service, "get_async_redis_client", get_redis)
    monkeypatch.setattr(skill_service, "SkillRepository", FakeRepo)

    with tempfile.TemporaryDirectory(prefix="pytest-skill-minio-") as temp_root:
        root = Path(temp_root)
        shared_source = root / "shared-demo"
        shared_source.mkdir()
        (shared_source / "SKILL.md").write_text(
            "---\nname: shared-demo\ndescription: shared demo\n---\n# Shared\n",
            encoding="utf-8",
        )
        (shared_source / "prompt.md").write_text("shared prompt", encoding="utf-8")
        personal_source = root / "personal-demo"
        personal_source.mkdir()
        (personal_source / "SKILL.md").write_text(
            "---\nname: personal-demo\ndescription: personal demo\n---\n# Personal\n",
            encoding="utf-8",
        )

        try:
            shared = await skill_service.import_skill_dir(None, source_dir=shared_source, created_by="admin")
            user = User(
                username="admin",
                uid="admin",
                password_hash="x",
                role="admin",
                department_id=1,
            )
            listed = await skill_service.list_accessible_skills(None, user)
            thread_root = await skill_service.sync_thread_readable_skills_async(
                "thread-minio",
                [shared.slug],
                {shared.slug: listed[0].source_dir},
            )

            assert shared.dir_path == "skills/shared-demo"
            assert (await store.read("skills/shared-demo/SKILL.md")).data.startswith(b"---")
            assert (thread_root / "shared-demo" / "prompt.md").read_text() == "shared prompt"
            assert (await store.read("threads/thread-minio/skills/shared-demo/prompt.md")).data == b"shared prompt"

            installed = await skill_service.install_personal_skill_dir("user-1", personal_source)
            personal = await skill_service.list_personal_skills("user-1", refresh=True)
            assert installed.slug == "personal-demo"
            assert [item.slug for item in personal.items] == ["personal-demo"]
            assert (personal.items[0].source_dir / "SKILL.md").is_file()

            deleted = await skill_service.delete_personal_skill("user-1", "personal-demo")
            assert deleted.items == []
            assert await store.list("users/user-1/workspace/agents/skills/personal-demo/") == []
        finally:
            await store.delete_prefix("skills/")
            await store.delete_prefix("threads/thread-minio/")
            await store.delete_prefix("users/user-1/")
            session = aioboto3.Session()
            async with session.client("s3", **client_options) as client:
                await client.delete_bucket(Bucket=bucket)
