"""真实 PostgreSQL 委派取消边界：恢复 Run、无关后续任务和子取消隔离。"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.integration.services.test_agent_input_schema import _create_schema, _drop_schema
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.threads import Conversation, SubagentThread
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件只在隔离 PostgreSQL Schema 中运行。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本测试不创建知识库。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本测试不创建沙盒。"""
    yield


async def test_cascade_follows_turn_delegation_not_child_thread_latest_turn():
    """根 Turn 的所有 Run 参与委派；同子 Thread 的无关后续 Turn 保持 pending。"""
    schema, admin_engine, engine = await _create_schema()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            parent = await db.scalar(select(Conversation).where(Conversation.thread_id == "input-thread"))
            children = []
            for suffix in ("active", "reused", "grandchild"):
                conversation = Conversation(
                    thread_id=f"child-{suffix}",
                    uid="input-user",
                    agent_id="child",
                    project_id="input-project",
                    status="subagent",
                )
                db.add(conversation)
                await db.flush()
                relation = SubagentThread(
                    uid="input-user",
                    parent_conversation_id=parent.id,
                    child_conversation_id=conversation.id,
                    child_thread_id=conversation.thread_id,
                    subagent_slug="child",
                    created_by_run_id="root-first",
                )
                db.add(relation)
                await db.flush()
                children.append((conversation, relation))
            turns, runs = AgentTurnRepository(db), AgentRunRepository(db)
            root = await turns.create(turn_id="root-turn", thread_id=parent.thread_id, uid="input-user", app_id=None)
            first = await runs.create_run(
                run_id="root-first",
                conversation_thread_id=parent.thread_id,
                conversation_id=parent.id,
                agent_slug="main",
                uid="input-user",
                turn_id=root.id,
                input_payload={},
            )
            first.status = "yielded"
            resumed = await runs.create_run(
                run_id="root-resume",
                conversation_thread_id=parent.thread_id,
                conversation_id=parent.id,
                agent_slug="main",
                uid="input-user",
                turn_id=root.id,
                input_payload={},
                run_type="resume",
                resume_from_run_id=first.id,
            )
            await turns.set_current(root, run_id=resumed.id)
            child_runs = []
            for number, (conversation, relation) in enumerate(children):
                turn = await turns.create(
                    turn_id=f"child-turn-{number}", thread_id=conversation.thread_id, uid="input-user", app_id=None
                )
                child = await runs.create_run(
                    run_id=f"child-run-{number}",
                    conversation_thread_id=conversation.thread_id,
                    conversation_id=conversation.id,
                    agent_slug="child",
                    uid="input-user",
                    turn_id=turn.id,
                    input_payload={},
                    run_type="subagent",
                    created_by_run_id=first.id if number < 2 else "child-run-0",
                    subagent_thread_relation_id=relation.id,
                )
                await turns.set_current(turn, run_id=child.id)
                child_runs.append((turn, child))
            # 子恢复继续拥有同一 Turn，新 Run 不靠 run_type 判断委派身份。
            active_turn, original = child_runs[0]
            original.status = "interrupted"
            current = await runs.create_run(
                run_id="child-resume",
                conversation_thread_id=original.conversation_thread_id,
                conversation_id=original.conversation_id,
                agent_slug="child",
                uid="input-user",
                turn_id=active_turn.id,
                input_payload={},
                run_type="subagent",
                created_by_run_id=first.id,
                resume_from_run_id=original.id,
                subagent_thread_relation_id=children[0][1].id,
            )
            await turns.set_current(active_turn, run_id=current.id)
            old_turn, old_run = child_runs[1]
            old_turn.status, old_run.status = "completed", "completed"
            other_parent_turn = AgentTurn(
                id="other-parent-turn", conversation_thread_id=parent.thread_id,
                uid="input-user", app_id=None, status="completed",
            )
            db.add(other_parent_turn)
            await db.flush()
            other_parent_run = await runs.create_run(
                run_id="other-parent-run",
                conversation_thread_id=parent.thread_id,
                conversation_id=parent.id,
                agent_slug="main",
                uid="input-user",
                turn_id=other_parent_turn.id,
                input_payload={},
            )
            other_parent_run.status = "completed"
            unrelated = await turns.create(
                turn_id="unrelated-turn", thread_id=old_run.conversation_thread_id, uid="input-user", app_id=None
            )
            unrelated_run = await runs.create_run(
                run_id="unrelated-run",
                conversation_thread_id=old_run.conversation_thread_id,
                conversation_id=old_run.conversation_id,
                agent_slug="child",
                uid="input-user",
                turn_id=unrelated.id,
                input_payload={},
                run_type="subagent",
                created_by_run_id=other_parent_run.id,
                subagent_thread_relation_id=children[1][1].id,
            )
            await turns.set_current(unrelated, run_id=unrelated_run.id)
            queued = await AgentInputRepository(db).create(
                input_id="unrelated-followup",
                thread_id=old_run.conversation_thread_id,
                uid="input-user",
                app_id=None,
                agent_slug="child",
                kind="follow_up",
                input_payload={},
            )
            await db.commit()
        async with sessions() as db:
            root_run = await AgentRunRepository(db).get_run("root-resume")
            result = await AgentRunRepository(db).cancel_active_execution_tree_descendants(root_run)
            assert {run_id for run_id, _ in result} == {"child-resume", "child-run-2"}
            await db.commit()
        async with sessions() as db:
            assert (await db.get(AgentTurn, "child-turn-0")).status == "cancelled"
            assert (await db.get(AgentTurn, "child-turn-2")).status == "cancelled"
            assert (await AgentRunRepository(db).get_run("unrelated-run")).status == "pending"
            assert (await db.get(AgentTurn, "unrelated-turn")).status == "running"
            assert (await db.get(AgentInput, queued.id)).status == "pending"
            assert not (
                await db.scalar(select(Conversation).where(Conversation.thread_id == "child-reused"))
            ).queue_paused
            assert (await db.get(AgentTurn, "root-turn")).status == "running"
    finally:
        await _drop_schema(schema, admin_engine, engine)
