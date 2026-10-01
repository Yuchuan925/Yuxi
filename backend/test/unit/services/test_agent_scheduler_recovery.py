"""恢复扫描必须尊重子 Run 的持久执行树状态。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import yuxi.modules.agents.services.scheduler as scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_status", "current_status", "expected_delivery", "expected_terminal"),
    [
        ("running", "pending", ["child-run"], []),
        ("completed", "pending", ["child-run"], []),
        ("running", "cancelled", [], []),
    ],
)
async def test_recovery_republishes_owned_child_after_parent_completion(
    monkeypatch, parent_status, current_status, expected_delivery, expected_terminal
):
    """崩溃遗留子 Run 可补投；父树取消和扫描后的取消均不能投递。"""
    candidate = SimpleNamespace(
        id="child-run",
        uid="user-1",
        app_id=None,
        status="pending",
        run_type="subagent",
        conversation_thread_id="child-thread",
        runtime_scope_id="child-thread",
        agent_slug="child",
        turn_id="turn-1",
        created_by_run_id="parent-run",
    )
    current = SimpleNamespace(**{**vars(candidate), "status": current_status})
    parent = SimpleNamespace(
        id="parent-run",
        uid="user-1",
        app_id=None,
        status=parent_status,
        run_type="chat",
        conversation_id=1,
        conversation_thread_id="root-thread",
        turn_id="turn-1",
        runtime_scope_id="root-thread",
    )
    root = SimpleNamespace(id=1, thread_id="root-thread", uid="user-1", app_id=None, status="active")
    child_thread = SimpleNamespace(
        thread_id="child-thread", uid="user-1", app_id=None, agent_id="child", status="subagent"
    )
    delivered = []
    terminal = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self.rows

        def all(self):
            return self.rows

    class Session:
        def __init__(self):
            self.queries = 0

        async def execute(self, statement):
            self.queries += 1
            if self.queries == 1:
                sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
                return Result([] if "agent_runs.run_type != 'subagent'" in sql else [candidate])
            return Result([])

    @asynccontextmanager
    async def session_context():
        yield Session()

    class RunRepo:
        def __init__(self, db):
            pass

        async def get_run(self, run_id):
            return current

        async def lock_run_for_user(self, run_id, uid):
            return parent if run_id == "parent-run" else current

        async def get_subagent_run_with_creator(self, **kwargs):
            return parent, current

        async def set_terminal_status(self, run_id, *, status, **kwargs):
            terminal.append((run_id, status))
            current.status = status

    class ConvRepo:
        def __init__(self, db):
            pass

        async def lock_conversation_by_thread_id(self, thread_id):
            return root

        async def get_conversation_by_thread_id(self, thread_id):
            return child_thread

    class TurnRepo:
        def __init__(self, db):
            pass

        async def get_for_scope(self, **kwargs):
            return SimpleNamespace(status="running", current_run_id="child-run")

    async def binding(**kwargs):
        return SimpleNamespace(materialize_managed=False)

    async def deliver(dispatch):
        delivered.append(dispatch.run_id)

    monkeypatch.setattr(scheduler.pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(scheduler, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(scheduler, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(scheduler, "AgentTurnRepository", TurnRepo)
    monkeypatch.setattr(scheduler, "resolve_conversation_workdir_binding", binding)
    monkeypatch.setattr(scheduler, "deliver", deliver)

    await scheduler.recover_pending_dispatches()

    assert delivered == expected_delivery
    assert terminal == expected_terminal
