"""反馈写入按 Thread 与 APP 边界验证结果消息归属。"""

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.modules.agents.services.feedback import submit_message_feedback_view
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.models.threads import Conversation
from yuxi.modules.agents.models.messages import Message, MessageFeedback
from yuxi.modules.workspace.models import Project
from yuxi.modules.identity.models import User

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件在已迁移的隔离 PostgreSQL 上运行。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本文件不创建知识库资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本文件不创建沙盒。"""
    yield


async def test_feedback_rejects_message_from_another_app_thread():
    """已授权 Thread 不能作为同一用户另一 APP 消息的反馈跳板。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    uid = f"feedback-scope-{suffix}"
    project_id = f"project-{suffix}"
    first_thread = f"thread-a-{suffix}"
    second_thread = f"thread-b-{suffix}"
    message_id = None
    try:
        async with sessions() as db:
            db.add(User(username=uid, uid=uid, password_hash="test", role="user"))
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    uid=uid,
                    name="Project",
                    selection_status="selectable",
                    workdir_path=f"projects/{project_id}",
                    directory_mode="managed",
                )
            )
            await db.flush()
            first = Conversation(
                thread_id=first_thread, uid=uid, app_id="app-a", agent_id="main", project_id=project_id
            )
            second = Conversation(
                thread_id=second_thread, uid=uid, app_id="app-b", agent_id="main", project_id=project_id
            )
            db.add_all([first, second])
            await db.flush()
            message = Message(conversation_id=second.id, role="assistant", content="private answer")
            db.add(message)
            await db.flush()
            message_id = message.id
            await db.commit()

        async with sessions() as db:
            with pytest.raises(HTTPException) as exc_info:
                await submit_message_feedback_view(
                    message_id=message_id,
                    rating="like",
                    reason=None,
                    db=db,
                    current_uid=uid,
                    thread_id=first_thread,
                    app_id="app-a",
                )
            assert exc_info.value.status_code == 404
            assert await db.scalar(select(MessageFeedback).where(MessageFeedback.message_id == message_id)) is None
    finally:
        async with sessions() as db:
            if message_id is not None:
                await db.execute(delete(MessageFeedback).where(MessageFeedback.message_id == message_id))
                await db.execute(delete(Message).where(Message.id == message_id))
            await db.execute(delete(Conversation).where(Conversation.thread_id.in_((first_thread, second_thread))))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.uid == uid))
            await db.commit()
        await engine.dispose()


async def test_feedback_accepts_only_completed_turn_result_message(test_client, standard_user):
    """真实 HTTP 只允许给完成 Turn 明确指定的最终输出写反馈。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uid = standard_user["user"]["uid"]
    suffix = uuid.uuid4().hex
    project_id = f"feedback-project-{suffix}"
    thread_id = f"feedback-thread-{suffix}"
    turn_id = f"feedback-turn-{suffix}"
    prior_run_id = f"feedback-prior-{suffix}"
    result_run_id = f"feedback-result-{suffix}"
    message_ids = {}
    created = False
    try:
        async with sessions() as db:
            db.add(
                Project(
                    id=project_id,
                    uid=uid,
                    name="Feedback result test",
                    selection_status="implicit",
                    workdir_path=f"projects/{project_id}",
                    directory_mode="managed",
                )
            )
            await db.flush()
            conversation = Conversation(
                thread_id=thread_id,
                uid=uid,
                agent_id="main",
                project_id=project_id,
                status="active",
            )
            db.add(conversation)
            await db.flush()
            db.add(
                AgentTurn(
                    id=turn_id,
                    conversation_thread_id=thread_id,
                    uid=uid,
                    status="completed",
                    current_run_id=result_run_id,
                    result_run_id=result_run_id,
                )
            )
            await db.flush()
            db.add_all(
                [
                    AgentRun(
                        id=prior_run_id,
                        conversation_thread_id=thread_id,
                        runtime_scope_id=thread_id,
                        agent_slug="main",
                        uid=uid,
                        turn_id=turn_id,
                        conversation_id=conversation.id,
                        run_type="chat",
                        status="yielded",
                        input_payload={},
                    ),
                    AgentRun(
                        id=result_run_id,
                        conversation_thread_id=thread_id,
                        runtime_scope_id=thread_id,
                        agent_slug="main",
                        uid=uid,
                        turn_id=turn_id,
                        conversation_id=conversation.id,
                        run_type="resume",
                        resume_from_run_id=prior_run_id,
                        status="completed",
                        input_payload={},
                    ),
                ]
            )
            await db.flush()
            for name, role, message_type, run_id in (
                ("user", "user", "text", None),
                ("resume", "user", "text", result_run_id),
                ("audit", "assistant", "model_audit", result_run_id),
                ("prior", "assistant", "text", prior_run_id),
                ("result", "assistant", "text", result_run_id),
            ):
                message = Message(
                    conversation_id=conversation.id,
                    role=role,
                    content=name,
                    message_type=message_type,
                    run_id=run_id,
                    turn_id=turn_id,
                )
                db.add(message)
                await db.flush()
                message_ids[name] = message.id
            result_run = await db.get(AgentRun, result_run_id)
            result_run.output_message_id = message_ids["result"]
            await db.commit()
            created = True

        for name in ("user", "resume", "audit", "prior"):
            response = await test_client.post(
                f"/api/v1/agents/threads/{thread_id}/messages/{message_ids[name]}/feedback",
                headers=standard_user["headers"],
                json={"rating": "like"},
            )
            assert response.status_code == 404, (name, response.text)

        response = await test_client.post(
            f"/api/v1/agents/threads/{thread_id}/messages/{message_ids['result']}/feedback",
            headers=standard_user["headers"],
            json={"rating": "like"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["message_id"] == message_ids["result"]

        async with sessions() as db:
            rows = (
                await db.scalars(
                    select(MessageFeedback.message_id).where(MessageFeedback.message_id.in_(message_ids.values()))
                )
            ).all()
            assert rows == [message_ids["result"]]
    finally:
        if created:
            async with sessions() as db:
                await db.execute(delete(MessageFeedback).where(MessageFeedback.message_id.in_(message_ids.values())))
                await db.execute(delete(Message).where(Message.id.in_(message_ids.values())))
                await db.execute(delete(AgentRun).where(AgentRun.id.in_((prior_run_id, result_run_id))))
                await db.execute(delete(AgentTurn).where(AgentTurn.id == turn_id))
                await db.execute(delete(Conversation).where(Conversation.thread_id == thread_id))
                await db.execute(delete(Project).where(Project.id == project_id))
                await db.commit()
        await engine.dispose()
