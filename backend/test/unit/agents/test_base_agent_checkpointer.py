from types import SimpleNamespace

import pytest
from yuxi.agents.base import BaseAgent


@pytest.mark.asyncio
async def test_postgres_is_the_python_default_checkpointer(monkeypatch: pytest.MonkeyPatch):
    """非 Compose 启动也应默认使用 PostgreSQL；显式声明也走同一路径。"""
    agent = object.__new__(BaseAgent)
    agent.checkpointer = None
    saver = object()
    monkeypatch.delenv("LANGGRAPH_CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.setattr("yuxi.agents.base.pg_manager", SimpleNamespace(get_langgraph_checkpointer=lambda: saver))

    assert await agent._get_checkpointer() is saver

    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "postgres")
    assert await agent._get_checkpointer() is saver


@pytest.mark.asyncio
async def test_unknown_checkpointer_backend_is_rejected(monkeypatch: pytest.MonkeyPatch):
    """非法后端配置应立即暴露。"""
    agent = object.__new__(BaseAgent)
    agent.checkpointer = None
    monkeypatch.setenv("LANGGRAPH_CHECKPOINTER_BACKEND", "unknown")

    with pytest.raises(ValueError, match="不支持的 LangGraph checkpointer backend"):
        await agent._get_checkpointer()
