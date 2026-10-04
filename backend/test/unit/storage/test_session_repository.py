from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.modules.agents.repositories.sessions import SessionRepository, MAX_SESSION_TITLE_LENGTH
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.models.messages import Message, ToolCall
from yuxi.shared.datetime import utc_now

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture()
async def session_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


def test_normalize_title_truncates_when_too_long():
    repo = SessionRepository(None)  # type: ignore[arg-type]
    raw = "a" * (MAX_SESSION_TITLE_LENGTH + 50)

    normalized = repo._normalize_title(raw)

    assert normalized is not None
    assert len(normalized) == MAX_SESSION_TITLE_LENGTH
    assert normalized == "a" * MAX_SESSION_TITLE_LENGTH


def test_normalize_title_trims_spaces():
    repo = SessionRepository(None)  # type: ignore[arg-type]

    normalized = repo._normalize_title("   hello world   ")

    assert normalized == "hello world"


@pytest.mark.asyncio
async def test_list_agent_runs_for_trace_returns_latest_bounded_window_in_order(session_session):
    now = utc_now()
    agent_session = Session(
        thread_id="thread-run-trace-window",
        project_id="project-run-trace-window",
        uid="user-a",
        agent_id="agent-a",
        title="Run trace window",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session_session.add(agent_session)
    await session_session.flush()
    session_session.add(
        AgentTurn(id="turn-trace", thread_id=agent_session.thread_id, uid=agent_session.uid, status="completed")
    )
    await session_session.flush()
    for index in range(3):
        created_at = now + timedelta(seconds=index)
        session_session.add(
            AgentRun(
                id=f"run-trace-{index}",
                thread_id=agent_session.thread_id,
                runtime_scope_id=agent_session.thread_id,
                agent_slug="main",
                uid=agent_session.uid,
                status="completed",
                turn_id="turn-trace",
                session_record_id=agent_session.id,
                input_payload={},
                created_at=created_at,
                started_at=created_at,
                finished_at=created_at,
            )
        )
    await session_session.commit()

    runs, truncated = await SessionRepository(session_session).list_agent_runs_for_trace(
        agent_session.id,
        limit=2,
    )

    assert [run.id for run in runs] == ["run-trace-1", "run-trace-2"]
    assert truncated is True


@pytest.mark.asyncio
async def test_lock_session_refreshes_cached_lifecycle_state(tmp_path):
    """加锁读取必须刷新同一 Session 中已缓存的生命周期状态。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent_session-lock.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as reader, factory() as writer:
            agent_session = Session(
                thread_id="thread-refresh-lock",
                project_id="project-refresh-lock",
                uid="user-a",
                agent_id="agent-a",
                title="Refresh lock",
                status="active",
            )
            reader.add(agent_session)
            await reader.commit()

            repository = SessionRepository(reader)
            cached = await repository.get_session_by_id(agent_session.id)
            assert cached is agent_session
            assert cached.status == "active"

            await writer.execute(update(Session).where(Session.id == agent_session.id).values(status="deleted"))
            await writer.commit()

            locked = await repository.lock_session_by_thread_id(agent_session.thread_id)

            assert locked is agent_session
            assert locked.status == "deleted"
    finally:
        await engine.dispose()


def _seed_source_filter_sessions() -> tuple[Session, Session, Session, datetime]:
    now = utc_now()
    normal = Session(
        thread_id="thread-normal",
        project_id="project-thread-normal",
        uid="user-a",
        agent_id="agent-a",
        title="Normal",
        status="active",
        created_at=now,
        updated_at=now,
        extra_metadata={},
    )
    subagent = Session(
        thread_id="thread-subagent",
        project_id="project-thread-subagent",
        uid="user-a",
        agent_id="agent-a",
        title="Subagent Thread",
        status="active",
        is_pinned=True,
        created_at=now,
        updated_at=now + timedelta(minutes=2),
        extra_metadata={"source": "subagent"},
    )
    public_api = Session(
        thread_id="thread-public",
        project_id="project-thread-public",
        uid="user-a",
        agent_id="agent-a",
        title="Public API Thread",
        status="active",
        created_at=now,
        updated_at=now + timedelta(minutes=1),
        extra_metadata={"source": "public_api"},
    )
    return normal, subagent, public_api, now


@pytest.mark.asyncio
async def test_model_audit_messages_are_hidden_from_history_and_message_count(session_session):
    now = utc_now()
    agent_session = Session(
        thread_id="thread-model-audit",
        project_id="project-model-audit",
        uid="user-a",
        agent_id="agent-a",
        title="Model Audit",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session_session.add(agent_session)
    await session_session.flush()
    visible_message = Message(
        session_record_id=agent_session.id,
        role="user",
        content="visible",
        message_type="text",
    )
    audit_message = Message(
        session_record_id=agent_session.id,
        role="assistant",
        content="hidden intermediate",
        message_type="model_audit",
        operation_id="model-1",
        execution_status="completed",
    )
    tool_audit = Message(
        session_record_id=agent_session.id,
        role="tool",
        content="hidden tool output",
        message_type="tool_audit",
        operation_id="tool-1",
        started_at=now,
        sequence=2,
        execution_status="completed",
    )
    session_session.add_all([visible_message, audit_message, tool_audit])
    await session_session.commit()

    repo = SessionRepository(session_session)
    messages = await repo.get_messages(agent_session.id)

    assert [message.content for message in messages] == ["visible"]

    previous_updated_at = agent_session.updated_at
    await repo.publish_assistant_output(audit_message)
    published_messages = await repo.get_messages(agent_session.id)

    assert [message.content for message in published_messages] == ["visible", "hidden intermediate"]
    assert audit_message.message_type == "text"
    assert agent_session.updated_at > previous_updated_at


@pytest.mark.asyncio
async def test_only_state_proven_terminal_model_audit_keeps_tool_call_visible(session_session):
    now = utc_now()
    agent_session = Session(
        thread_id="thread-tool-audit",
        project_id="project-tool-audit",
        uid="user-a",
        agent_id="agent-a",
        title="Tool Audit",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session_session.add(agent_session)
    await session_session.flush()
    session_session.add(AgentTurn(id="turn-tool-audit", thread_id=agent_session.thread_id, uid=agent_session.uid))
    await session_session.flush()
    runs = [
        AgentRun(
            id="run-active",
            thread_id=agent_session.thread_id,
            runtime_scope_id=agent_session.thread_id,
            agent_slug="main",
            uid=agent_session.uid,
            status="running",
            turn_id="turn-tool-audit",
            session_record_id=agent_session.id,
            input_payload={},
        ),
        AgentRun(
            id="run-unproven",
            thread_id=agent_session.thread_id,
            runtime_scope_id=agent_session.thread_id,
            agent_slug="main",
            uid=agent_session.uid,
            status="completed",
            turn_id="turn-tool-audit",
            session_record_id=agent_session.id,
            input_payload={},
        ),
        AgentRun(
            id="run-proven",
            thread_id=agent_session.thread_id,
            runtime_scope_id=agent_session.thread_id,
            agent_slug="main",
            uid=agent_session.uid,
            status="interrupted",
            turn_id="turn-tool-audit",
            session_record_id=agent_session.id,
            input_payload={},
        ),
    ]
    session_session.add_all(runs)
    await session_session.flush()
    audit_messages = [
        Message(
            session_record_id=agent_session.id,
            role="assistant",
            content="active",
            message_type="model_audit",
            extra_metadata={"state_reconciled": True},
            run_id="run-active",
            operation_id="model-active",
            execution_status="completed",
        ),
        Message(
            session_record_id=agent_session.id,
            role="assistant",
            content="unproven",
            message_type="model_audit",
            run_id="run-unproven",
            operation_id="model-unproven",
            execution_status="completed",
        ),
        Message(
            session_record_id=agent_session.id,
            role="assistant",
            content="proven",
            message_type="model_audit",
            extra_metadata={"state_reconciled": True},
            run_id="run-proven",
            operation_id="model-proven",
            execution_status="completed",
        ),
    ]
    for message in audit_messages:
        message.turn_id = "turn-tool-audit"
    session_session.add_all(audit_messages)
    await session_session.flush()
    session_session.add_all(
        [
            ToolCall(
                message_id=message.id,
                langgraph_tool_call_id=f"tool-{message.operation_id}",
                tool_name="search",
                status="success",
            )
            for message in audit_messages
        ]
    )
    await session_session.commit()

    messages = await SessionRepository(session_session).get_messages(agent_session.id)

    assert [message.content for message in messages] == ["proven"]
    assert messages[0].tool_calls[0].langgraph_tool_call_id == "tool-model-proven"


@pytest.mark.asyncio
async def test_list_sessions_excludes_subagent_source(session_session):
    normal, subagent, public_api, _ = _seed_source_filter_sessions()
    session_session.add_all([normal, subagent, public_api])
    await session_session.commit()

    repo = SessionRepository(session_session)
    items = await repo.list_sessions(
        uid="user-a",
        limit=20,
        offset=0,
        exclude_sources=("subagent",),
    )

    assert {item.thread_id for item in items} == {"thread-normal", "thread-public"}


@pytest.mark.asyncio
async def test_list_sessions_paginates_only_non_pinned_items(session_session):
    now = utc_now()
    pinned = Session(
        thread_id="thread-pinned",
        project_id="project-pinned",
        uid="user-a",
        agent_id="agent-a",
        title="Pinned",
        status="active",
        is_pinned=True,
        created_at=now,
        updated_at=now + timedelta(minutes=10),
    )
    regular = [
        Session(
            thread_id=f"thread-{index}",
            project_id=f"project-{index}",
            uid="user-a",
            agent_id="agent-a",
            title=f"Thread {index}",
            status="active",
            created_at=now,
            updated_at=now + timedelta(minutes=index),
        )
        for index in range(4)
    ]
    session_session.add_all([pinned, *regular])
    await session_session.commit()

    repository = SessionRepository(session_session)
    first_page = await repository.list_sessions(uid="user-a", limit=2, offset=0)
    second_page = await repository.list_sessions(uid="user-a", limit=2, offset=2)

    assert [item.thread_id for item in first_page] == ["thread-pinned", "thread-3", "thread-2"]
    assert [item.thread_id for item in second_page] == ["thread-pinned", "thread-1", "thread-0"]


@pytest.mark.asyncio
async def test_search_sessions_by_message_content_filters_user_status_and_tool_messages(session_session):
    now = utc_now()
    active = Session(
        thread_id="thread-active",
        project_id="project-thread-active",
        uid="user-a",
        agent_id="agent-a",
        title="Active Thread",
        status="active",
        created_at=now,
        updated_at=now,
    )
    deleted = Session(
        thread_id="thread-deleted",
        project_id="project-thread-deleted",
        uid="user-a",
        agent_id="agent-a",
        title="Deleted Thread",
        status="deleted",
        created_at=now,
        updated_at=now,
    )
    other_user = Session(
        thread_id="thread-other-user",
        project_id="project-thread-other-user",
        uid="user-b",
        agent_id="agent-a",
        title="Other User Thread",
        status="active",
        created_at=now,
        updated_at=now,
    )
    tool_only = Session(
        thread_id="thread-tool-only",
        project_id="project-thread-tool-only",
        uid="user-a",
        agent_id="agent-a",
        title="Tool Only Thread",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session_session.add_all([active, deleted, other_user, tool_only])
    await session_session.flush()
    session_session.add_all(
        [
            Message(
                agent_session=active,
                role="assistant",
                content="大陆部署方案需要保留",
                message_type="text",
                created_at=now,
            ),
            Message(
                agent_session=deleted,
                role="assistant",
                content="大陆 deleted should not show",
                message_type="text",
                created_at=now,
            ),
            Message(
                agent_session=other_user,
                role="assistant",
                content="大陆 other user should not show",
                message_type="text",
                created_at=now,
            ),
            Message(
                agent_session=tool_only,
                role="tool",
                content="大陆 tool output should not show",
                message_type="tool_result",
                created_at=now,
            ),
        ]
    )
    await session_session.commit()

    repo = SessionRepository(session_session)
    items, has_more = await repo.search_sessions_by_message_content(
        uid="user-a",
        query="大陆",
        limit=20,
        offset=0,
    )

    assert has_more is False
    assert [item["agent_session"].thread_id for item in items] == ["thread-active"]
    assert items[0]["matched_count"] == 1
    assert items[0]["message_id"] is not None
    assert "大陆部署方案" in items[0]["snippets"][0]["content"]


@pytest.mark.asyncio
async def test_search_sessions_by_message_content_excludes_subagent_source(session_session):
    normal, subagent, public_api, now = _seed_source_filter_sessions()
    session_session.add_all([normal, subagent, public_api])
    await session_session.flush()
    session_session.add_all(
        [
            Message(agent_session=normal, role="user", content="导航隐藏检查", message_type="text", created_at=now),
            Message(
                agent_session=subagent,
                role="user",
                content="导航隐藏检查 call",
                message_type="text",
                created_at=now,
            ),
            Message(
                agent_session=public_api,
                role="user",
                content="导航隐藏检查 eval",
                message_type="text",
                created_at=now,
            ),
        ]
    )
    await session_session.commit()

    repo = SessionRepository(session_session)
    items, has_more = await repo.search_sessions_by_message_content(
        uid="user-a",
        query="导航隐藏检查",
        limit=20,
        offset=0,
        exclude_sources=("subagent",),
    )

    assert has_more is False
    assert {item["agent_session"].thread_id for item in items} == {"thread-normal", "thread-public"}


@pytest.mark.asyncio
async def test_search_sessions_by_message_content_filters_agent_and_paginates(session_session):
    now = utc_now()
    old = now - timedelta(days=1)
    first = Session(
        thread_id="thread-first",
        project_id="project-thread-first",
        uid="user-a",
        agent_id="agent-a",
        title="First",
        status="active",
        created_at=old,
        updated_at=old,
    )
    second = Session(
        thread_id="thread-second",
        project_id="project-thread-second",
        uid="user-a",
        agent_id="agent-a",
        title="Second",
        status="active",
        created_at=now,
        updated_at=now,
    )
    other_agent = Session(
        thread_id="thread-other-agent",
        project_id="project-thread-other-agent",
        uid="user-a",
        agent_id="agent-b",
        title="Other Agent",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session_session.add_all([first, second, other_agent])
    await session_session.flush()
    session_session.add_all(
        [
            Message(
                agent_session=first,
                role="user",
                content="大陆关键词 old",
                message_type="text",
                created_at=old,
            ),
            Message(
                agent_session=second,
                role="assistant",
                content="大陆关键词 latest",
                message_type="text",
                created_at=now,
            ),
            Message(
                agent_session=other_agent,
                role="assistant",
                content="大陆关键词 other agent",
                message_type="text",
                created_at=now,
            ),
        ]
    )
    await session_session.commit()

    repo = SessionRepository(session_session)
    first_page, has_more = await repo.search_sessions_by_message_content(
        uid="user-a",
        agent_id="agent-a",
        query="大陆",
        limit=1,
        offset=0,
    )
    second_page, second_has_more = await repo.search_sessions_by_message_content(
        uid="user-a",
        agent_id="agent-a",
        query="大陆",
        limit=1,
        offset=1,
    )

    assert has_more is True
    assert [item["agent_session"].thread_id for item in first_page] == ["thread-second"]
    assert second_has_more is False
    assert [item["agent_session"].thread_id for item in second_page] == ["thread-first"]
