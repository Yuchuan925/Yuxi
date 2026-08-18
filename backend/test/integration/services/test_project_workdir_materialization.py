from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.storage_migration as storage_migration
import yuxi.services.project_workdir_materialization_service as svc
from yuxi.repositories.project_workdir_repository import FILE_STORAGE_MATERIALIZATION_ID
from yuxi.storage.postgres.models_business import (
    AgentRun,
    Base,
    ConfigOption,
    Conversation,
    FileStorageMaterialization,
    Message,
    ProjectWorkdir,
    ToolCall,
)
from yuxi.storage.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _ScopedMinioClient:
    """把迁移测试隔离到独立真实 bucket。"""

    def __init__(self, delegate, bucket_name: str):
        self._delegate = delegate
        self.KB_BUCKETS = {"documents": bucket_name}

    def __getattr__(self, name):
        return getattr(self._delegate, name)


async def test_real_postgres_detects_legacy_cutover_before_schema_initialization(monkeypatch):
    """首次安装、旧 schema 与已 active 部署必须由迁移前 PostgreSQL 事实区分。"""
    schema_name = f"pytest_storage_gate_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with session_factory() as db:
            yield db

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        monkeypatch.setattr(
            storage_migration,
            "pg_manager",
            SimpleNamespace(get_async_session_context=session_context),
        )

        assert await storage_migration._legacy_cutover_pending_before_schema_init() is False
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)"))
        assert await storage_migration._legacy_cutover_pending_before_schema_init() is True
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE file_storage_materializations "
                    "(id VARCHAR(64) PRIMARY KEY, phase VARCHAR(32) NOT NULL)"
                )
            )
            await connection.execute(
                text("INSERT INTO file_storage_materializations (id, phase) VALUES (:id, 'active')"),
                {"id": FILE_STORAGE_MATERIALIZATION_ID},
            )
        assert await storage_migration._legacy_cutover_pending_before_schema_init() is False
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


async def test_real_postgres_storage_migration_converges_runs_and_imports_base_toml(
    monkeypatch,
    tmp_path,
):
    """停机 proof 之后，旧 Run 与管理员配置必须在迁移 Owner 中形成持久事实。"""
    schema_name = f"pytest_storage_state_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with session_factory() as db:
            yield db

    legacy_root = tmp_path / "legacy"
    (legacy_root / "config").mkdir(parents=True)
    (legacy_root / "config/base.toml").write_text(
        'default_model = "legacy:model"\nenable_content_guard = true\n',
        encoding="utf-8",
    )
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add(
                AgentRun(
                    id="legacy-pending-run",
                    conversation_thread_id="legacy-thread",
                    runtime_scope_id="legacy-thread",
                    agent_slug="main",
                    uid="user-1",
                    status="pending",
                    request_id="legacy-pending-request",
                    run_type="chat",
                    input_payload={},
                )
            )
            await db.commit()

        monkeypatch.setattr(
            storage_migration,
            "pg_manager",
            SimpleNamespace(get_async_session_context=session_context),
        )
        monkeypatch.setattr(storage_migration, "get_legacy_storage_dir", lambda: legacy_root)

        await storage_migration._converge_database_state(fail_nonterminal_runs=False)

        async with session_factory() as db:
            run = await db.get(AgentRun, "legacy-pending-run")
            config = await db.scalar(select(ConfigOption).where(ConfigOption.key == "system_options"))
            assert run.status == "pending"
            assert config.value["default_model"] == "legacy:model"
            assert config.value["enable_content_guard"] is True

        await storage_migration._converge_database_state(fail_nonterminal_runs=True)

        async with session_factory() as db:
            run = await db.get(AgentRun, "legacy-pending-run")
            config = await db.scalar(select(ConfigOption).where(ConfigOption.key == "system_options"))
            assert run.status == "failed"
            assert run.error_type == "storage_migration"
            assert run.runtime_cleanup_pending is False
            assert config.value["default_model"] == "legacy:model"
            assert config.value["enable_content_guard"] is True
            assert config.params["migration_version"] == 1
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


