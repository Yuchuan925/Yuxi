"""子执行必须绑定活跃根 Turn 与明确的父执行树。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.modules.agents.services.subagents as module
from yuxi.modules.agents.services.input_messages import build_chat_input_message


@pytest.mark.asyncio
async def test_progress_projects_structured_stream_events(monkeypatch):
    """子 Run 的结构化增量应在父工具进度中可见。"""

    async def recent_events(_run_id, *, limit):
        assert limit == 100
        return [
            {
                "seq": "6-0",
                "event": {
                    "type": "agent.session.turn.item.added",
                    "item": {"type": "function_call", "id": "t1", "name": "search"},
                },
            },
            {
                "seq": "5-0",
                "event": {"type": "agent.session.turn.output_text.delta", "item_id": "m1", "delta": "正在查询"},
            },
        ]

    monkeypatch.setattr(module, "list_recent_run_stream_events", recent_events)
    progress = await module.get_agent_run_progress("run-1")
    assert progress["messages"] == [
        {"content": "正在查询", "kind": "assistant_message", "seq": "5-0", "item_id": "m1"},
        {"content": "调用工具 search", "kind": "tool_call", "seq": "6-0", "item_id": "t1"},
    ]


@pytest.mark.asyncio
async def test_start_rejects_archived_root_before_creating_child(monkeypatch):
    """Project 归档父 Thread 后，旧父 Run 不能再写新的子 Thread。"""
    parent = SimpleNamespace(
        id="parent-run",
        uid="user-1",
        app_id=None,
        runtime_scope_id="root-thread",
        turn_id="turn-1",
        status="running",
    )

    class RunRepo:
        def __init__(self, db):
            pass

        async def get_run_for_user(self, run_id, uid):
            assert (run_id, uid) == ("parent-run", "user-1")
            return parent

    class ConvRepo:
        def __init__(self, db):
            pass

        async def get_session_by_thread_id(self, thread_id):
            assert thread_id == "root-thread"
            return SimpleNamespace(uid="user-1", app_id=None, status="archived")

    class UnusedRepo:
        def __init__(self, db):
            pass

    monkeypatch.setattr(module, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(module, "SessionRepository", ConvRepo)
    monkeypatch.setattr(module, "ProjectRepository", UnusedRepo)
    monkeypatch.setattr(module, "SubagentThreadRepository", UnusedRepo)
    service = module.SubagentRunService(object())
    with pytest.raises(ValueError, match="根 Thread 不存在"):
        await service.start(
            uid="user-1",
            created_by_run_id="parent-run",
            agent_item=SimpleNamespace(slug="child", name="Child"),
            input_message=build_chat_input_message("work"),
            tool_call_id="call-1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_present", "project_active", "root_rebound"),
    [(True, True, False), (True, False, False), (False, True, False), (True, True, True)],
)
async def test_start_locks_agent_and_project_before_root_execution_tree(
    monkeypatch, agent_present, project_active, root_rebound
):
    """子 Run 创建先锁子 Agent 与 Project，删除后不得进入根执行树。"""
    locks = []
    parent = SimpleNamespace(
        id="parent-run",
        uid="user-1",
        app_id=None,
        runtime_scope_id="root-thread",
        session_record_id=10,
        thread_id="root-thread",
        turn_id="turn-1",
        run_type="chat",
        status="running",
    )
    root = SimpleNamespace(
        id=10,
        thread_id="root-thread",
        project_id="project-1",
        uid="user-1",
        app_id=None,
        status="active",
    )

    class AgentRepo:
        def __init__(self, db):
            pass

        async def get_visible_by_slug(self, *, slug, user, kind, for_key_share=False):
            assert slug == "child" and for_key_share
            locks.append("agent")
            return SimpleNamespace(id=7, slug="child", name="Child", is_subagent=True) if agent_present else None

    class RunRepo:
        def __init__(self, db):
            pass

        async def get_run_for_user(self, run_id, uid):
            return parent

        async def lock_run_for_user(self, run_id, uid):
            locks.append("run")
            return parent

    class ConvRepo:
        def __init__(self, db):
            pass

        async def get_session_by_thread_id(self, thread_id):
            return root

        async def lock_session_by_thread_id(self, thread_id):
            locks.append("thread")
            return root

    class ProjectRepo:
        def __init__(self, db):
            pass

        async def lock_active_for_user(self, project_id, uid):
            locks.append("project")
            if root_rebound:
                root.project_id = "other-project"
            return SimpleNamespace(id=project_id) if project_active else None

    class TurnRepo:
        def __init__(self, db):
            pass

        async def get_for_scope(self, **kwargs):
            locks.append("turn")
            return SimpleNamespace(current_run_id="parent-run", status="running")

    class UnusedRepo:
        def __init__(self, db):
            pass

    async def relation(self, **kwargs):
        return SimpleNamespace(id=1, child_thread_id="child-thread")

    async def existing_run(self, **kwargs):
        return SimpleNamespace(id="child-run"), False, None

    monkeypatch.setattr(module, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(module, "AgentRepository", AgentRepo, raising=False)
    monkeypatch.setattr(module, "SessionRepository", ConvRepo)
    monkeypatch.setattr(module, "ProjectRepository", ProjectRepo)
    monkeypatch.setattr(module, "SubagentThreadRepository", UnusedRepo)
    monkeypatch.setattr(module, "AgentTurnRepository", TurnRepo)
    monkeypatch.setattr(module.SubagentRunService, "_ensure_thread_relation", relation)
    monkeypatch.setattr(module.SubagentRunService, "_create_run_record", existing_run)

    class Db:
        async def scalar(self, statement):
            return SimpleNamespace(uid="user-1", role="user", is_deleted=0)

    async def start():
        """调用待测子 Run 创建入口。"""
        return await module.SubagentRunService(Db()).start(
            uid="user-1",
            created_by_run_id="parent-run",
            agent_item=SimpleNamespace(id=7, slug="child", name="Child"),
            input_message=build_chat_input_message("work"),
            tool_call_id="call-1",
        )

    if agent_present and project_active and not root_rebound:
        await start()
        assert locks == ["agent", "project", "thread", "turn", "run"]
    elif root_rebound:
        with pytest.raises(ValueError, match="根 Thread 不存在"):
            await start()
        assert locks == ["agent", "project", "thread"]
    elif agent_present:
        with pytest.raises(ValueError, match="Project 不存在"):
            await start()
        assert locks == ["agent", "project"]
    else:
        with pytest.raises(ValueError, match="子智能体不存在"):
            await start()
        assert locks == ["agent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("paused,kind", [(True, None), (False, "steer"), (False, "follow_up")])
async def test_delegation_does_not_resume_or_claim_another_input(monkeypatch, paused, kind):
    """重新委派遇到暂停或待消费输入时返回 busy，不改变子队列。"""
    child = SimpleNamespace(id=20, thread_id="child", queue_paused=paused, extra_metadata={"saved": True})

    class ConvRepo:
        def __init__(self, db):
            pass

        async def lock_session_by_thread_id(self, thread_id):
            return child

    class ReceiptRepo:
        def __init__(self, db):
            pass

        async def get_for_scope(self, **kwargs):
            return None

    class TurnRepo:
        def __init__(self, db):
            pass

        async def lock_active_for_thread(self, **kwargs):
            return None

    class InputRepo:
        def __init__(self, db):
            pass

        async def get_queue_head(self, **kwargs):
            return SimpleNamespace(kind=kind) if kind else None

    class UnusedRepo:
        def __init__(self, db):
            pass

    async def forbidden_accept(**kwargs):
        pytest.fail("busy 子 Thread 不应接收新的委派输入")

    monkeypatch.setattr(module, "SessionRepository", ConvRepo)
    monkeypatch.setattr(module, "AgentInputReceiptRepository", ReceiptRepo)
    monkeypatch.setattr(module, "AgentTurnRepository", TurnRepo)
    monkeypatch.setattr(module, "AgentInputRepository", InputRepo)
    monkeypatch.setattr(module, "AgentRunRepository", UnusedRepo)
    monkeypatch.setattr(module, "ProjectRepository", UnusedRepo)
    monkeypatch.setattr(module, "SubagentThreadRepository", UnusedRepo)
    monkeypatch.setattr(module, "accept_locked", forbidden_accept)
    with pytest.raises(module.SubagentRunBusy) as exc:
        await module.SubagentRunService(object())._create_run_record(
            input_message=build_chat_input_message("delegate"),
            current_uid="user",
            creator_run=SimpleNamespace(id="parent", app_id=None, session_record_id=10),
            relation=SimpleNamespace(child_thread_id="child", child_session_record_id=20, parent_session_record_id=10),
            agent_item=SimpleNamespace(slug="helper"),
            tool_call_id="call",
        )
    assert exc.value.active_run_status == ("paused" if paused else "pending")
    assert child.queue_paused == paused and child.extra_metadata == {"saved": True}


def test_state_requires_tool_call_identity():
    """不从相邻子 Run 猜测工具调用身份。"""
    run = SimpleNamespace(id="run-1", input_payload={"runtime": {}})
    with pytest.raises(ValueError, match="tool_call_id"):
        module.serialize_subagent_run_state(run)
