# ruff: noqa: E402  # 入口必须先加载环境。
"""只为全新部署建立当前 PostgreSQL Schema。"""

from __future__ import annotations

import asyncio

from yuxi.bootstrap.environment import load_environment

load_environment()

from sqlalchemy import text

from yuxi.bootstrap.models import load_models
from yuxi.infrastructure.postgres.base import BusinessBase, KnowledgeBase
from yuxi.infrastructure.postgres.checkpointer import setup_langgraph_checkpointer
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.infrastructure.postgres.schema import BUSINESS_SCHEMA_VERSION, KNOWLEDGE_SCHEMA_VERSION, get_schema_versions
from yuxi.migrations.schema import (
    create_business_tables,
    create_knowledge_tables,
    create_schema_version_table,
    ensure_business_schema,
    ensure_knowledge_schema,
    record_schema_version,
    schema_migration_lock,
)
from yuxi.modules.system.options import ensure_options_in_db


async def _require_empty_database() -> None:
    """拒绝未版本化的旧库和上次初始化失败后留下的半成品。"""
    load_models()
    owned_tables = set(BusinessBase.metadata.tables) | set(KnowledgeBase.metadata.tables)
    async with pg_manager.async_engine.connect() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))
        existing = owned_tables.intersection(rows.scalars().all())
    if existing:
        raise RuntimeError(f"Fresh Yuxi schema requires an empty database; found {', '.join(sorted(existing))}")


async def main() -> None:
    """新库建表并记录版本；已有库只接受完整的当前版本。"""
    pg_manager.initialize()
    try:
        async with schema_migration_lock(pg_manager):
            versions = await get_schema_versions(pg_manager)
            current = {"business": BUSINESS_SCHEMA_VERSION, "knowledge": KNOWLEDGE_SCHEMA_VERSION}
            if versions == current:
                return
            if versions:
                raise RuntimeError(f"Unsupported Yuxi schema versions: {versions}; required {current}")

            await _require_empty_database()
            await create_business_tables(pg_manager)
            await create_knowledge_tables(pg_manager)
            await ensure_business_schema(pg_manager)
            await ensure_knowledge_schema(pg_manager)
            await setup_langgraph_checkpointer(pg_manager)
            async with pg_manager.get_async_session_context() as session:
                await ensure_options_in_db(session)
                await session.commit()
            await create_schema_version_table(pg_manager)
            await record_schema_version(pg_manager, "business", BUSINESS_SCHEMA_VERSION)
            await record_schema_version(pg_manager, "knowledge", KNOWLEDGE_SCHEMA_VERSION)
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
