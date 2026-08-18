"""真实 PostgreSQL 上的 Project Workdir 与 runtime scope 基础测试。"""

from __future__ import annotations

import os
import uuid
import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.manager import (
    PROJECT_WORKDIR_SCHEMA_STATEMENTS,
    RUNTIME_SCOPE_SCHEMA_STATEMENTS,
    PostgresManager,
)
from yuxi.storage.postgres.models_business import (
    AgentRun,
    Base,
    Conversation,
    ConversationStats,
    ProjectWorkdir,
    SubagentThread,
)
from yuxi.repositories.conversation_repository import ConversationRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def workdir_database():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        for _ in range(2):
            for statement in (*PROJECT_WORKDIR_SCHEMA_STATEMENTS, *RUNTIME_SCOPE_SCHEMA_STATEMENTS):
                await connection.execute(text(statement))
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_project_workdir_schema_evolution_is_idempotent(workdir_database):
    engine, _ = workdir_database
    async with engine.connect() as connection:
        conversation_column = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'conversations' AND column_name = 'workdir_id')"
            )
        )
        run_column = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'agent_runs' AND column_name = 'runtime_scope_id')"
            )
        )
        foreign_key = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.key_column_usage "
                "WHERE table_name = 'conversations' AND column_name = 'workdir_id')"
            )
        )
        status_check = await connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'ck_project_workdirs_materialization_status')"
            )
        )
        conversation_nullable = await connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'conversations' AND column_name = 'workdir_id'"
            )
        )
        run_nullable = await connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'agent_runs' AND column_name = 'runtime_scope_id'"
            )
        )

    assert conversation_column is True
    assert run_column is True
    assert foreign_key is True
    assert status_check is True
    assert conversation_nullable == "NO"
    assert run_nullable == "NO"


async def test_conversation_cannot_bind_another_users_project_workdir(workdir_database):
    _, session_factory = workdir_database
    suffix = uuid.uuid4().hex
    owner_uid = f"pytest-workdir-owner-{suffix}"
    attacker_uid = f"pytest-workdir-attacker-{suffix}"
    workdir_id = f"workdir-{suffix}"

    async with session_factory() as db:
        db.add(
            ProjectWorkdir(
                id=workdir_id,
                uid=owner_uid,
                storage_key=f"projects/{workdir_id}",
                materialization_status="pending",
            )
        )
        await db.commit()
        db.add(
            Conversation(
                thread_id=f"pytest-cross-uid-{suffix}",
                uid=attacker_uid,
                agent_id="main",
                status="active",
                workdir_id=workdir_id,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
        await db.execute(delete(ProjectWorkdir).where(ProjectWorkdir.id == workdir_id))
        await db.commit()


async def test_single_legacy_schema_pass_backfills_runtime_scope_after_thread_id():
    schema_name = f"pytest_legacy_scope_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    isolated_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        async with isolated_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("ALTER TABLE agent_runs ALTER COLUMN conversation_thread_id DROP NOT NULL"))
            await connection.execute(text("ALTER TABLE agent_runs ALTER COLUMN runtime_scope_id DROP NOT NULL"))
            await connection.execute(text("ALTER TABLE agent_runs ADD COLUMN thread_id VARCHAR(64)"))
            await connection.execute(
                text(
                    "INSERT INTO agent_runs ("
                    "id, conversation_thread_id, runtime_scope_id, agent_slug, uid, status, request_id, "
                    "source, channel, origin_metadata, run_type, input_payload, token_usage, thread_id"
                    ") VALUES ("
                    "'legacy-run', NULL, NULL, 'main', 'legacy-user', 'completed', 'legacy-request', "
                    "'chat', 'web', '{}'::jsonb, 'chat', '{}'::jsonb, '{}'::jsonb, 'legacy-thread'"
                    ")"
                )
            )

        await PostgresManager.ensure_business_schema(
            SimpleNamespace(async_engine=isolated_engine, _check_initialized=lambda: None)
        )

        async with isolated_engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT conversation_thread_id, runtime_scope_id FROM agent_runs WHERE id = 'legacy-run'")
                )
            ).one()
            legacy_column_exists = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = :schema_name AND table_name = 'agent_runs' "
                    "AND column_name = 'thread_id')"
                ),
                {"schema_name": schema_name},
            )

        assert row == ("legacy-thread", "legacy-thread")
        assert legacy_column_exists is False
    finally:
        await isolated_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


