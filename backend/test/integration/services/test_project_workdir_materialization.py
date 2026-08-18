from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.services.project_workdir_materialization_service as svc
from yuxi.repositories.project_workdir_repository import FILE_STORAGE_MATERIALIZATION_ID
from yuxi.storage.postgres.models_business import (
    Base,
    Conversation,
    FileStorageMaterialization,
    Message,
    ProjectWorkdir,
    ThreadOutputRevision,
    ToolCall,
)
from yuxi.storage.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_real_postgres_epoch_materializes_legacy_files_and_activates_atomically(monkeypatch, tmp_path):
    schema_name = f"pytest_workdir_materialize_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    minio = get_minio_client()
    bucket_name = minio.KB_BUCKETS["documents"]
    revision_id = uuid.uuid4().hex
    object_name = f"pytest/project-workdir-materialization/{revision_id}/report.txt"
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        thread_id = "legacy-thread"
        workdir_id = "workdir-1"
        legacy_root = tmp_path / "threads" / thread_id / "user-data"
        (legacy_root / "uploads").mkdir(parents=True)
        (legacy_root / "outputs").mkdir(parents=True)
        (legacy_root / "uploads/input.txt").write_bytes(b"input")
        await minio.aupload_file(bucket_name, object_name, b"report")

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
                current_output_revision_id=revision_id,
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
            db.add(
                ThreadOutputRevision(
                    id=revision_id,
                    conversation_id=conversation.id,
                    thread_id=thread_id,
                    uid="user-1",
                    status="published",
                    files=[
                        {
                            "path": "/home/gem/user-data/outputs/report.txt",
                            "bucket_name": bucket_name,
                            "object_name": object_name,
                            "size": 6,
                            "sha256": hashlib.sha256(b"report").hexdigest(),
                            "content_type": "text/plain",
                        }
                    ],
                )
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
        monkeypatch.setattr(svc, "get_save_dir", lambda: tmp_path)
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

        (legacy_root / "uploads/input.txt").write_bytes(b"input-after-retry")
        monkeypatch.setattr(svc, "materialize_inventory_epoch", original_materialize)
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
    finally:
        await minio.adelete_file(bucket_name, object_name)
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
        monkeypatch.setattr(svc, "get_save_dir", lambda: tmp_path)
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