async def test_real_postgres_epoch_materializes_legacy_files_and_activates_atomically(monkeypatch, tmp_path):
    schema_name = f"pytest_workdir_materialize_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    raw_minio = get_minio_client()
    bucket_name = f"pytest-workdir-{uuid.uuid4().hex}"
    minio = _ScopedMinioClient(raw_minio, bucket_name)
    revision_id = uuid.uuid4().hex
    object_name = f"threads/legacy-thread/outputs/revisions/{revision_id}/report.txt"
    orphan_object_name = "threads/orphan-thread/attachments/file-2/original/orphan.txt"
    tmp_sentinel = "tmp/chat_attachments/user-1/file-3/original/tmp.txt"
    knowledge_sentinel = "knowledgebases/kb-1/document.txt"
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text("ALTER TABLE conversations ADD COLUMN current_output_revision_id VARCHAR(64)")
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE thread_output_revisions (
                        id VARCHAR(64) PRIMARY KEY,
                        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        thread_id VARCHAR(64) NOT NULL,
                        uid VARCHAR(64) NOT NULL,
                        run_id VARCHAR(64),
                        base_revision_id VARCHAR(64),
                        status VARCHAR(32) NOT NULL,
                        files JSONB NOT NULL DEFAULT '[]'::jsonb,
                        error_message TEXT,
                        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                        published_at TIMESTAMP WITHOUT TIME ZONE,
                        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        thread_id = "legacy-thread"
        workdir_id = "workdir-1"
        legacy_root = tmp_path / "threads" / thread_id / "user-data"
        (legacy_root / "uploads").mkdir(parents=True)
        (legacy_root / "outputs").mkdir(parents=True)
        (legacy_root / "uploads/input.txt").write_bytes(b"input")
        await minio.aupload_file(bucket_name, object_name, b"report")
        await minio.aupload_file(bucket_name, orphan_object_name, b"orphan")
        await minio.aupload_file(bucket_name, tmp_sentinel, b"tmp")
        await minio.aupload_file(bucket_name, knowledge_sentinel, b"knowledge")
        monkeypatch.setattr(svc, "get_minio_client", lambda: minio)

        async with session_factory() as db:
            db.add(
                ProjectWorkdir(
                    id=workdir_id,
                    uid="user-1",
                    storage_key=f"projects/{workdir_id}",
                    materialization_status="pending",
                )
            )
            await db.flush()
            conversation = Conversation(
                thread_id=thread_id,
                uid="user-1",
                agent_id="main",
                status="active",
                workdir_id=workdir_id,
                extra_metadata={
                    "attachments": [
                        {
                            "file_id": "legacy-file",
                            "file_name": "input.txt",
                            "status": "uploaded",
                            "path": "/home/gem/user-data/uploads/input.txt",
                            "artifact_url": (
                                "/api/chat/thread/legacy-thread/artifacts/home/gem/user-data/uploads/input.txt"
                            ),
                            "original_path": "/home/gem/user-data/uploads/input.txt",
                            "original_artifact_url": (
                                "/api/chat/thread/legacy-thread/artifacts/home/gem/user-data/uploads/input.txt"
                            ),
                        }
                    ]
                },
            )
            db.add(conversation)
            await db.flush()
            await db.execute(
                text("UPDATE conversations SET current_output_revision_id = :revision_id WHERE id = :conversation_id"),
                {"revision_id": revision_id, "conversation_id": conversation.id},
            )
            assistant_message = Message(
                conversation_id=conversation.id,
                role="assistant",
                content="artifact",
            )
            db.add(assistant_message)
            await db.flush()
            db.add(
                ToolCall(
                    message_id=assistant_message.id,
                    tool_name="present_artifacts",
                    tool_input={"filepaths": ["/home/gem/user-data/outputs/report.txt"]},
                    tool_output="ok",
                    status="success",
                )
            )
            await db.execute(
                text(
                    """
                    INSERT INTO thread_output_revisions (
                        id, conversation_id, thread_id, uid, status, files
                    ) VALUES (
                        :id, :conversation_id, :thread_id, :uid, 'published', CAST(:files AS jsonb)
                    )
                    """
                ),
                {
                    "id": revision_id,
                    "conversation_id": conversation.id,
                    "thread_id": thread_id,
                    "uid": "user-1",
                    "files": json.dumps(
                        [
                            {
                                "path": "/home/gem/user-data/outputs/report.txt",
                                "bucket_name": bucket_name,
                                "object_name": object_name,
                                "size": 6,
                                "sha256": hashlib.sha256(b"report").hexdigest(),
                                "content_type": "text/plain",
                            }
                        ]
                    ),
                },
            )
            db.add(FileStorageMaterialization(id=FILE_STORAGE_MATERIALIZATION_ID, phase="pending"))
            await db.commit()

        @asynccontextmanager
        async def session_context():
            async with session_factory() as db:
                yield db

        monkeypatch.setattr(
            svc,
            "pg_manager",
            SimpleNamespace(async_engine=engine, get_async_session_context=session_context),
        )
        monkeypatch.setattr(svc, "get_legacy_storage_dir", lambda: tmp_path)
        monkeypatch.setattr(svc, "project_workdir_host_dir", lambda value: tmp_path / "projects" / value)

        original_materialize = svc.materialize_inventory_epoch

        async def fail_after_materializing(*, epoch_id, inventories):
            await original_materialize(epoch_id=epoch_id, inventories=inventories)
            raise RuntimeError("activation interrupted")

        monkeypatch.setattr(svc, "materialize_inventory_epoch", fail_after_materializing)
        with pytest.raises(RuntimeError, match="activation interrupted"):
            await svc.ensure_project_workdir_materialized()

        async with session_factory() as db:
            failed_workdir = await db.get(ProjectWorkdir, workdir_id)
            failed_control = await db.get(FileStorageMaterialization, FILE_STORAGE_MATERIALIZATION_ID)
            failed_epoch_id = failed_control.epoch_id
            assert failed_control.phase == "error"
            assert failed_workdir.materialization_status != "ready"

        async with session_factory() as db:
            await db.execute(
                text(
                    """
                    UPDATE conversations
                    SET current_output_revision_id = 'missing-revision'
                    WHERE thread_id = :thread_id
                    """
                ),
                {"thread_id": thread_id},
            )
            await db.commit()
        monkeypatch.setattr(svc, "materialize_inventory_epoch", original_materialize)
        with pytest.raises(ValueError, match="current outputs revision 不可用"):
            await svc.ensure_project_workdir_materialized()
        async with session_factory() as db:
            await db.execute(
                text("UPDATE conversations SET current_output_revision_id = :revision_id WHERE thread_id = :thread_id"),
                {"revision_id": revision_id, "thread_id": thread_id},
            )
            await db.commit()

        invalid_files = json.dumps(
            [
                {
                    "path": "/home/gem/user-data/outputs/report.txt",
                    "bucket_name": bucket_name,
                    "object_name": f"threads/other-thread/outputs/revisions/{revision_id}/report.txt",
                    "size": 6,
                    "sha256": hashlib.sha256(b"report").hexdigest(),
                }
            ]
        )
        async with session_factory() as db:
            await db.execute(
                text("UPDATE thread_output_revisions SET files = CAST(:files AS jsonb) WHERE id = :revision_id"),
                {"files": invalid_files, "revision_id": revision_id},
            )
            await db.commit()
        with pytest.raises(ValueError, match="对象作用域无效"):
            await svc.ensure_project_workdir_materialized()
        async with session_factory() as db:
            await db.execute(
                text("UPDATE thread_output_revisions SET files = CAST(:files AS jsonb) WHERE id = :revision_id"),
                {
                    "files": json.dumps(
                        [
                            {
                                "path": "/home/gem/user-data/outputs/report.txt",
                                "bucket_name": bucket_name,
                                "object_name": object_name,
                                "size": 6,
                                "sha256": hashlib.sha256(b"report").hexdigest(),
                                "content_type": "text/plain",
                            }
                        ]
                    ),
                    "revision_id": revision_id,
                },
            )
            await db.commit()

        (legacy_root / "uploads/input.txt").write_bytes(b"input-after-retry")

        class CleanupFailureClient:
            KB_BUCKETS = minio.KB_BUCKETS

            async def adownload_response(self, bucket, object_key):
                return await minio.adownload_response(bucket, object_key)

            async def alist_object_names(self, bucket, prefix):
                return await minio.alist_object_names(bucket, prefix)

            async def adelete_file(self, _bucket, _object):
                raise svc.StorageError("cleanup unavailable")

        monkeypatch.setattr(svc, "get_minio_client", CleanupFailureClient)
        with pytest.raises(svc.StorageError, match="cleanup unavailable"):
            await svc.ensure_project_workdir_materialized()

        async with session_factory() as db:
            control = await db.get(FileStorageMaterialization, FILE_STORAGE_MATERIALIZATION_ID)
            assert control.phase == "active"
            assert await db.scalar(text("SELECT to_regclass('thread_output_revisions') IS NOT NULL")) is True

        monkeypatch.setattr(svc, "get_minio_client", lambda: minio)
        await svc.ensure_project_workdir_materialized()

        final = tmp_path / "projects" / workdir_id
        assert (final / "uploads/input.txt").read_bytes() == b"input-after-retry"
        assert (final / "outputs/report.txt").read_bytes() == b"report"
        async with session_factory() as db:
            workdir = await db.get(ProjectWorkdir, workdir_id)
            control = await db.get(FileStorageMaterialization, FILE_STORAGE_MATERIALIZATION_ID)
            assert control.phase == "active"
            assert control.activated_at is not None
            assert control.epoch_id != failed_epoch_id
            assert workdir.materialization_status == "ready"
            assert workdir.materialization_epoch_id == control.epoch_id
            assert workdir.source_fingerprint
            conversation = await db.scalar(select(Conversation).where(Conversation.thread_id == thread_id))
            attachment = conversation.extra_metadata["attachments"][0]
            assert attachment["path"] == "/home/gem/projects/project-workdir-1/uploads/input.txt"
            assert attachment["original_path"] == attachment["path"]
            assert attachment["artifact_url"].endswith("/home/gem/projects/project-workdir-1/uploads/input.txt")
            tool_call = await db.scalar(select(ToolCall).where(ToolCall.tool_name == "present_artifacts"))
            assert tool_call.tool_input == {"filepaths": ["/home/gem/projects/project-workdir-1/outputs/report.txt"]}
            count = await db.scalar(select(text("count(*)")).select_from(ProjectWorkdir))
            assert count == 1
            schema_state = (
                await db.execute(
                    text(
                        """
                        SELECT
                            to_regclass('thread_output_revisions') IS NULL,
                            NOT EXISTS (
                                SELECT 1 FROM pg_attribute
                                WHERE attrelid = to_regclass('conversations')
                                  AND attname = 'current_output_revision_id'
                                  AND NOT attisdropped
                            )
                        """
                    )
                )
            ).one()
            assert tuple(schema_state) == (True, True)
        assert await minio.astat_file(bucket_name, object_name) is None
        assert await minio.astat_file(bucket_name, orphan_object_name) is None
        assert await minio.astat_file(bucket_name, tmp_sentinel) is not None
        assert await minio.astat_file(bucket_name, knowledge_sentinel) is not None
        await svc.ensure_project_workdir_materialized()
    finally:
        await raw_minio.adelete_bucket(bucket_name)
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


async def test_concurrent_startup_holds_one_dedicated_advisory_lock_until_activation(monkeypatch, tmp_path):
    """API/worker 并发启动时只能有一个物化 Owner，且完成后不能泄漏 session lock。"""

    schema_name = f"pytest_workdir_lock_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    first_task = None
    second_task = None
    release_materialization = asyncio.Event()
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            db.add(FileStorageMaterialization(id=FILE_STORAGE_MATERIALIZATION_ID, phase="pending"))
            await db.commit()

        @asynccontextmanager
        async def session_context():
            async with session_factory() as db:
                yield db

        monkeypatch.setattr(
            svc,
            "pg_manager",
            SimpleNamespace(async_engine=engine, get_async_session_context=session_context),
        )
        monkeypatch.setattr(svc, "get_legacy_storage_dir", lambda: tmp_path)
        monkeypatch.setattr(svc, "project_workdir_host_dir", lambda value: tmp_path / "projects" / value)

        entered_materialization = asyncio.Event()
        calls = 0

        async def blocking_materialization(*, epoch_id, inventories):
            nonlocal calls
            del epoch_id, inventories
            calls += 1
            entered_materialization.set()
            await release_materialization.wait()

        monkeypatch.setattr(svc, "materialize_inventory_epoch", blocking_materialization)
        first_task = asyncio.create_task(svc.ensure_project_workdir_materialized())
        await asyncio.wait_for(entered_materialization.wait(), timeout=5)
        second_task = asyncio.create_task(svc.ensure_project_workdir_materialized())

        async with admin_engine.connect() as connection:
            lock_key = int(
                await connection.scalar(
                    text("SELECT hashtext(:key)"),
                    {"key": svc.MATERIALIZATION_LOCK_KEY},
                )
            )
        class_id = (lock_key >> 32) & 0xFFFFFFFF
        object_id = lock_key & 0xFFFFFFFF
        observed = None
        for _attempt in range(50):
            async with admin_engine.connect() as connection:
                observed = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FILTER (WHERE granted), "
                            "count(*) FILTER (WHERE NOT granted) FROM pg_locks "
                            "WHERE locktype = 'advisory' AND classid = :class_id AND objid = :object_id"
                        ),
                        {"class_id": class_id, "object_id": object_id},
                    )
                ).one()
            if tuple(observed) == (1, 1):
                break
            await asyncio.sleep(0.1)
        assert tuple(observed) == (1, 1)
        assert second_task.done() is False

        release_materialization.set()
        await asyncio.gather(first_task, second_task)
        assert calls == 1

        async with admin_engine.connect() as connection:
            remaining = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :class_id AND objid = :object_id"
                ),
                {"class_id": class_id, "object_id": object_id},
            )
        assert remaining == 0
    finally:
        release_materialization.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first_task, second_task) if task is not None), return_exceptions=True)
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


