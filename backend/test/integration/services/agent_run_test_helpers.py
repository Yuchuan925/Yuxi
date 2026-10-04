"""AgentRun 集成测试共享的数据工厂。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.input_receipt import AgentInputReceiptRepository
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.workspace.models import Project
from yuxi.modules.identity.models import User
from yuxi.shared.datetime import utc_now


async def create_agent_run(
    session_factory,
    *,
    prefix: str,
    message_content: str,
    input_payload: dict[str, Any],
    status: str = "pending",
    worker_id: str | None = None,
    lease_expires_at: datetime | None = None,
) -> tuple[str, str, int]:
    """创建供 AgentRun 集成测试使用的最小持久化链路。"""
    run_id = str(uuid.uuid4())
    input_id = f"{prefix}-{uuid.uuid4()}"
    turn_id = f"{prefix}-{uuid.uuid4()}"
    thread_id = f"pytest-{prefix}-{uuid.uuid4()}"
    uid = f"pytest-user-{uuid.uuid4()}"
    project_id = str(uuid.uuid4())

    async with session_factory() as db:
        db.add(User(username=uid, uid=uid, password_hash="test"))
        await db.flush()
        db.add(
            Project(
                id=project_id,
                uid=uid,
                selection_status="implicit",
                workdir_path=f"projects/{project_id}",
                directory_mode="managed",
            )
        )
        await db.flush()
        agent_session = Session(
            thread_id=thread_id,
            uid=uid,
            project_id=project_id,
            agent_id="main",
            status="active",
        )
        db.add(agent_session)
        await db.flush()
        input_repo = AgentInputRepository(db)
        await input_repo.create(
            input_id=input_id,
            thread_id=thread_id,
            uid=uid,
            app_id=None,
            agent_slug="main",
            kind="follow_up",
            input_payload=dict(input_payload),
        )
        receipt = await AgentInputReceiptRepository(db).create(
            receipt_id=str(uuid.uuid4()),
            idempotency_key=str(uuid.uuid4()),
            uid=uid,
            app_id=None,
            thread_id=thread_id,
            event_type="message",
            intent_hash="test-intent",
            input_id=input_id,
        )
        message = Message(
            session_record_id=agent_session.id,
            role="user",
            content=message_content,
            delivery_status="queued",
        )
        db.add(message)
        await db.flush()
        await input_repo.add_messages(input_id=input_id, receipt_id=receipt.id, message_ids=[message.id])
        turn = AgentTurn(id=turn_id, thread_id=thread_id, uid=uid, app_id=None, status="running")
        db.add(turn)
        await db.flush()
        db.add(
            AgentRun(
                id=run_id,
                thread_id=thread_id,
                runtime_scope_id=thread_id,
                agent_slug="main",
                uid=uid,
                turn_id=turn_id,
                input_id=input_id,
                session_record_id=agent_session.id,
                input_message_id=message.id,
                input_payload=dict(input_payload),
                status=status,
                run_type="chat",
                worker_id=worker_id,
                heartbeat_at=utc_now() if worker_id else None,
                lease_expires_at=lease_expires_at,
            )
        )
        await db.flush()
        turn.current_run_id = run_id
        await input_repo.consume(
            input_id=input_id,
            turn_id=turn_id,
            run_id=run_id,
            cutoff_seq=receipt.receive_seq,
        )
        await db.commit()
        return run_id, thread_id, message.id