async def test_schema_rerun_does_not_create_legacy_workdir_for_bound_conversation(workdir_database):
    engine, session_factory = workdir_database
    suffix = uuid.uuid4().hex
    uid = f"pytest-bound-{suffix}"
    thread_id = f"pytest-bound-thread-{suffix}"
    conversation_id: int | None = None
    workdir_id: str | None = None
    legacy_id = f"legacy-{hashlib.md5(f'{uid}:{thread_id}'.encode(), usedforsecurity=False).hexdigest()}"

    try:
        async with session_factory() as db:
            conversation = await ConversationRepository(db).add_conversation(
                uid=uid,
                agent_id="main",
                thread_id=thread_id,
            )
            await db.commit()
            conversation_id = conversation.id
            workdir_id = conversation.workdir_id

        async with engine.begin() as connection:
            for statement in PROJECT_WORKDIR_SCHEMA_STATEMENTS:
                await connection.execute(text(statement))

        async with session_factory() as db:
            assert await db.get(ProjectWorkdir, legacy_id) is None
    finally:
        async with session_factory() as db:
            if conversation_id is not None:
                await db.execute(delete(ConversationStats).where(ConversationStats.conversation_id == conversation_id))
                await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
            if workdir_id is not None:
                await db.execute(delete(ProjectWorkdir).where(ProjectWorkdir.id == workdir_id))
            await db.commit()


async def test_schema_backfill_binds_child_to_parent_workdir_and_runtime_scope(workdir_database):
    engine, session_factory = workdir_database
    suffix = uuid.uuid4().hex
    uid = f"pytest-workdir-{suffix}"
    parent_thread = f"pytest-parent-{suffix}"
    child_thread = f"pytest-child-{suffix}"
    parent_run_id = f"pytest-parent-run-{suffix}"
    child_run_id = f"pytest-child-run-{suffix}"
    conversation_ids: list[int] = []
    workdir_ids: list[str] = []

    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE conversations ALTER COLUMN workdir_id DROP NOT NULL"))
            await connection.execute(text("ALTER TABLE agent_runs ALTER COLUMN runtime_scope_id DROP NOT NULL"))
        async with session_factory() as db:
            parent = Conversation(thread_id=parent_thread, uid=uid, agent_id="main", status="active")
            child = Conversation(thread_id=child_thread, uid=uid, agent_id="worker", status="subagent")
            db.add_all([parent, child])
            await db.flush()
            relation = SubagentThread(
                uid=uid,
                parent_conversation_id=parent.id,
                child_conversation_id=child.id,
                child_thread_id=child_thread,
                subagent_slug="worker",
                created_by_run_id=parent_run_id,
            )
            db.add(relation)
            await db.flush()
            db.add_all(
                [
                    AgentRun(
                        id=parent_run_id,
                        conversation_thread_id=parent_thread,
                        runtime_scope_id=None,
                        agent_slug="main",
                        uid=uid,
                        status="completed",
                        request_id=f"pytest-parent-request-{suffix}",
                        conversation_id=parent.id,
                        input_payload={},
                        run_type="chat",
                    ),
                    AgentRun(
                        id=child_run_id,
                        conversation_thread_id=child_thread,
                        runtime_scope_id=None,
                        agent_slug="worker",
                        uid=uid,
                        status="completed",
                        request_id=f"pytest-child-request-{suffix}",
                        conversation_id=child.id,
                        created_by_run_id=parent_run_id,
                        subagent_thread_relation_id=relation.id,
                        input_payload={},
                        run_type="subagent",
                    ),
                ]
            )
            await db.commit()
            conversation_ids = [parent.id, child.id]

        async with engine.begin() as connection:
            for statement in (*PROJECT_WORKDIR_SCHEMA_STATEMENTS, *RUNTIME_SCOPE_SCHEMA_STATEMENTS):
                await connection.execute(text(statement))

        async with session_factory() as db:
            parent = await db.get(Conversation, conversation_ids[0])
            child = await db.get(Conversation, conversation_ids[1])
            parent_run = await db.get(AgentRun, parent_run_id)
            child_run = await db.get(AgentRun, child_run_id)

            assert parent.workdir_id
            assert child.workdir_id == parent.workdir_id
            assert parent_run.runtime_scope_id == parent_thread
            assert child_run.runtime_scope_id == parent_thread
            workdir = await db.get(ProjectWorkdir, parent.workdir_id)
            assert workdir.uid == uid
            assert workdir.materialization_status == "pending"
            workdir_ids = [parent.workdir_id]
    finally:
        async with session_factory() as db:
            await db.execute(delete(AgentRun).where(AgentRun.id.in_([parent_run_id, child_run_id])))
            if conversation_ids:
                await db.execute(
                    delete(SubagentThread).where(SubagentThread.child_conversation_id == conversation_ids[1])
                )
                await db.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
            if workdir_ids:
                await db.execute(delete(ProjectWorkdir).where(ProjectWorkdir.id.in_(workdir_ids)))
            await db.commit()
