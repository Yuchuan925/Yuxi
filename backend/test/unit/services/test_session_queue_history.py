"""Public Thread 历史按 Input、Turn 和 Run 明确归属。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.modules.agents.services.messages import get_thread_history
from yuxi.modules.agents.services.scope import ActorScope
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.models.messages import Message, ToolCall
from yuxi.modules.workspace.models import Project

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
SCOPE = ActorScope(uid="user-1", app_id=None)
STARTED_AT = datetime(2026, 9, 29, 9, 0, 0)


@pytest_asyncio.fixture()
async def session():
    """为真实 ORM 查询建立独立内存数据库。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Project(
                id="project-thread-1",
                uid="user-1",
                selection_status="implicit",
                directory_mode="managed",
                workdir_path="projects/project-thread-1",
            )
        )
        db.add(
            Session(
                id=1,
                thread_id="thread-1",
                project_id="project-thread-1",
                uid="user-1",
                agent_id="main",
                status="active",
            )
        )
        await db.commit()
        yield db
    await engine.dispose()


def _turn_run(*, turn_id: str, run_id: str, created_at: datetime) -> tuple[AgentTurn, AgentRun]:
    """建立一轮已完成的顶层执行。"""
    turn = AgentTurn(
        id=turn_id,
        thread_id="thread-1",
        uid="user-1",
        status="completed",
        current_run_id=run_id,
        result_run_id=run_id,
    )
    run = AgentRun(
        id=run_id,
        turn_id=turn_id,
        thread_id="thread-1",
        runtime_scope_id="thread-1",
        agent_slug="main",
        uid="user-1",
        session_record_id=1,
        run_type="chat",
        input_payload={},
        status="completed",
        created_at=created_at,
    )
    return turn, run


async def test_history_shows_pending_input_and_later_binds_its_own_reply(session):
    """排队消息可见；领取后仅与其自身 Turn/Run 和回复关联。"""
    turn_a, run_a = _turn_run(turn_id="turn-a", run_id="run-a", created_at=STARTED_AT)
    session.add_all([turn_a, run_a])
    session.add(
        AgentInput(
            id="input-b",
            received_seq=2,
            thread_id="thread-1",
            uid="user-1",
            agent_slug="main",
            kind="follow_up",
            status="pending",
            input_payload={},
            source="chat",
            channel="web",
            origin_metadata={},
        )
    )
    session.add_all(
        [
            Message(
                id=1,
                session_record_id=1,
                role="user",
                content="A",
                turn_id="turn-a",
                run_id="run-a",
                delivery_status="dispatched",
                created_at=STARTED_AT,
            ),
            Message(
                id=2,
                session_record_id=1,
                role="assistant",
                content="A reply",
                turn_id="turn-a",
                run_id="run-a",
                delivery_status="complete",
                created_at=STARTED_AT + timedelta(seconds=1),
            ),
            Message(
                id=3,
                session_record_id=1,
                role="user",
                content="B",
                delivery_status="queued",
                extra_metadata={"input_id": "input-b"},
                created_at=STARTED_AT + timedelta(seconds=2),
            ),
        ]
    )
    await _register_visible(session)
    await session.commit()

    pending = await get_thread_history(db=session, scope=SCOPE, thread_id="thread-1")
    assert [item["content"][0]["text"] for item in pending["items"]] == ["A", "A reply", "B"]
    assert pending["items"][-1]["yuxi"]["run_id"] is None
    assert pending["thread"]["queued_input_count"] == 1

    turn_b, run_b = _turn_run(turn_id="turn-b", run_id="run-b", created_at=STARTED_AT + timedelta(seconds=3))
    run_b.input_id = "input-b"
    session.add_all([turn_b, run_b])
    queued_message = await session.get(Message, 3)
    queued_message.turn_id = "turn-b"
    queued_message.run_id = "run-b"
    queued_message.delivery_status = "dispatched"
    input_b = await session.get(AgentInput, "input-b")
    input_b.status = "consumed"
    input_b.turn_id = "turn-b"
    input_b.consumed_run_id = "run-b"
    input_b.cutoff_seq = 2
    input_b.consumed_at = STARTED_AT + timedelta(seconds=3)
    session.add(
        Message(
            id=4,
            session_record_id=1,
            role="assistant",
            content="B reply",
            turn_id="turn-b",
            run_id="run-b",
            delivery_status="complete",
            created_at=STARTED_AT + timedelta(seconds=4),
        )
    )
    await _register_visible(session)
    await session.commit()

    completed = await get_thread_history(db=session, scope=SCOPE, thread_id="thread-1")
    assert [item["content"][0]["text"] for item in completed["items"]] == ["A", "A reply", "B", "B reply"]
    assert [(item["turn_id"], item["yuxi"]["run_id"]) for item in completed["items"]] == [
        ("turn-a", "run-a"),
        ("turn-a", "run-a"),
        ("turn-b", "run-b"),
        ("turn-b", "run-b"),
    ]
    assert completed["thread"]["queued_input_count"] == 0
    assert [(item["turn_id"], item["run_id"]) for item in completed["runs"]] == [
        ("turn-a", "run-a"),
        ("turn-b", "run-b"),
    ]


