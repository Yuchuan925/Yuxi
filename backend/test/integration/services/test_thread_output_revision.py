"""真实 PostgreSQL 上的 outputs revision 条件发布测试。"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.backends.sandbox import (
    ProvisionerSandboxBackend,
    get_sandbox_provider,
    sandbox_outputs_dir,
)
from yuxi.agents.toolkits.buildin.tools import _normalize_presented_artifact_path
from yuxi.repositories.thread_output_repository import (
    OutputRevisionConflictError,
    ThreadOutputRepository,
    merge_output_manifests,
)
from yuxi.services.thread_output_service import (
    discard_unreferenced_output_checkpoint,
    get_current_output_snapshot,
    hydrate_legacy_thread_outputs_to_sandbox,
    hydrate_thread_outputs_to_sandbox,
    publish_staged_outputs,
    stage_thread_outputs,
)
from yuxi.storage.minio import get_minio_client
from yuxi.storage.minio.client import StorageError
from yuxi.storage.postgres.manager import THREAD_OUTPUT_SCHEMA_STATEMENTS, pg_manager
from yuxi.storage.postgres.models_business import Conversation, ThreadOutputRevision

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _file(path: str, digest: str) -> dict:
    return {"path": path, "size": 1, "sha256": digest * 64, "content_type": "text/plain"}


async def test_output_revision_schema_is_idempotent_and_stale_base_cannot_publish():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        for _ in range(2):
            for statement in THREAD_OUTPUT_SCHEMA_STATEMENTS:
                await connection.execute(text(statement))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    thread_id = f"pytest-output-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"

    try:
        async with session_factory() as db:
            conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
            db.add(conversation)
            await db.flush()
            repository = ThreadOutputRepository(db)
            first = await repository.create_staging(
                revision_id=uuid.uuid4().hex,
                conversation=conversation,
                run_id=None,
                base_revision_id=None,
            )
            await repository.set_files(first.id, [_file("/home/gem/user-data/outputs/base.txt", "a")])
            await repository.publish(first.id)
            await db.commit()
            first_id = first.id
            conversation_id = conversation.id

        async with session_factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            repository = ThreadOutputRepository(db)
            next_revision = await repository.create_staging(
                revision_id=uuid.uuid4().hex,
                conversation=conversation,
                run_id=None,
                base_revision_id=first_id,
            )
            stale_revision = await repository.create_staging(
                revision_id=uuid.uuid4().hex,
                conversation=conversation,
                run_id=None,
                base_revision_id=first_id,
            )
            await repository.set_files(next_revision.id, [_file("/home/gem/user-data/outputs/base.txt", "b")])
            await repository.set_files(stale_revision.id, [_file("/home/gem/user-data/outputs/base.txt", "c")])
            await db.commit()
            next_id = next_revision.id
            stale_id = stale_revision.id

        async def publish(revision_id: str) -> str:
            async with session_factory() as db:
                try:
                    await ThreadOutputRepository(db).publish(revision_id)
                    await db.commit()
                    return "published"
                except OutputRevisionConflictError:
                    await db.rollback()
                    return "conflict"

        results = await asyncio.gather(publish(next_id), publish(stale_id))
        assert sorted(results) == ["conflict", "published"]

        async with session_factory() as db:
            current = await db.scalar(select(Conversation).where(Conversation.id == conversation_id))
            winner_id = current.current_output_revision_id
            loser_id = stale_id if winner_id == next_id else next_id
            loser = await db.get(ThreadOutputRevision, loser_id)
            assert winner_id in {next_id, stale_id}
            assert loser.status == "staging"

            winner = await db.get(ThreadOutputRevision, winner_id)
            winner_files = list(winner.files)
            child = await ThreadOutputRepository(db).create_staging(
                revision_id=uuid.uuid4().hex,
                conversation=current,
                run_id=None,
                base_revision_id=winner_id,
            )
            parent = await ThreadOutputRepository(db).create_staging(
                revision_id=uuid.uuid4().hex,
                conversation=current,
                run_id=None,
                base_revision_id=winner_id,
            )
            child_file = _file("/home/gem/user-data/outputs/child.txt", "d")
            await ThreadOutputRepository(db).set_files(child.id, [*winner_files, child_file])
            await ThreadOutputRepository(db).set_files(parent.id, winner_files)
            await db.commit()

        async with session_factory() as parent_db:
            # 模拟父 Run 长 session：先缓存旧指针，随后子 Run 在独立事务推进 current。
            cached_parent_conversation = await parent_db.get(Conversation, conversation_id)
            assert cached_parent_conversation.current_output_revision_id == winner_id

            async with session_factory() as child_db:
                await ThreadOutputRepository(child_db).publish(child.id)
                await child_db.commit()

            merged_parent = await ThreadOutputRepository(parent_db).publish(parent.id)
            await parent_db.commit()
            assert merged_parent.files == [*winner_files, child_file]
    finally:
        async with session_factory() as db:
            conversation_ids = list(
                (await db.scalars(select(Conversation.id).where(Conversation.thread_id == thread_id))).all()
            )
            if conversation_ids:
                await db.execute(
                    delete(ThreadOutputRevision).where(ThreadOutputRevision.conversation_id.in_(conversation_ids))
                )
                await db.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
            await db.commit()
        await engine.dispose()


async def test_sandbox_output_stages_to_minio_publishes_and_rehydrates(monkeypatch):
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def local_session_context():
        async with session_factory() as db:
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", local_session_context)
    thread_id = f"pytest-output-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    virtual_path = "/home/gem/user-data/outputs/report.txt"
    content = b"persistent output\n"
    conversation_id: int | None = None
    descriptors: list[dict] = []

    try:
        async with session_factory() as db:
            conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
            db.add(conversation)
            await db.commit()
            conversation_id = conversation.id

        backend = ProvisionerSandboxBackend(thread_id=thread_id, uid=uid)
        write_results = await asyncio.to_thread(backend.upload_files, [(virtual_path, content)])
        assert write_results[0].error is None

        limited_target = f"/tmp/yuxi-output-limit-{uuid.uuid4().hex}"
        with pytest.raises(ValueError, match="snapshot failed"):
            await asyncio.to_thread(
                backend.download_output_file_to_path,
                virtual_path,
                limited_target,
                len(content) - 1,
            )
        assert not os.path.exists(limited_target)

        revision_id = await stage_thread_outputs(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            conversation_id=conversation_id,
            run_id=None,
            base_revision_id=None,
        )
        async with session_factory() as db:
            await publish_staged_outputs(db=db, revision_id=revision_id)
            await db.commit()
            revision = await db.get(ThreadOutputRevision, revision_id)
            conversation = await db.get(Conversation, conversation_id)
            descriptors = list(revision.files)
            assert revision.status == "published"
            assert conversation.current_output_revision_id == revision_id

        assert len(descriptors) == 1
        descriptor = descriptors[0]
        assert descriptor["path"] == virtual_path
        assert descriptor["size"] == len(content)
        stored = await get_minio_client().adownload_file(descriptor["bucket_name"], descriptor["object_name"])
        assert stored == content

        await asyncio.to_thread(backend.clear_scope_files, "outputs")
        await hydrate_thread_outputs_to_sandbox(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            files=descriptors,
        )
        read_result = await asyncio.to_thread(backend.read, virtual_path)
        assert read_result.error is None
        assert read_result.file_data == {"content": content.decode().rstrip(), "encoding": "utf-8"}
    finally:
        for descriptor in descriptors:
            await get_minio_client().adelete_file(descriptor["bucket_name"], descriptor["object_name"])
        try:
            get_sandbox_provider().release(thread_id, uid=uid)
        except Exception:
            pass
        if conversation_id is not None:
            async with session_factory() as db:
                await db.execute(
                    delete(ThreadOutputRevision).where(ThreadOutputRevision.conversation_id == conversation_id)
                )
                await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
                await db.commit()
        await engine.dispose()


async def test_legacy_host_outputs_replay_into_first_published_revision(monkeypatch):
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def local_session_context():
        async with session_factory() as db:
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", local_session_context)
    thread_id = f"pytest-legacy-output-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    virtual_path = "/home/gem/user-data/outputs/legacy/report.txt"
    content = "legacy output survives first publication\n"
    legacy_root = sandbox_outputs_dir(thread_id)
    conversation_id: int | None = None
    descriptors: list[dict] = []

    try:
        (legacy_root / "legacy").mkdir(parents=True, exist_ok=True)
        (legacy_root / "legacy" / "report.txt").write_text(content, encoding="utf-8")
        async with session_factory() as db:
            conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
            db.add(conversation)
            await db.commit()
            conversation_id = conversation.id
            revision_id, files = await get_current_output_snapshot(conversation=conversation, db=db)
            assert revision_id is None
            assert files == []

        await hydrate_legacy_thread_outputs_to_sandbox(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            legacy_root=legacy_root,
        )
        backend = ProvisionerSandboxBackend(thread_id=thread_id, uid=uid)
        replayed = await asyncio.to_thread(backend.read, virtual_path)
        assert replayed.error is None
        assert replayed.file_data == {"content": content.rstrip(), "encoding": "utf-8"}

        staged_revision_id = await stage_thread_outputs(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            conversation_id=conversation_id,
            run_id=None,
            base_revision_id=None,
        )
        async with session_factory() as db:
            await publish_staged_outputs(db=db, revision_id=staged_revision_id)
            await db.commit()
            conversation = await db.get(Conversation, conversation_id)
            revision_id, descriptors = await get_current_output_snapshot(conversation=conversation, db=db)
            assert revision_id == staged_revision_id
            assert [item["path"] for item in descriptors] == [virtual_path]

        await asyncio.to_thread(backend.clear_scope_files, "outputs")
        await hydrate_thread_outputs_to_sandbox(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            files=descriptors,
        )
        restored = await asyncio.to_thread(backend.read, virtual_path)
        assert restored.error is None
        assert restored.file_data == {"content": content.rstrip(), "encoding": "utf-8"}
    finally:
        shutil.rmtree(legacy_root, ignore_errors=True)
        for descriptor in descriptors:
            await get_minio_client().adelete_file(descriptor["bucket_name"], descriptor["object_name"])
        try:
            get_sandbox_provider().release(thread_id, uid=uid)
        except Exception:
            pass
        if conversation_id is not None:
            async with session_factory() as db:
                await db.execute(
                    delete(ThreadOutputRevision).where(ThreadOutputRevision.conversation_id == conversation_id)
                )
                await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
                await db.commit()
        await engine.dispose()


async def test_rejected_subagent_checkpoint_cleanup_removes_revision_and_objects(monkeypatch):
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def local_session_context():
        async with session_factory() as db:
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", local_session_context)
    thread_id = f"pytest-rejected-checkpoint-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    revision_id = uuid.uuid4().hex
    minio_client = get_minio_client()
    bucket_name = minio_client.KB_BUCKETS["documents"]
    object_name = f"threads/{thread_id}/outputs/revisions/{revision_id}/orphan.txt"
    conversation_id: int | None = None

    try:
        await minio_client.aupload_file(bucket_name, object_name, b"orphan")
        async with session_factory() as db:
            conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
            db.add(conversation)
            await db.flush()
            conversation_id = conversation.id
            repository = ThreadOutputRepository(db)
            await repository.create_staging(
                revision_id=revision_id,
                conversation=conversation,
                run_id=None,
                base_revision_id=None,
            )
            await repository.set_files(
                revision_id,
                [
                    {
                        "path": "/home/gem/user-data/outputs/orphan.txt",
                        "bucket_name": bucket_name,
                        "object_name": object_name,
                        "size": 6,
                        "sha256": "0" * 64,
                    }
                ],
            )
            await repository.checkpoint(revision_id)
            await db.commit()

        await discard_unreferenced_output_checkpoint(revision_id)

        async with session_factory() as db:
            assert await db.get(ThreadOutputRevision, revision_id) is None
        with pytest.raises(StorageError, match="对象.*不存在"):
            await minio_client.adownload_file(bucket_name, object_name)
    finally:
        await minio_client.adelete_file(bucket_name, object_name)
        if conversation_id is not None:
            async with session_factory() as db:
                await db.execute(
                    delete(ThreadOutputRevision).where(ThreadOutputRevision.conversation_id == conversation_id)
                )
                await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
                await db.commit()
        await engine.dispose()


async def test_parent_child_private_checkpoint_round_trip_reaches_parent_artifact_consumer(monkeypatch):
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def local_session_context():
        async with session_factory() as db:
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", local_session_context)
    thread_id = f"pytest-parent-child-output-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    parent_instance = f"parent-{uuid.uuid4()}"
    child_instance = f"child-{uuid.uuid4()}"
    input_path = "/home/gem/user-data/outputs/parent-input.txt"
    child_path = "/home/gem/user-data/outputs/child-report.txt"
    content = b"parent private checkpoint\n"
    conversation_id: int | None = None
    object_descriptors: dict[tuple[str, str], dict] = {}

    try:
        async with session_factory() as db:
            conversation = Conversation(thread_id=thread_id, uid=uid, agent_id="main", status="active")
            db.add(conversation)
            await db.commit()
            conversation_id = conversation.id

        parent_backend = ProvisionerSandboxBackend(
            thread_id=thread_id,
            uid=uid,
            sandbox_instance_id=parent_instance,
        )
        assert (await asyncio.to_thread(parent_backend.upload_files, [(input_path, content)]))[0].error is None
        parent_checkpoint_id = await stage_thread_outputs(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            conversation_id=conversation_id,
            run_id=None,
            base_revision_id=None,
            sandbox_instance_id=parent_instance,
        )
        async with session_factory() as db:
            parent_checkpoint = await ThreadOutputRepository(db).checkpoint(parent_checkpoint_id)
            await db.commit()
            parent_checkpoint_files = list(parent_checkpoint.files or [])

        child_backend = ProvisionerSandboxBackend(
            thread_id=thread_id,
            uid=uid,
            sandbox_instance_id=child_instance,
        )
        await asyncio.to_thread(child_backend.ensure_available)
        await hydrate_thread_outputs_to_sandbox(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            files=parent_checkpoint_files,
            sandbox_instance_id=child_instance,
            create_if_missing=False,
        )
        child_read = await asyncio.to_thread(child_backend.read, input_path)
        assert child_read.error is None
        assert child_read.file_data == {"content": content.decode().rstrip(), "encoding": "utf-8"}
        assert (await asyncio.to_thread(child_backend.upload_files, [(child_path, content)]))[0].error is None

        child_checkpoint_id = await stage_thread_outputs(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            conversation_id=conversation_id,
            run_id=None,
            base_revision_id=parent_checkpoint_id,
            sandbox_instance_id=child_instance,
        )
        async with session_factory() as db:
            published_projection = await ThreadOutputRepository(db).publish(child_checkpoint_id)
            await db.commit()
            child_checkpoint = await db.get(ThreadOutputRevision, child_checkpoint_id)
            assert child_checkpoint.status == "checkpoint"
            assert [item["path"] for item in published_projection.files] == [child_path]
            child_checkpoint_files = list(child_checkpoint.files or [])

        parent_local_id = await stage_thread_outputs(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            conversation_id=conversation_id,
            run_id=None,
            base_revision_id=child_checkpoint_id,
            sandbox_instance_id=parent_instance,
        )
        async with session_factory() as db:
            parent_local = await ThreadOutputRepository(db).checkpoint(parent_local_id)
            merged_files = merge_output_manifests(
                base=parent_checkpoint_files,
                staged=child_checkpoint_files,
                current=list(parent_local.files or []),
            )
            await ThreadOutputRepository(db).set_checkpoint_files(parent_local_id, merged_files)
            await db.commit()

        await hydrate_thread_outputs_to_sandbox(
            runtime_thread_id=thread_id,
            file_thread_id=thread_id,
            skills_thread_id=thread_id,
            uid=uid,
            files=merged_files,
            sandbox_instance_id=parent_instance,
            create_if_missing=False,
        )
        parent_child_read = await asyncio.to_thread(parent_backend.read, child_path)
        assert parent_child_read.error is None
        assert parent_child_read.file_data == {"content": content.decode().rstrip(), "encoding": "utf-8"}
        runtime = SimpleNamespace(
            context=SimpleNamespace(
                thread_id=thread_id,
                file_thread_id=thread_id,
                skills_thread_id=thread_id,
                uid=uid,
                sandbox_instance_id=parent_instance,
            )
        )
        assert _normalize_presented_artifact_path(child_path, runtime) == child_path
    finally:
        if conversation_id is not None:
            async with session_factory() as db:
                revisions = list(
                    (
                        await db.scalars(
                            select(ThreadOutputRevision).where(ThreadOutputRevision.conversation_id == conversation_id)
                        )
                    ).all()
                )
                for revision in revisions:
                    for descriptor in revision.files or []:
                        object_descriptors[(descriptor["bucket_name"], descriptor["object_name"])] = descriptor
                await db.execute(
                    delete(ThreadOutputRevision).where(ThreadOutputRevision.conversation_id == conversation_id)
                )
                await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
                await db.commit()
        for bucket_name, object_name in object_descriptors:
            await get_minio_client().adelete_file(bucket_name, object_name)
        for instance_id in (parent_instance, child_instance):
            try:
                get_sandbox_provider().release(thread_id, uid=uid, sandbox_instance_id=instance_id)
            except Exception:
                pass
        await engine.dispose()
