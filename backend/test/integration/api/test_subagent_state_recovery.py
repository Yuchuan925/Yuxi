"""通过真实 HTTP 验证子 Run 身份恢复和用户隔离。"""

from __future__ import annotations

import os
import uuid

import pytest

from yuxi.bootstrap.models import load_models
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.models.sessions import Session, SubagentThread
from yuxi.modules.workspace.models import Project
from yuxi.shared.datetime import utc_now

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_state_recovers_children_without_checkpoint_and_rejects_other_user(
    test_client, standard_user, admin_headers
):
    """创建已提交而父 checkpoint 从未写入的子 Run，页面仍能发现并读到终态。"""
    uid = standard_user["user"]["uid"]
    project_id = str(uuid.uuid4())
    parent_thread, child_thread = (f"pytest-subagent-state-{uuid.uuid4()}" for _ in range(2))
    parent_id, child_id = (str(uuid.uuid4()) for _ in range(2))
    turn_id, child_turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    load_models()
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
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
            parent = Session(
                thread_id=parent_thread, uid=uid, project_id=project_id, agent_id="state-probe", status="active"
            )
            child = Session(
                thread_id=child_thread, uid=uid, project_id=project_id, agent_id="state-probe-child", status="subagent"
            )
            db.add_all([parent, child])
            await db.flush()
            db.add(
                AgentTurn(
                    id=turn_id,
                    thread_id=parent_thread,
                    uid=uid,
                    status="completed",
                    current_run_id=parent_id,
                    result_run_id=parent_id,
                )
            )
            await db.flush()
            db.add(
                AgentRun(
                    id=parent_id,
                    uid=uid,
                    agent_slug="state-probe",
                    run_type="chat",
                    session_record_id=parent.id,
                    thread_id=parent_thread,
                    runtime_scope_id=parent_thread,
                    turn_id=turn_id,
                    status="completed",
                    finished_at=utc_now(),
                    input_payload={},
                )
            )
            await db.flush()
            relation = SubagentThread(
                uid=uid,
                parent_session_record_id=parent.id,
                child_session_record_id=child.id,
                child_thread_id=child_thread,
                subagent_slug="state-probe-child",
                created_by_run_id=parent_id,
            )
            db.add(relation)
            await db.flush()
            db.add(
                AgentTurn(
                    id=child_turn_id,
                    thread_id=child_thread,
                    uid=uid,
                    status="completed",
                    current_run_id=child_id,
                    result_run_id=child_id,
                )
            )
            await db.flush()
            db.add(
                AgentRun(
                    id=child_id,
                    uid=uid,
                    agent_slug="state-probe-child",
                    run_type="subagent",
                    session_record_id=child.id,
                    thread_id=child_thread,
                    runtime_scope_id=child_thread,
                    turn_id=child_turn_id,
                    status="completed",
                    finished_at=utc_now(),
                    created_by_run_id=parent_id,
                    subagent_thread_relation_id=relation.id,
                    input_payload={"runtime": {"tool_call_id": "start-probe"}},
                )
            )
            await db.commit()

        response = await test_client.get(
            f"/api/v1/agents/threads/{parent_thread}/state", headers=standard_user["headers"]
        )
        assert response.status_code == 200, response.text
        runs = response.json()["agent_state"]["subagent_runs"]
        assert len(runs) == 1, response.json()
        assert runs[0]["run_id"] == child_id
        assert runs[0]["status"] == "completed"
        assert runs[0]["events_url"] == f"/api/v1/agents/threads/{child_thread}/events"
        child_run_url = f"/api/v1/agents/threads/{child_thread}/runs/{child_id}"
        run_response = await test_client.get(child_run_url, headers=standard_user["headers"])
        assert run_response.status_code == 200, run_response.text
        assert run_response.json()["status"] == "completed"
        async with sessions() as db:
            persisted = await db.get(AgentRun, child_id)
            assert persisted is not None and persisted.turn_id == child_turn_id
            assert persisted.created_by_run_id == parent_id
        for url in (f"/api/v1/agents/threads/{parent_thread}/state", child_run_url):
            denied = await test_client.get(url, headers=admin_headers)
            assert denied.status_code == 404, denied.text
            assert child_id not in denied.text
    finally:
        async with sessions() as db:
            await db.execute(
                update(AgentTurn)
                .where(AgentTurn.id.in_([turn_id, child_turn_id]))
                .values(current_run_id=None, result_run_id=None)
            )
            await db.execute(delete(AgentRun).where(AgentRun.id == child_id))
            await db.execute(delete(SubagentThread).where(SubagentThread.child_thread_id == child_thread))
            await db.execute(delete(AgentRun).where(AgentRun.id == parent_id))
            await db.execute(delete(AgentTurn).where(AgentTurn.id.in_([turn_id, child_turn_id])))
            await db.execute(delete(Session).where(Session.thread_id.in_([parent_thread, child_thread])))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.commit()
        await engine.dispose()
