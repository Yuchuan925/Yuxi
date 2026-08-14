from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from yuxi.storage.postgres.manager import PostgresManager


@pytest.mark.asyncio
async def test_langgraph_setup_uses_cross_process_advisory_lock():
    """官方 checkpoint migration 必须被 PostgreSQL advisory lock 包围。"""
    manager = object.__new__(PostgresManager)
    manager._initialized = True
    manager._langgraph_checkpointer_setup = False
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    @asynccontextmanager
    async def connection():
        yield Connection()

    async def setup():
        statements.append("setup")

    saver = SimpleNamespace(setup=setup)
    manager.langgraph_pool = SimpleNamespace(connection=connection)
    manager.langgraph_checkpointer = saver

    assert await manager.setup_langgraph_checkpointer() is saver
    assert statements == [
        "SELECT pg_advisory_lock(94721802)",
        "setup",
        "SELECT pg_advisory_unlock(94721802)",
    ]
