"""Agent 删除与持久生命周期事实的边界。"""

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.api.routers.agents.management import delete_agent
from yuxi.modules.agents.repositories.definitions import AgentRepository
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.agents.models.threads import Conversation
from yuxi.modules.identity.models import User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    """建立可核对删除结果的持久单测库。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed_agent(session):
    """创建待删除 Agent 与历史 Thread。"""
    user = User(username="owner", uid="owner", password_hash="x", role="superadmin")
    agent = Agent(
        visibility="shared",
        slug="custom-agent",
        backend_id="ChatbotAgent",
        name="Custom Agent",
        share_config={"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": None},
        created_by=user.uid,
    )
    thread = Conversation(
        thread_id="agent-thread", project_id="agent-project", uid=user.uid, agent_id=agent.slug, status="active"
    )
    session.add_all([user, agent, thread])
    await session.flush()
    return user, agent, thread


@pytest.mark.parametrize("status", ["running", "waiting", "cancelling"])
async def test_delete_agent_returns_conflict_for_active_turn(session, status):
    """三种非终态 Turn 均禁止删除其 Agent。"""
    user, agent, _ = await _seed_agent(session)
    session.add(AgentTurn(id="active-turn", conversation_thread_id="agent-thread", uid=user.uid, status=status))
    await session.flush()

    with pytest.raises(HTTPException) as exc:
        await delete_agent(agent.slug, current_user=user, db=session)

    assert exc.value.status_code == 409
    assert await session.scalar(select(Agent.id).where(Agent.id == agent.id)) == agent.id


async def test_delete_agent_rejects_pending_input_after_prior_turn(session):
    """上轮已结束但新输入排队时仍保留 Agent。"""
    user, agent, _ = await _seed_agent(session)
    session.add(
        AgentInput(
            id="pending-input",
            received_seq=1,
            conversation_thread_id="agent-thread",
            uid=user.uid,
            agent_slug=agent.slug,
            kind="follow_up",
            status="pending",
            input_payload={},
        )
    )
    await session.flush()

    with pytest.raises(ValueError, match="待处理输入"):
        await AgentRepository(session).delete(agent=agent, user=user)

    assert await session.scalar(select(Agent.id).where(Agent.id == agent.id)) == agent.id


@pytest.mark.parametrize(
    ("run_status", "cleanup_pending"),
    [("pending", False), ("completed", True)],
)
async def test_delete_agent_rejects_active_run_or_runtime_cleanup(session, run_status, cleanup_pending):
    """Turn 已终态也不能遗失 pending Run 或待清理 runtime。"""
    user, agent, thread = await _seed_agent(session)
    turn = AgentTurn(id="finished-turn", conversation_thread_id=thread.thread_id, uid=user.uid, status="completed")
    session.add(turn)
    await session.flush()
    session.add(
        AgentRun(
            id="unfinished-run",
            conversation_thread_id=thread.thread_id,
            runtime_scope_id=thread.thread_id,
            agent_slug=agent.slug,
            uid=user.uid,
            status=run_status,
            runtime_cleanup_pending=cleanup_pending,
            turn_id=turn.id,
            conversation_id=thread.id,
            run_type="chat",
            input_payload={},
        )
    )
    await session.flush()

    with pytest.raises(ValueError, match="活跃执行"):
        await AgentRepository(session).delete(agent=agent, user=user)

    assert await session.scalar(select(Agent.id).where(Agent.id == agent.id)) == agent.id


async def test_delete_agent_accepts_terminal_history(session):
    """仅有完成历史时允许删除 Agent，Thread 历史仍可保留。"""
    user, agent, thread = await _seed_agent(session)
    turn = AgentTurn(id="finished-turn", conversation_thread_id=thread.thread_id, uid=user.uid, status="completed")
    session.add(turn)
    await session.flush()
    session.add(
        AgentRun(
            id="finished-run",
            conversation_thread_id=thread.thread_id,
            runtime_scope_id=thread.thread_id,
            agent_slug=agent.slug,
            uid=user.uid,
            status="completed",
            runtime_cleanup_pending=False,
            turn_id=turn.id,
            conversation_id=thread.id,
            run_type="chat",
            input_payload={},
        )
    )
    await session.flush()

    await AgentRepository(session).delete(agent=agent, user=user)

    assert await session.scalar(select(Agent.id).where(Agent.slug == "custom-agent")) is None
    thread_id = await session.scalar(select(Conversation.thread_id).where(Conversation.agent_id == "custom-agent"))
    assert thread_id == "agent-thread"


async def test_repository_delete_rechecks_management_permission(session):
    """仓储直接调用不能绕过删除权限。"""
    _owner, agent, _ = await _seed_agent(session)
    reader = User(username="reader", uid="reader", password_hash="x", role="user")

    with pytest.raises(PermissionError, match="不能删除"):
        await AgentRepository(session).delete(agent=agent, user=reader)

    assert await session.scalar(select(Agent.id).where(Agent.id == agent.id)) == agent.id


async def test_create_lookup_uses_shared_key_lock():
    """Thread 创建查询须与 Agent 删除的独占锁冲突。"""

    class CaptureDb:
        """记录实际仓储查询。"""

        statement = None

        async def execute(self, statement):
            """返回不存在的 Agent 即可验证锁语句。"""
            self.statement = statement
            return self

        def scalar_one_or_none(self):
            """模拟空查询结果。"""
            return None

    db = CaptureDb()
    assert await AgentRepository(db).get_by_slug("custom-agent", for_key_share=True) is None
    sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert sql.endswith("FOR KEY SHARE")


async def test_shared_key_lookup_refreshes_stale_agent_in_same_session(session):
    """锁读刷新先前加载的 Agent，子执行不使用陈旧配置。"""
    _user, agent, _thread = await _seed_agent(session)
    await session.execute(text("UPDATE agents SET is_subagent = 1 WHERE id = :id"), {"id": agent.id})
    await session.commit()
    assert agent.is_subagent is False

    refreshed = await AgentRepository(session).get_by_slug(agent.slug, for_key_share=True)

    assert refreshed is agent
    assert refreshed.is_subagent is True
