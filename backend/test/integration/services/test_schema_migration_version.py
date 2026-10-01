"""Yuxi Schema 版本事实在真实 PostgreSQL 上的集成测试。"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.infrastructure.postgres.schema import BUSINESS_SCHEMA_VERSION, KNOWLEDGE_SCHEMA_VERSION
from yuxi.migrations import main as schema_bootstrap
from yuxi.migrations.schema import (
    create_business_tables,
    create_knowledge_tables,
    create_schema_version_table,
    ensure_business_schema,
    ensure_knowledge_schema,
    record_schema_version,
)
from yuxi.infrastructure.postgres.schema import get_schema_versions, require_current_schema
from yuxi.infrastructure.postgres.manager import PostgresManager
from yuxi.modules.knowledge.models import KnowledgeBase

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """隔离 Schema 测试没有 HTTP 资源需要清理。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """隔离 Schema 测试没有 Sandbox 资源需要清理。"""
    yield


def _scoped_manager(engine) -> PostgresManager:
    """创建不触碰进程单例的隔离 manager。"""
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


async def _create_isolated_manager(prefix: str):
    """创建位于独立 PostgreSQL Schema 的 manager 与清理句柄。"""
    schema = f"{prefix}_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    return schema, admin_engine, scoped_engine, _scoped_manager(scoped_engine)


async def _drop_isolated_schema(schema: str, admin_engine, scoped_engine) -> None:
    """释放隔离 Schema 及其 engine。"""
    await scoped_engine.dispose()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await admin_engine.dispose()


async def test_schema_migration_lock_serializes_real_postgres_sessions() -> None:
    """两个 migrator 竞争同一 advisory lock 时只允许一个进入临界区。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    manager = _scoped_manager(engine)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_migrator() -> None:
        async with manager.schema_migration_lock():
            first_entered.set()
            await release_first.wait()

    async def second_migrator() -> None:
        await first_entered.wait()
        async with manager.schema_migration_lock():
            second_entered.set()

    first_task = asyncio.create_task(first_migrator())
    second_task = asyncio.create_task(second_migrator())
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        await asyncio.sleep(0.1)
        assert second_entered.is_set() is False
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)
        assert second_entered.is_set() is True
    finally:
        release_first.set()
        for task in (first_task, second_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        await engine.dispose()


async def test_fresh_business_schema_contains_input_lifecycle_without_request_table() -> None:
    """新环境只建立 Input、Receipt、Turn 和 Run 关系，重复收敛不回建 Request。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_agent_schema")
    try:
        await create_business_tables(manager)
        await ensure_business_schema(manager)
        await ensure_business_schema(manager)
        async with scoped_engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text("SELECT table_name FROM information_schema.tables WHERE table_schema = :schema"),
                        {"schema": schema},
                    )
                ).scalars()
            )
            run_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = 'agent_runs'"
                        ),
                        {"schema": schema},
                    )
                ).scalars()
            )
            input_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = 'agent_inputs'"
                        ),
                        {"schema": schema},
                    )
                ).scalars()
            )
            execution_seq_nullable = await connection.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'agent_runs' "
                    "AND column_name = 'execution_seq'"
                ),
                {"schema": schema},
            )
            execution_seq_default = await connection.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'agent_runs' "
                    "AND column_name = 'execution_seq'"
                ),
                {"schema": schema},
            )
        assert {"agent_turns", "agent_runs", "agent_inputs", "agent_input_receipts", "agent_input_messages"} <= tables
        assert "agent_run_requests" not in tables
        assert "agent_session_input_receipts" not in tables
        assert {"turn_id", "input_id", "resume_from_run_id"} <= run_columns
        assert execution_seq_nullable == "NO"
        assert execution_seq_default and "nextval" in execution_seq_default
        assert "agent_runs_execution_seq" in execution_seq_default
        assert "request_id" not in run_columns
        assert {"kind", "status", "turn_id", "consumed_run_id", "cutoff_seq", "received_seq"} <= input_columns
        assert BUSINESS_SCHEMA_VERSION == 12
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)


async def test_knowledge_timestamp_defaults_match_database_clock_in_non_utc_session() -> None:
    """带时区字段不依赖 PostgreSQL 会话时区解释无时区 UTC。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_knowledge_clock")
    try:
        await create_knowledge_tables(manager)
        session_factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        async with session_factory.begin() as session:
            await session.execute(text("SET TIME ZONE 'Asia/Shanghai'"))
            database = KnowledgeBase(kb_id="clock-kb", name="clock", kb_type="milvus")
            session.add(database)
            await session.flush()
            database_now = await session.scalar(text("SELECT clock_timestamp()"))

        async with session_factory() as verify_session:
            await verify_session.execute(text("SET TIME ZONE 'UTC'"))
            persisted_at = await verify_session.scalar(
                text("SELECT created_at FROM knowledge_bases WHERE kb_id = 'clock-kb'")
            )

        assert persisted_at.tzinfo is not None
        assert abs((persisted_at - database_now).total_seconds()) < 2
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)


async def test_unversioned_existing_yuxi_table_blocks_fresh_schema_initialization(monkeypatch) -> None:
    """未版本化的旧表不得被空库初始化误认为全新部署。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_fresh_only")
    monkeypatch.setattr(schema_bootstrap, "pg_manager", manager)
    try:
        await schema_bootstrap._require_empty_database()
        async with scoped_engine.begin() as connection:
            await connection.execute(text("CREATE TABLE conversations (id integer PRIMARY KEY)"))
        with pytest.raises(RuntimeError, match="conversations"):
            await schema_bootstrap._require_empty_database()
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)


async def test_schema_version_is_persisted_and_runtime_validation_fails_closed() -> None:
    """版本表缺失、错误和正确三种状态必须形成精确启动结论。"""
    schema, admin_engine, scoped_engine, manager = await _create_isolated_manager("pytest_schema_version")

    try:
        with pytest.raises(RuntimeError, match="business=missing"):
            await require_current_schema(manager)

        await create_schema_version_table(manager)
        await record_schema_version(manager, "business", BUSINESS_SCHEMA_VERSION + 1)
        with pytest.raises(RuntimeError, match=f"business={BUSINESS_SCHEMA_VERSION + 1}"):
            await require_current_schema(manager)

        await record_schema_version(manager, "business", BUSINESS_SCHEMA_VERSION)
        with pytest.raises(RuntimeError, match="knowledge=missing"):
            await require_current_schema(manager)

        await record_schema_version(manager, "knowledge", KNOWLEDGE_SCHEMA_VERSION)
        await require_current_schema(manager)
        assert await get_schema_versions(manager) == {
            "business": BUSINESS_SCHEMA_VERSION,
            "knowledge": KNOWLEDGE_SCHEMA_VERSION,
        }
    finally:
        await _drop_isolated_schema(schema, admin_engine, scoped_engine)
