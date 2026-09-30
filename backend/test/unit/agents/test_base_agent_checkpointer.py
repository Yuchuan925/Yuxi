from types import SimpleNamespace

import pytest
import yuxi.modules.agents.runtime.base as base
from yuxi.modules.agents.runtime.base import BaseAgent


@pytest.mark.asyncio
async def test_base_agent_gets_independent_postgres_checkpointer_per_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个 Agent 的多次构图不能通过缓存共享 saver 锁。"""
    agent = object.__new__(BaseAgent)
    manager = SimpleNamespace()
    checkpointers = []

    def create_checkpointer(received_manager):
        assert received_manager is manager
        checkpointer = object()
        checkpointers.append(checkpointer)
        return checkpointer

    monkeypatch.setattr(base, "pg_manager", manager)
    monkeypatch.setattr(base, "get_langgraph_checkpointer", create_checkpointer)

    assert await agent._get_checkpointer() is not await agent._get_checkpointer()
    assert len(checkpointers) == 2
