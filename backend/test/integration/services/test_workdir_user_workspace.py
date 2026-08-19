"""真实 PostgreSQL 与文件系统上的 Workdir/UserWorkspace 契约。"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.agents.backends.sandbox.paths import ensure_bound_user_workdir
from yuxi.services.legacy_workdir_importer import (
    cleanup_legacy_workdir_sources,
    import_legacy_workdirs,
    read_legacy_bindings,
    rewrite_legacy_workdir_paths,
    verify_workdir_bindings,
)
from yuxi.storage.postgres.manager import (
    LEGACY_WORKDIR_CUTOVER_STATEMENTS,
    LEGACY_WORKDIR_SCHEMA_DROP_STATEMENTS,
    WORKDIR_PATH_SCHEMA_STATEMENTS,
)
from yuxi.storage.postgres.models_business import Base, Conversation

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_conversation_default_and_explicit_workdirs_use_user_workspace(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "user-data"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            first = await ConversationRepository(db).add_conversation(
                uid="user-1",
                agent_id="main",
                thread_id="thread-1",
            )
            await db.commit()
            assert first.workdir_path.startswith("projects/")
            ensure_bound_user_workdir("user-1", first.workdir_path)
            first_directory = (
                tmp_path / "user-data" / "shared" / "user-1" / "workspace" / first.workdir_path
            )
            assert first_directory.is_dir()

            second = await ConversationRepository(db).add_conversation(
                uid="user-1",
                agent_id="main",
                thread_id="thread-2",
                workdir_path=first.workdir_path,
            )
            await db.commit()
            assert second.workdir_path == first.workdir_path

            with pytest.raises(ValueError):
                await ConversationRepository(db).add_conversation(
                    uid="user-1",
                    agent_id="main",
                    thread_id="thread-escape",
                    workdir_path="../outside",
                )
    finally:
        await engine.dispose()


async def test_legacy_ready_workdir_cutover_is_atomic_and_rewrites_paths(monkeypatch, tmp_path: Path):
    schema_name = f"pytest_workdir_cutover_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    legacy_projects = tmp_path / "legacy-projects"
    source = legacy_projects / "workdir-1"
    source.mkdir(parents=True)
    (source / "report.md").write_text("legacy-report", encoding="utf-8")
    monkeypatch.setenv("YUXI_LEGACY_PROJECTS_DIR", str(legacy_projects))
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "user-data"))

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("ALTER TABLE conversations ALTER COLUMN workdir_path DROP NOT NULL"))
            await connection.execute(text("ALTER TABLE conversations ADD COLUMN workdir_id VARCHAR(64)"))
            await connection.execute(
                text(
                    "CREATE TABLE project_workdirs ("
                    "id VARCHAR(64) PRIMARY KEY, uid VARCHAR(255) NOT NULL, "
                    "materialization_status VARCHAR(16) NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE file_storage_materializations ("
                    "id VARCHAR(64) PRIMARY KEY, phase VARCHAR(16) NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO project_workdirs VALUES "
                    "('workdir-1', 'user-1', 'ready')"
                )
            )
            await connection.execute(
                text("INSERT INTO file_storage_materializations VALUES ('project-workdir-v1', 'active')")
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(thread_id, uid, agent_id, status, is_pinned, workdir_path, extra_metadata) "
                    "VALUES ('thread-1', 'user-1', 'main', 'active', false, NULL, "
                    "'{\"attachments\":[{\"file_id\":\"f1\",\"path\":"
                    "\"/home/gem/projects/project-workdir-1/report.md\"}]}'::jsonb)"
                )
            )
            await connection.execute(
                text("UPDATE conversations SET workdir_id = 'workdir-1' WHERE thread_id = 'thread-1'")
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(DBAPIError, match="storage-migrator cutover"):
            async with engine.begin() as connection:
                for statement in WORKDIR_PATH_SCHEMA_STATEMENTS:
                    await connection.execute(text(statement))
        async with factory() as db:
            workdirs, conversations = await read_legacy_bindings(db)
        import_legacy_workdirs(workdirs, conversations)
        async with engine.begin() as connection:
            for statement in LEGACY_WORKDIR_CUTOVER_STATEMENTS:
                await connection.execute(text(statement))
        async with factory() as db:
            retry_workdirs, retry_conversations = await read_legacy_bindings(db)
        assert retry_workdirs == workdirs
        assert retry_conversations == conversations
        import_legacy_workdirs(retry_workdirs, retry_conversations)
        cleanup_legacy_workdir_sources(retry_workdirs, retry_conversations)
        async with factory() as db:
            await rewrite_legacy_workdir_paths(db)
            await verify_workdir_bindings(db)
            for statement in LEGACY_WORKDIR_SCHEMA_DROP_STATEMENTS:
                await db.execute(text(statement))
            await db.commit()
            conversation = await db.scalar(select(Conversation).where(Conversation.thread_id == "thread-1"))
            old_table = await db.scalar(text("SELECT to_regclass('project_workdirs')"))
        async with engine.begin() as connection:
            for statement in WORKDIR_PATH_SCHEMA_STATEMENTS:
                await connection.execute(text(statement))

        target = tmp_path / "user-data/shared/user-1/workspace/projects/workdir-1/report.md"
        assert target.read_text(encoding="utf-8") == "legacy-report"
        assert conversation.workdir_path == "projects/workdir-1"
        assert old_table is None
        assert conversation.extra_metadata["attachments"][0]["path"] == (
            "/home/gem/user-data/projects/workdir-1/report.md"
        )
        assert not source.exists()
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


async def test_legacy_thread_only_layout_is_imported_and_requires_no_project_table(monkeypatch, tmp_path: Path):
    schema_name = f"pytest_thread_cutover_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    legacy_storage = tmp_path / "legacy"
    source = legacy_storage / "threads/thread-early/user-data/uploads"
    source.mkdir(parents=True)
    (source / "input.txt").write_text("early-layout", encoding="utf-8")
    monkeypatch.setattr(
        "yuxi.services.legacy_workdir_importer.get_legacy_storage_dir",
        lambda: legacy_storage,
    )
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "user-data"))

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("ALTER TABLE conversations DROP COLUMN workdir_path"))
            await connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(thread_id, uid, agent_id, status, is_pinned, extra_metadata) "
                    "VALUES ('thread-early', 'user-early', 'main', 'active', false, '{}')"
                )
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            workdirs, conversations = await read_legacy_bindings(db)
        expected_id = "legacy-" + hashlib.md5(b"user-early:thread-early").hexdigest()
        assert len(workdirs) == 1
        assert (workdirs[0].workdir_id, workdirs[0].uid) == (expected_id, "user-early")
        assert conversations[0].workdir_id == expected_id

        import_legacy_workdirs(workdirs, conversations)
        async with engine.begin() as connection:
            for statement in LEGACY_WORKDIR_CUTOVER_STATEMENTS:
                await connection.execute(text(statement))
        async with factory() as db:
            await verify_workdir_bindings(db)

        target = tmp_path / f"user-data/shared/user-early/workspace/projects/{expected_id}/uploads/input.txt"
        assert target.read_text(encoding="utf-8") == "early-layout"
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()
