import pytest

import yuxi.storage.postgres.manager as postgres_manager


def test_redact_postgres_url_hides_password():
    db_url = "postgresql+asyncpg://postgres:p%40ssword@postgres:5432/yuxi"

    redacted = postgres_manager.redact_postgres_url(db_url)

    assert redacted == "postgresql+asyncpg://postgres:***@postgres:5432/yuxi"
    assert "p%40ssword" not in redacted


def test_redact_postgres_url_does_not_return_invalid_input():
    invalid_url = "not a database url with secret-value"

    assert postgres_manager.redact_postgres_url(invalid_url) == "<invalid PostgreSQL URL>"


@pytest.mark.asyncio
async def test_setup_langgraph_checkpointer_is_idempotent(monkeypatch):
    setup_calls = 0

    class FakeCheckpointer:
        def __init__(self, pool):
            assert pool == "pool"

        async def setup(self):
            nonlocal setup_calls
            setup_calls += 1

    manager = object.__new__(postgres_manager.PostgresManager)
    manager._initialized = True
    manager.langgraph_pool = "pool"
    manager.langgraph_checkpointer = None
    manager._langgraph_checkpointer_setup = False
    monkeypatch.setattr(postgres_manager, "AsyncPostgresSaver", FakeCheckpointer)

    first = await manager.setup_langgraph_checkpointer()
    second = await manager.setup_langgraph_checkpointer()

    assert first is second
    assert setup_calls == 1
