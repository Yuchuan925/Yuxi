from types import SimpleNamespace

import pytest
from yuxi.modules.agents.runtime.base import BaseAgent


@pytest.mark.asyncio
async def test_base_agent_gets_independent_postgres_checkpointer_per_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个 Agent 的多次构图不能通过缓存共享 saver 锁。"""
    agent = object.__new__(BaseAgent)
    manager = SimpleNamespace(get_langgraph_checkpointer=object)
    monkeypatch.setattr("yuxi.modules.agents.runtime.base.pg_manager", manager)

    assert await agent._get_checkpointer() is not await agent._get_checkpointer()
