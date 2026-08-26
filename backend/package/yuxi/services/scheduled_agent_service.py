"""用户 Agent 定时任务的用例、校验和 worker 调度。"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.buildin import agent_manager
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_request_repository import AgentRunRequestRepository
from yuxi.repositories.scheduled_agent_repository import ScheduledAgentRepository
from yuxi.services.agent_run_service import get_agent_run_result
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.services.run_submission_service import RunOrigin, RunSubmissionCommand, submit_run_command
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import ScheduledAgentJob, ScheduledAgentRun, User
from yuxi.utils.datetime_utils import utc_now_naive

SCHEDULED_AGENT_SOURCE = "scheduled_agent"
MAX_JOBS_PER_USER = 50
MAX_PROMPT_LENGTH = 32_000
MAX_NAME_LENGTH = 255


def build_request_id(prefix: str, value: str) -> str:
    """为调度对象生成稳定、长度受限的 ID。"""
    return f"{prefix[:16]}-{hashlib.sha256(value.encode()).hexdigest()[:47]}"


def validate_schedule(cron_expression: str, timezone: str) -> tuple[str, str]:
    """校验 cron 表达式和 IANA 时区。"""
    expression = str(cron_expression or "").strip()
    if not expression:
        raise HTTPException(status_code=422, detail="cron_expression 不能为空")
    try:
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="cron_expression 不是有效的 5 段 cron 表达式") from None
    zone = str(timezone or "").strip()
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="timezone 必须是有效的 IANA 时区") from None
    return expression, zone


def next_run_at(cron_expression: str, timezone: str, after: datetime) -> datetime:
    """计算下一次 UTC 触发时间，数据库统一保存无时区 UTC。"""
    zone = ZoneInfo(timezone)
    local_after = after.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
    next_local = croniter(cron_expression, local_after).get_next(datetime)
    return next_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _normalize_text(value: str, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field} 不能为空")
    if len(normalized) > maximum:
        raise HTTPException(status_code=422, detail=f"{field} 不能超过 {maximum} 个字符")
    return normalized


async def _validate_agent(agent_slug: str, user: User, db: AsyncSession):
    repo = AgentRepository(db)
    agent = await repo.get_visible_by_slug(slug=agent_slug, user=user, kind="main")
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在或不可访问")
    if not agent_manager.get_agent(agent.backend_id):
        raise HTTPException(status_code=404, detail="智能体后端不存在")
    return agent


def _serialize_job(job: ScheduledAgentJob) -> dict:
    return job.to_dict()


async def list_scheduled_jobs(*, user: User, db: AsyncSession) -> dict:
    """列出当前用户自己的定时任务。"""
    repo = ScheduledAgentRepository(db)
    jobs = await repo.list_jobs(str(user.uid))
    result = []
    for job in jobs:
        item = _serialize_job(job)
        runs = await repo.list_runs(job.id, str(user.uid), 20)
        item["runs"] = [run.to_dict() for run in runs]
        result.append(item)
    return {"jobs": result}


async def get_scheduled_job(*, job_id: str, user: User, db: AsyncSession) -> dict | None:
    """读取当前用户拥有的定时任务及其触发记录。"""
    repo = ScheduledAgentRepository(db)
    job = await repo.get_job(job_id, str(user.uid))
    if not job:
        return None
    data = _serialize_job(job)
    data["runs"] = [run.to_dict() for run in await repo.list_runs(job.id, str(user.uid), 20)]
    return data


async def create_scheduled_job(*, user: User, db: AsyncSession, data: dict) -> dict:
    """校验并创建用户定时任务。"""
    repo = ScheduledAgentRepository(db)
    if len(await repo.list_jobs(str(user.uid))) >= MAX_JOBS_PER_USER:
        raise HTTPException(status_code=409, detail=f"每个用户最多创建 {MAX_JOBS_PER_USER} 个定时任务")
    agent_slug = _normalize_text(data.get("agent_slug"), "agent_slug", 64)
    await _validate_agent(agent_slug, user, db)
    name = _normalize_text(data.get("name"), "name", MAX_NAME_LENGTH)
    prompt = _normalize_text(data.get("prompt"), "prompt", MAX_PROMPT_LENGTH)
    expression, timezone = validate_schedule(data.get("cron_expression"), data.get("timezone"))
    now = utc_now_naive()
    job = ScheduledAgentJob(
        id=str(uuid.uuid4()),
        uid=str(user.uid),
        agent_slug=agent_slug,
        thread_id=str(uuid.uuid4()),
        name=name,
        prompt=prompt,
        cron_expression=expression,
        timezone=timezone,
        enabled=bool(data.get("enabled", True)),
        next_run_at=next_run_at(expression, timezone, now),
        created_at=now,
        updated_at=now,
    )
    await repo.add_job(job)
    await db.commit()
    return _serialize_job(job)


async def update_scheduled_job(*, job_id: str, user: User, db: AsyncSession, data: dict) -> dict | None:
    """更新当前用户拥有的任务；修改计划时从当前时刻重新计算下一次触发。"""
    repo = ScheduledAgentRepository(db)
    job = await repo.get_job(job_id, str(user.uid), lock=True)
    if not job:
        return None
    if "agent_slug" in data:
        agent_slug = _normalize_text(data["agent_slug"], "agent_slug", 64)
        await _validate_agent(agent_slug, user, db)
        job.agent_slug = agent_slug
    if "name" in data:
        job.name = _normalize_text(data["name"], "name", MAX_NAME_LENGTH)
    if "prompt" in data:
        job.prompt = _normalize_text(data["prompt"], "prompt", MAX_PROMPT_LENGTH)
    expression = data.get("cron_expression", job.cron_expression)
    timezone = data.get("timezone", job.timezone)
    expression, timezone = validate_schedule(expression, timezone)
    if expression != job.cron_expression or timezone != job.timezone:
        job.next_run_at = next_run_at(expression, timezone, utc_now_naive())
    job.cron_expression, job.timezone = expression, timezone
    if "enabled" in data:
        job.enabled = bool(data["enabled"])
    job.updated_at = utc_now_naive()
    await db.commit()
    return _serialize_job(job)


async def delete_scheduled_job(*, job_id: str, user: User, db: AsyncSession) -> bool:
    """删除任务定义；数据库级联删除触发索引，但不删除 AgentRun。"""
    repo = ScheduledAgentRepository(db)
    job = await repo.get_job(job_id, str(user.uid), lock=True)
    if not job:
        return False
    await repo.delete_job(job)
    await db.commit()
    return True


async def dispatch_scheduled_run(*, scheduled_run_id: str) -> None:
    """将已 claim 的触发记录转成统一 AgentRun 请求。"""
    async with pg_manager.get_async_session_context() as db:
        scheduled_run = await db.get(ScheduledAgentRun, scheduled_run_id)
        if scheduled_run is None or scheduled_run.status != "dispatching":
            return
        job = await db.get(ScheduledAgentJob, scheduled_run.job_id)
        user = await db.scalar(select(User).where(User.uid == job.uid)) if job else None
        if job is None or user is None:
            scheduled_run.status = "cancelled"
            scheduled_run.error_message = "任务已删除、停用或用户不存在"
            scheduled_run.completed_at = utc_now_naive()
            await db.commit()
            return
        await _validate_agent(job.agent_slug, user, db)
        if not job.enabled:
            scheduled_run.status = "cancelled"
            scheduled_run.error_message = "任务已停用"
            scheduled_run.completed_at = utc_now_naive()
            await db.commit()
            return
        result = await submit_run_command(
            command=RunSubmissionCommand(
                agent_slug=job.agent_slug,
                thread_id=job.thread_id,
                request_id=scheduled_run.request_id,
                input_message=build_chat_input_message(scheduled_run.prompt),
                origin=RunOrigin(
                    source=SCHEDULED_AGENT_SOURCE,
                    channel="worker",
                    external_id=scheduled_run.id,
                    metadata={"scheduled_job_id": job.id, "scheduled_run_id": scheduled_run.id},
                ),
                request_metadata={"scheduled_job_id": job.id, "scheduled_run_id": scheduled_run.id},
                queue_policy="enqueue",
                create_conversation=True,
                conversation_title=job.name,
            ),
            current_user=user,
            db=db,
        )
        scheduled_run.run_id = result.get("run_id")
        scheduled_run.status = "dispatched" if result.get("run_id") else "queued"
        job.last_run_at = scheduled_run.scheduled_for
        job.updated_at = utc_now_naive()
        await db.commit()


async def recover_scheduled_dispatches(*, limit: int = 100) -> int:
    """恢复 worker 中断后遗留的定时触发意图。"""
    from datetime import timedelta

    async with pg_manager.get_async_session_context() as db:
        records = await ScheduledAgentRepository(db).list_dispatching_runs(
            before=utc_now_naive() - timedelta(seconds=30),
            limit=limit,
        )
    for record in records:
        try:
            await dispatch_scheduled_run(scheduled_run_id=record.id)
        except Exception:
            continue
    return len(records)


async def claim_and_dispatch_due_jobs(*, limit: int = 20) -> int:
    """批量领取到期任务并提交对应 AgentRun。"""
    count = 0
    for _ in range(max(0, limit)):
        async with pg_manager.get_async_session_context() as db:
            run = await ScheduledAgentRepository(db).claim_due_job(now=utc_now_naive())
            if run is None:
                break
            await db.commit()
            run_id = run.id
        try:
            await dispatch_scheduled_run(scheduled_run_id=run_id)
        except Exception as exc:
            async with pg_manager.get_async_session_context() as db:
                failed = await db.get(ScheduledAgentRun, run_id)
                if failed and failed.status == "dispatching":
                    failed.status = "failed"
                    failed.error_message = str(exc)
                    failed.completed_at = utc_now_naive()
                    await db.commit()
        count += 1
    return count


async def reconcile_scheduled_run_results(*, limit: int = 100) -> int:
    """把已完成 AgentRun 的结果投影回定时触发记录。"""
    async with pg_manager.get_async_session_context() as db:
        rows = await db.execute(
            select(ScheduledAgentRun, ScheduledAgentJob)
            .join(ScheduledAgentJob, ScheduledAgentJob.id == ScheduledAgentRun.job_id)
            .where(ScheduledAgentRun.status.in_({"queued", "dispatched"}))
            .order_by(ScheduledAgentRun.created_at.asc())
            .limit(limit)
        )
        records = list(rows.all())
    changed = 0
    for scheduled_run, job in records:
        run_id = scheduled_run.run_id
        if not run_id:
            async with pg_manager.get_async_session_context() as db:
                request = await AgentRunRequestRepository(db).get_by_request_id(scheduled_run.request_id)
                run_id = request.dispatched_run_id if request else None
                if run_id:
                    current = await db.get(ScheduledAgentRun, scheduled_run.id)
                    if current and current.status == "queued":
                        current.status = "dispatched"
                        current.run_id = run_id
                        await db.commit()
        result = await _load_run_result(run_id, job.uid)
        if result is None or result.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            continue
        async with pg_manager.get_async_session_context() as db:
            current = await db.get(ScheduledAgentRun, scheduled_run.id)
            if current and current.status in {"queued", "dispatched"}:
                current.status = str(result["status"])
                current.error_message = (result.get("error") or {}).get("message")
                current.completed_at = utc_now_naive()
                await db.commit()
                changed += 1
    return changed


async def _load_run_result(run_id: str | None, uid: str) -> dict | None:
    if not run_id:
        return None
    async with pg_manager.get_async_session_context() as db:
        return await get_agent_run_result(run_id=run_id, current_uid=uid, db=db)


__all__ = [
    "SCHEDULED_AGENT_SOURCE",
    "build_request_id",
    "claim_and_dispatch_due_jobs",
    "create_scheduled_job",
    "delete_scheduled_job",
    "dispatch_scheduled_run",
    "get_scheduled_job",
    "list_scheduled_jobs",
    "next_run_at",
    "reconcile_scheduled_run_results",
    "recover_scheduled_dispatches",
    "update_scheduled_job",
    "validate_schedule",
]
