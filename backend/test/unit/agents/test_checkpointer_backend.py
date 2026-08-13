from __future__ import annotations

import pytest

from yuxi.agents.base import BaseAgent


class _TestAgent(BaseAgent):
    async def get_graph(self, **kwargs):
        del kwargs
        return None


def _build_agent(tmp_path) -> _TestAgent:
    agent = object.__new__(_TestAgent)
    agent.checkpointer = None
    agent._async_conn = None
    agent.workdir = tmp_path
    return agent


@pytest.mark.asyncio
async def test_checkpointer_defaults_to_postgres_without_fallback(monkeypatch, tmp_path):
    postgres_checkpointer = object()
    agent = _build_agent(tmp_path)

    monkeypatch.delenv("LANGGRAPH_CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setattr(agent, "_create_postgres_checkpointer", lambda: postgres_checkpointer)

    async def fail_sqlite_connection():
        raise AssertionError("默认 PostgreSQL 不应尝试 SQLite")

    monkeypatch.setattr(agent, "get_async_conn", fail_sqlite_connection)

    assert await agent._get_checkpointer() is postgres_checkpointer


@pytest.mark.asyncio
async def test_postgres_checkpointer_failure_does_not_fallback(monkeypatch, tmp_path):
    agent = _build_agent(tmp_path)

    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "postgres")

    def fail_postgres():
        raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(agent, "_create_postgres_checkpointer", fail_postgres)

    async def fail_sqlite_connection():
        raise AssertionError("PostgreSQL 失败后不得尝试 SQLite")

    monkeypatch.setattr(agent, "get_async_conn", fail_sqlite_connection)

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        await agent._get_checkpointer()


@pytest.mark.asyncio
async def test_memory_checkpointer_requires_explicit_backend(monkeypatch, tmp_path):
    agent = _build_agent(tmp_path)
    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "memory")

    checkpointer = await agent._get_checkpointer()

    assert checkpointer.__class__.__name__ == "InMemorySaver"


@pytest.mark.asyncio
async def test_sqlite_checkpointer_requires_explicit_backend(monkeypatch, tmp_path):
    sqlite_checkpointer = object()
    connection = object()
    agent = _build_agent(tmp_path)
    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "sqlite")

    async def get_connection():
        return connection

    monkeypatch.setattr(agent, "get_async_conn", get_connection)

    def create_sqlite_checkpointer(conn):
        assert conn is connection
        return sqlite_checkpointer

    monkeypatch.setattr("yuxi.agents.base.AsyncSqliteSaver", create_sqlite_checkpointer)

    assert await agent._get_checkpointer() is sqlite_checkpointer
