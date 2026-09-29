"""Run 结果读取只依赖明确归属的持久输出。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.modules.agents.services.subagents as subagent_run_service


@pytest.mark.asyncio
async def test_run_result_rejects_output_bound_to_another_turn(monkeypatch):
    """即使输出 ID 存在，也不能把另一轮消息当作当前 Run 结果。"""
    run = SimpleNamespace(
        id="run-1",
        uid="user-1",
        status="completed",
        output_message_id=7,
        turn_id="turn-1",
        conversation_id=3,
        conversation_thread_id="thread-1",
        agent_slug="agent",
        langfuse_trace_id=None,
        token_usage={},
        error_type=None,
        error_message=None,
    )
    message = SimpleNamespace(id=7, run_id="run-1", turn_id="turn-2", conversation_id=3, content="wrong")

    class Repo:
        def __init__(self, db):
            pass

        async def get_run_for_user(self, run_id, uid):
            assert (run_id, uid) == ("run-1", "user-1")
            return run

    class DB:
        async def get(self, model, output_id):
            assert output_id == 7
            return message

    monkeypatch.setattr(subagent_run_service, "AgentRunRepository", Repo)
    with pytest.raises(ValueError, match="归属不一致"):
        await subagent_run_service.get_agent_run_result(run_id="run-1", current_uid="user-1", db=DB())


@pytest.mark.asyncio
async def test_run_result_reads_only_explicit_output(monkeypatch):
    """没有 output_message_id 时不从同线程相邻 Run 猜测文本。"""
    run = SimpleNamespace(
        id="run-1",
        uid="user-1",
        status="completed",
        output_message_id=None,
        turn_id="turn-1",
        conversation_id=3,
        conversation_thread_id="thread-1",
        agent_slug="agent",
        langfuse_trace_id="trace-1",
        token_usage={"available": False},
        error_type=None,
        error_message=None,
    )

    class Repo:
        def __init__(self, db):
            pass

        async def get_run_for_user(self, run_id, uid):
            return run

    class DB:
        async def get(self, *_args):
            raise AssertionError("无输出绑定时不应查询消息")

    monkeypatch.setattr(subagent_run_service, "AgentRunRepository", Repo)
    result = await subagent_run_service.get_agent_run_result(run_id="run-1", current_uid="user-1", db=DB())
    assert result["status"] == "completed"
    assert result["output"] == ""
    assert result["final_message_id"] is None
    assert result["turn_id"] == "turn-1"
