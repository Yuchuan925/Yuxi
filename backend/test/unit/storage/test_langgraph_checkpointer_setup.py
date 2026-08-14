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
            return SimpleNamespace(fetchone=_true_row)

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


@pytest.mark.asyncio
async def test_langgraph_setup_discards_connection_when_unlock_fails():
    """无法确认 advisory lock 已释放时不能把持锁 session 放回池中。"""
    manager = object.__new__(PostgresManager)
    manager._initialized = True
    manager._langgraph_checkpointer_setup = False
    connection_closed = False

    class Connection:
        async def execute(self, statement):
            if "unlock" in statement:
                raise RuntimeError("unlock failed")
            return SimpleNamespace(fetchone=_true_row)

        async def close(self):
            nonlocal connection_closed
            connection_closed = True

    @asynccontextmanager
    async def connection():
        yield Connection()

    manager.langgraph_pool = SimpleNamespace(connection=connection)
    manager.langgraph_checkpointer = SimpleNamespace(setup=_noop)

    with pytest.raises(RuntimeError, match="unlock failed"):
        await manager.setup_langgraph_checkpointer()

    assert connection_closed is True
    assert manager._langgraph_checkpointer_setup is False


async def _true_row():
    return (True,)


async def _noop():
    return None
