"""Public Thread 状态、已读与作用域的单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.conversation_repository import ConversationRepository, UNVIEWED_RUN_MARKER
from yuxi.services.agents.scope import ActorScope
from yuxi.services.agents.threads import archive_thread, get_thread_snapshot, list_threads, mark_thread_viewed
from yuxi.storage.postgres.models_business import AgentRun, AgentTurn, Base, Conversation, Project

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]
SCOPE = ActorScope(uid="user-1", app_id=None)


@pytest_asyncio.fixture()
async def session():
    """创建与生产 ORM 同形的独立内存数据库。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed_thread(
    db, thread_id: str, *, uid: str = "user-1", app_id: str | None = None, last_viewed_run_id: str | None = None
) -> Conversation:
    """创建一个完整作用域的活动 Thread。"""
    project_id = f"project-{thread_id}"
    db.add(
        Project(
            id=project_id,
            uid=uid,
            selection_status="implicit",
            workdir_path=f"projects/{project_id}",
            directory_mode="managed",
        )
    )
    conversation = Conversation(
        thread_id=thread_id,
        project_id=project_id,
        uid=uid,
        app_id=app_id,
        agent_id="main",
        title=thread_id,
        status="active",
        extra_metadata={},
        last_viewed_run_id=last_viewed_run_id,
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def _seed_run(
    db, conversation: Conversation, run_id: str, status: str, *, created_at: datetime | None = None
) -> AgentRun:
    """建立显式 Turn→Run 关系供状态读取。"""
    turn = AgentTurn(
        id=f"turn-{run_id}",
        conversation_thread_id=conversation.thread_id,
        uid=conversation.uid,
        app_id=conversation.app_id,
        status="running" if status in {"pending", "running", "cancel_requested"} else "completed",
        result_run_id=run_id if status == "completed" else None,
        current_run_id=run_id,
    )
    run = AgentRun(
        id=run_id,
        conversation_thread_id=conversation.thread_id,
        runtime_scope_id=conversation.thread_id,
        agent_slug="main",
        uid=conversation.uid,
        app_id=conversation.app_id,
        turn_id=turn.id,
        conversation_id=conversation.id,
        run_type="chat",
        input_payload={},
        status=status,
        created_at=created_at,
    )
    db.add_all([turn, run])
    await db.flush()
    return run


async def test_public_list_maps_run_states_and_enforces_scope(session):
    """侧边栏仅使用可见 Thread 的最新顶层 Run 计算状态。"""
    running = await _seed_thread(session, "thread-running")
    await _seed_run(session, running, "run-running", "running")
    ready = await _seed_thread(session, "thread-ready")
    await _seed_run(session, ready, "run-ready", "completed")
    done = await _seed_thread(session, "thread-done", last_viewed_run_id="run-done")
    await _seed_run(session, done, "run-done", "completed")
    await _seed_thread(session, "thread-empty")
    await _seed_thread(session, "thread-other-app", app_id="app-2")
    await _seed_thread(session, "thread-other-user", uid="user-2")
    await session.commit()

    items = await list_threads(db=session, scope=SCOPE)
    status_by_id = {item["id"]: item["thread_status"] for item in items}
    assert status_by_id == {
        "thread-running": "loading",
        "thread-ready": "ready",
        "thread-done": "done",
        "thread-empty": "done",
    }


async def test_latest_run_wins_and_viewed_mark_tracks_exact_run(session):
    """新 Run 产生后旧已读标记不掩盖未读结果。"""
    thread = await _seed_thread(session, "thread-latest", last_viewed_run_id="run-old")
    created_at = datetime(2026, 9, 29, 8, 0, 0)
    await _seed_run(session, thread, "run-old", "completed", created_at=created_at)
    await _seed_run(session, thread, "run-new", "completed", created_at=created_at + timedelta(seconds=1))
    await session.commit()

    before = await list_threads(db=session, scope=SCOPE)
    assert before[0]["thread_status"] == "ready"
    viewed = await mark_thread_viewed(db=session, thread_id=thread.thread_id, scope=SCOPE)
    assert viewed["thread_status"] == "done"
    after = await list_threads(db=session, scope=SCOPE)
    assert after[0]["thread_status"] == "done"
    assert thread.last_viewed_run_id == "run-new"


async def test_viewed_mark_does_not_complete_active_run(session):
    """活动 Run 仍保持加载状态，不能被查看操作伪装为完成。"""
    thread = await _seed_thread(session, "thread-active")
    await _seed_run(session, thread, "run-active", "running")
    await session.commit()

    viewed = await mark_thread_viewed(db=session, thread_id=thread.thread_id, scope=SCOPE)
    snapshot = await get_thread_snapshot(db=session, scope=SCOPE, thread_id=thread.thread_id)
    assert viewed["thread_status"] == "loading"
    assert snapshot["current_turn"] == {
        "turn_id": "turn-run-active",
        "status": "running",
        "run_id": "run-active",
        "run_status": "running",
        "waitpoint": None,
        "result_run_id": None,
    }


async def test_public_archive_refuses_active_turn(session):
    """归档不能绕过尚未结束的 Turn。"""
    thread = await _seed_thread(session, "thread-active")
    await _seed_run(session, thread, "run-active", "running")
    await session.commit()

    with pytest.raises(HTTPException) as failure:
        await archive_thread(db=session, scope=SCOPE, thread_id=thread.thread_id)
    assert failure.value.status_code == 409
    assert thread.status == "active"


async def test_public_archive_refuses_pending_runtime_cleanup(session):
    """Turn 已完成时仍须等待顶层 Run 释放运行时。"""
    thread = await _seed_thread(session, "thread-cleanup")
    run = await _seed_run(session, thread, "run-cleanup", "completed")
    run.runtime_cleanup_pending = True
    await session.commit()

    with pytest.raises(HTTPException) as failure:
        await archive_thread(db=session, scope=SCOPE, thread_id=thread.thread_id)
    assert failure.value.status_code == 409
    assert thread.status == "active"


async def test_new_thread_creation_uses_unviewed_marker(session):
    """新建 Conversation 有专门的未读标记。"""
    conversation = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        title="new-thread",
        thread_id="thread-new",
        project_id="11111111-1111-4111-8111-111111111111",
    )
    assert conversation.last_viewed_run_id == UNVIEWED_RUN_MARKER


async def test_new_thread_cannot_seed_attachment_records(session):
    """客户端 metadata 不能伪造已经确认的附件。"""
    conversation = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-reserved-metadata",
        metadata={"attachments": [{"bucket_name": "private", "object_name": "secret"}]},
        project_id="22222222-2222-4222-8222-222222222222",
    )
    assert conversation.extra_metadata["attachments"] == []