async def test_history_exposes_tool_result_without_internal_model_metadata(session):
    """历史消息不泄露模型运行内部 metadata。"""
    turn, run = _turn_run(turn_id="turn-a", run_id="run-a", created_at=STARTED_AT)
    session.add_all([turn, run])
    answer = Message(
        session_record_id=1,
        role="assistant",
        content="answer",
        turn_id="turn-a",
        run_id="run-a",
        message_type="text",
        operation_id="model-a",
        delivery_status="complete",
        extra_metadata={
            "model_run_id": "private-model-run",
            "start_metadata": {"provider": "private"},
            "finish_metadata": {"model_name": "private"},
            "langfuse_trace_id": "trace-safe",
        },
    )
    session.add(answer)
    await session.flush()
    session.add(
        ToolCall(
            message_id=answer.id,
            langgraph_tool_call_id="call-a",
            tool_name="search",
            tool_input={"q": "Yuxi"},
            tool_output="safe result",
            status="success",
        )
    )
    await _register_visible(session)
    await session.commit()

    history = await get_thread_history(db=session, scope=SCOPE, thread_id="thread-1")
    assert [item["type"] for item in history["items"]] == ["message", "function_call", "function_call_output"]
    assert history["items"][1]["call_id"] == history["items"][2]["call_id"] == "call-a"
    assert history["items"][2]["output"] == "safe result"
    assert "private-model-run" not in str(history)


async def test_history_rejects_other_app_scope(session):
    """相同用户的另一 APP 也不能读取 Thread 历史。"""
    agent_session = await session.get(Session, 1)
    agent_session.app_id = "other-app"
    await session.commit()
    with pytest.raises(HTTPException) as failure:
        await get_thread_history(db=session, scope=SCOPE, thread_id="thread-1")
    assert failure.value.status_code == 404


async def _register_visible(db):
    """历史 oracle 显式登记已经发送过的 item，不由待测 serializer 生成。"""
    from sqlalchemy import select

    messages = (await db.scalars(select(Message).where(Message.role == "assistant"))).all()
    for message in messages:
        public = {
            "message": {
                "id": f"visible-{message.id}",
                "type": "message",
                "role": "assistant",
                "turn_id": message.turn_id,
                "content": [{"type": "output_text", "text": message.content}],
                "status": "completed",
                "phase": "commentary",
                "yuxi": {"run_id": message.run_id, "output_index": 0},
            }
        }
        if message.operation_id:
            public["call"] = {
                "id": "call-item",
                "type": "function_call",
                "turn_id": message.turn_id,
                "call_id": "call-a",
                "name": "search",
                "arguments": {"q": "Yuxi"},
                "status": "completed",
                "yuxi": {"run_id": message.run_id, "output_index": 1},
            }
            public["result"] = {
                "id": "result-item",
                "type": "function_call_output",
                "turn_id": message.turn_id,
                "call_id": "call-a",
                "output": "safe result",
                "error": None,
                "status": "completed",
                "yuxi": {"run_id": message.run_id, "output_index": 2},
            }
        message.extra_metadata = {**(message.extra_metadata or {}), "public_items": public}
