"""用户 Agent 定时任务的 PostgreSQL 访问边界。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ScheduledAgentJob, ScheduledAgentRun


class ScheduledAgentRepository:
    """读写用户拥有的定时任务和触发记录。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_jobs(self, uid: str) -> list[ScheduledAgentJob]:
        result = await self.db.execute(
            select(ScheduledAgentJob)
            .where(ScheduledAgentJob.uid == str(uid))
            .order_by(ScheduledAgentJob.created_at.desc(), ScheduledAgentJob.id.desc())
        )
        return list(result.scalars().all())

    async def get_job(self, job_id: str, uid: str, *, lock: bool = False) -> ScheduledAgentJob | None:
        stmt = select(ScheduledAgentJob).where(
            ScheduledAgentJob.id == job_id,
            ScheduledAgentJob.uid == str(uid),
        )
        if lock:
            stmt = stmt.with_for_update()
        return await self.db.scalar(stmt)

    async def add_job(self, job: ScheduledAgentJob) -> ScheduledAgentJob:
        self.db.add(job)
        await self.db.flush()
        return job

    async def list_runs(self, job_id: str, uid: str, limit: int) -> list[ScheduledAgentRun]:
        result = await self.db.execute(
            select(ScheduledAgentRun)
            .join(ScheduledAgentJob, ScheduledAgentJob.id == ScheduledAgentRun.job_id)
            .where(
                ScheduledAgentRun.job_id == job_id,
                ScheduledAgentJob.uid == str(uid),
            )
            .order_by(ScheduledAgentRun.scheduled_for.desc(), ScheduledAgentRun.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_run(self, run_id: str, uid: str) -> ScheduledAgentRun | None:
        return await self.db.scalar(
            select(ScheduledAgentRun)
            .join(ScheduledAgentJob, ScheduledAgentJob.id == ScheduledAgentRun.job_id)
            .where(ScheduledAgentRun.id == run_id, ScheduledAgentJob.uid == str(uid))
        )

    async def claim_due_job(self, *, now: datetime) -> ScheduledAgentRun | None:
        """锁定一个到期任务，推进下一次时间并创建唯一触发意图。"""
        job = await self.db.scalar(
            select(ScheduledAgentJob)
            .where(ScheduledAgentJob.enabled.is_(True), ScheduledAgentJob.next_run_at <= now)
            .order_by(ScheduledAgentJob.next_run_at.asc(), ScheduledAgentJob.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None

        from yuxi.services.scheduled_agent_service import build_request_id, next_run_at

        scheduled_for = job.next_run_at
        job.next_run_at = next_run_at(job.cron_expression, job.timezone, scheduled_for)
        job.updated_at = now
        scheduled_run = ScheduledAgentRun(
            id=build_request_id("scheduled-run", f"{job.id}:{scheduled_for.isoformat()}"),
            job_id=job.id,
            request_id=build_request_id("scheduled-request", f"{job.id}:{scheduled_for.isoformat()}"),
            scheduled_for=scheduled_for,
            prompt=job.prompt,
            status="dispatching",
        )
        self.db.add(scheduled_run)
        await self.db.flush()
        return scheduled_run

    async def list_dispatching_runs(self, *, before: datetime, limit: int = 100) -> list[ScheduledAgentRun]:
        result = await self.db.execute(
            select(ScheduledAgentRun)
            .where(
                ScheduledAgentRun.status == "dispatching",
                ScheduledAgentRun.created_at <= before,
            )
            .order_by(ScheduledAgentRun.created_at.asc(), ScheduledAgentRun.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def save(self) -> None:
        await self.db.commit()

    async def delete_job(self, job: ScheduledAgentJob) -> None:
        await self.db.delete(job)
        await self.db.flush()
