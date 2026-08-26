"""用户定时 Agent 的真实 PostgreSQL 并发与历史语义。"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from yuxi.repositories.scheduled_agent_repository import ScheduledAgentRepository
from yuxi.services.scheduled_agent_service import _create_run_record, next_run_at
from yuxi.storage.postgres.models_business import Project, ScheduledAgentJob, ScheduledAgentRun, User
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_claim_concurrency_coalesce_and_soft_delete_history():
    """并发只领取一次，misfire 合并且软删除保留执行记录。"""
    database_url = os.environ["POSTGRES_URL"]
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = f"scheduled-user-{uuid.uuid4()}"
    project_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    now = utc_now_naive()

    try:
        async with session_factory() as db:
            db.add(User(username=uid, uid=uid, password_hash="test", role="user"))
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    uid=uid,
                    name="Scheduled Project",
                    selection_status="selectable",
                    workdir_path=f"projects/{project_id}",
                    directory_mode="managed",
                )
            )
            await db.flush()
            db.add(
                ScheduledAgentJob(
                    id=job_id,
                    uid=uid,
                    project_id=project_id,
                    agent_slug="chatbot",
                    name="Scheduled Job",
                    prompt="hello",
                    tool_approval_mode="always_trust",
                    cron_expression="* * * * *",
                    timezone="UTC",
                    enabled=True,
                    next_run_at=now - timedelta(minutes=10),
                )
            )
            await db.commit()

        async def claim():
            async with session_factory() as db:
                repo = ScheduledAgentRepository(db)
                job = await repo.claim_due_job(now=now)
                if job is None:
                    return None
                scheduled_for = job.next_run_at
                job.next_run_at = next_run_at(job.cron_expression, job.timezone, now)
                run = await _create_run_record(
                    repo=repo,
                    job=job,
                    trigger="scheduled",
                    occurrence_key=f"scheduled:{scheduled_for.isoformat()}",
                    scheduled_for=scheduled_for,
                )
                await db.commit()
                return run.id

        claimed = await asyncio.gather(claim(), claim())
        assert sum(item is not None for item in claimed) == 1

        async with session_factory() as db:
            repo = ScheduledAgentRepository(db)
            job = await repo.get_job(job_id, uid, lock=True)
            runs = list(
                (await db.execute(select(ScheduledAgentRun).where(ScheduledAgentRun.job_id == job_id))).scalars()
            )
            assert len(runs) == 1
            assert runs[0].thread_id
            assert job.next_run_at > now

            manual_now = utc_now_naive()
            manual = await _create_run_record(
                repo=repo,
                job=job,
                trigger="manual",
                occurrence_key=f"manual:{uuid.uuid4()}",
                scheduled_for=manual_now,
            )
            assert manual.status == "skipped"
            await repo.delete_job(job)
            await db.commit()
            manual_id = manual.id

        async with session_factory() as db:
            repo = ScheduledAgentRepository(db)
            assert await repo.get_job(job_id, uid) is None
            assert await repo.get_job(job_id, uid, include_deleted=True) is not None
            runs = await repo.list_runs(job_id, uid, 20)
            assert manual_id in {run.id for run in runs}
    finally:
        async with session_factory() as db:
            await db.execute(delete(ScheduledAgentRun).where(ScheduledAgentRun.job_id == job_id))
            await db.execute(delete(ScheduledAgentJob).where(ScheduledAgentJob.id == job_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.uid == uid))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_deleted_user_job_is_not_claimed():
    """软删除用户的任务不能继续产生后台副作用。"""
    database_url = os.environ["POSTGRES_URL"]
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = f"scheduled-deleted-{uuid.uuid4()}"
    project_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    try:
        async with session_factory() as db:
            db.add(User(username=uid, uid=uid, password_hash="test", role="user", is_deleted=1))
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    uid=uid,
                    name="Deleted User Project",
                    selection_status="selectable",
                    workdir_path=f"projects/{project_id}",
                    directory_mode="managed",
                )
            )
            await db.flush()
            db.add(
                ScheduledAgentJob(
                    id=job_id,
                    uid=uid,
                    project_id=project_id,
                    agent_slug="chatbot",
                    name="Deleted User Job",
                    prompt="hello",
                    tool_approval_mode="always_trust",
                    cron_expression="* * * * *",
                    timezone="UTC",
                    enabled=True,
                    next_run_at=utc_now_naive() - timedelta(minutes=1),
                )
            )
            await db.commit()

        async with session_factory() as db:
            assert await ScheduledAgentRepository(db).claim_due_job(now=utc_now_naive()) is None
    finally:
        async with session_factory() as db:
            await db.execute(delete(ScheduledAgentJob).where(ScheduledAgentJob.id == job_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.uid == uid))
            await db.commit()
        await engine.dispose()