async def test_active_upgrade_removes_legacy_attachment_objects_and_storage_metadata(monkeypatch, tmp_path):
    """已部署 4R-B 的 active 数据库升级时直接完成旧附件对象清理。"""
    schema_name = f"pytest_workdir_cleanup_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    raw_minio = get_minio_client()
    bucket_name = f"pytest-workdir-{uuid.uuid4().hex}"
    minio = _ScopedMinioClient(raw_minio, bucket_name)
    thread_id = f"legacy-attachment-{uuid.uuid4().hex}"
    object_name = f"threads/{thread_id}/attachments/file-1/original/report.txt"
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        await minio.aupload_file(bucket_name, object_name, b"legacy")
        monkeypatch.setattr(svc, "get_minio_client", lambda: minio)
        async with session_factory() as db:
            db.add(
                ProjectWorkdir(
                    id="workdir-active",
                    uid="user-1",
                    storage_key="projects/workdir-active",
                    materialization_status="ready",
                )
            )
            await db.flush()
            db.add(
                Conversation(
                    thread_id=thread_id,
                    uid="user-1",
                    agent_id="main",
                    status="active",
                    workdir_id="workdir-active",
                    extra_metadata={
                        "attachments": [
                            {
                                "file_id": "file-1",
                                "file_name": "report.txt",
                                "path": "/home/gem/projects/project-workdir-active/uploads/file-1_report.txt",
                                "original_path": (
                                    "/home/gem/projects/project-workdir-active/uploads/file-1_report.txt"
                                ),
                                "bucket_name": bucket_name,
                                "original_object_name": object_name,
                                "minio_url": f"minio://{bucket_name}/{object_name}",
                            }
                        ]
                    },
                )
            )
            db.add(
                FileStorageMaterialization(
                    id=FILE_STORAGE_MATERIALIZATION_ID,
                    phase="active",
                )
            )
            await db.commit()

        @asynccontextmanager
        async def session_context():
            async with session_factory() as db:
                yield db

        monkeypatch.setattr(
            svc,
            "pg_manager",
            SimpleNamespace(async_engine=engine, get_async_session_context=session_context),
        )
        await svc.ensure_project_workdir_materialized()

        assert await minio.astat_file(bucket_name, object_name) is None
        async with session_factory() as db:
            conversation = await db.scalar(select(Conversation).where(Conversation.thread_id == thread_id))
            attachment = conversation.extra_metadata["attachments"][0]
            assert "bucket_name" not in attachment
            assert "original_object_name" not in attachment
            assert "minio_url" not in attachment
            assert attachment["path"].startswith("/home/gem/projects/")
    finally:
        await raw_minio.adelete_bucket(bucket_name)
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()
