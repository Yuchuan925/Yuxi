"""AgentRun worker 职责。"""

from __future__ import annotations

from yuxi.infrastructure.postgres.schema import require_current_schema

import asyncio
from yuxi.modules.extensions.mcp.service import ensure_builtin_mcp_servers_in_db
from yuxi.modules.extensions.skills.shared import init_builtin_skills
from yuxi.modules.agents.services.scheduler import recover_pending_dispatches
from yuxi.modules.agents.services.transport import (
    RUN_RECONCILIATION_SECONDS,
    WORKER_RECONCILIATION_HEALTH_KEY,
    WORKER_RECONCILIATION_HEALTH_TTL_SECONDS,
    publish_worker_health,
)
from yuxi.modules.schedules.service import claim_and_dispatch_due_jobs, recover_scheduled_dispatches
from yuxi.modules.tasks.queue import (
    TASK_RECONCILIATION_HEALTH_KEY,
    TASK_RECONCILIATION_HEALTH_TTL_SECONDS,
    TASK_RECONCILIATION_SECONDS,
    reconcile_and_publish_tasks,
)
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.identity.security import AuthUtils
from yuxi.infrastructure.observability.logging import logger

from yuxi.modules.agents.services.leases import (
    WORKER_ID,
    reconcile_expired_run_leases,
    reconcile_pending_runtime_cleanups,
)


_RECONCILIATION_TASK_KEY = "agent_run_reconciliation_task"


_TASK_RECONCILIATION_TASK_KEY = "durable_task_reconciliation_task"


async def _reconcile_agent_run_leases_forever() -> None:
    """周期收敛失去 heartbeat 的 Run；多个 worker 并发执行仍由行锁保证单赢家。"""
    while True:
        await asyncio.sleep(RUN_RECONCILIATION_SECONDS)
        try:
            reconciled_ids = await reconcile_expired_run_leases()
            if reconciled_ids:
                logger.warning(f"Reconciled expired AgentRun leases: count={len(reconciled_ids)}")
            cleaned_ids = await reconcile_pending_runtime_cleanups()
            if cleaned_ids:
                logger.warning(f"Reconciled pending runtime cleanups: count={len(cleaned_ids)}")
            await recover_pending_dispatches()
            await recover_scheduled_dispatches()
            await claim_and_dispatch_due_jobs()
            await _publish_reconciliation_health()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Failed to reconcile expired AgentRun leases", exc_info=True)


async def _reconcile_durable_tasks_forever() -> None:
    """周期收敛失联通用 Task，并补发持久 pending 意图。"""
    while True:
        await asyncio.sleep(TASK_RECONCILIATION_SECONDS)
        try:
            reconciled = await reconcile_and_publish_tasks()
            if reconciled:
                logger.warning("Reconciled expired durable tasks: count=%s", len(reconciled))
            await _publish_task_reconciliation_health()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Failed to reconcile durable tasks", exc_info=True)


async def _publish_task_reconciliation_health() -> None:
    """续租 worker 的 Durable Task 收敛与 pending 补发能力。"""
    await publish_worker_health(
        TASK_RECONCILIATION_HEALTH_KEY,
        WORKER_ID,
        TASK_RECONCILIATION_HEALTH_TTL_SECONDS,
    )


async def _publish_reconciliation_health() -> None:
    """续租 worker 的 AgentRun lease 收敛能力；持续失败后 readiness 自动失效。"""

    await publish_worker_health(
        WORKER_RECONCILIATION_HEALTH_KEY,
        WORKER_ID,
        WORKER_RECONCILIATION_HEALTH_TTL_SECONDS,
    )


async def _worker_startup(ctx):
    """初始化 worker 依赖。"""

    if not isinstance(ctx, dict):
        raise TypeError("ARQ worker context 必须是字典")
    AuthUtils.require_security_secrets()
    ctx["worker_id"] = WORKER_ID
    pg_manager.initialize()
    await require_current_schema(pg_manager)
    async with pg_manager.get_async_session_context() as session:
        from yuxi.modules.system.options import ensure_options_in_db, invalidate_option_cache, system_options

        await ensure_options_in_db(session)
        await session.commit()
    await invalidate_option_cache(system_options.key)
    try:
        await ensure_builtin_mcp_servers_in_db()
    except Exception as exc:
        logger.error(
            "Optional worker component failed: component=builtin_mcp_servers, type=%s",
            type(exc).__name__,
        )
    async with pg_manager.get_async_session_context() as session:
        await init_builtin_skills(session)
    reconciled_ids = await reconcile_expired_run_leases()
    if reconciled_ids:
        logger.warning(f"Reconciled expired AgentRun leases at startup: count={len(reconciled_ids)}")
    await reconcile_pending_runtime_cleanups()
    await recover_pending_dispatches()
    await reconcile_and_publish_tasks()
    await _publish_task_reconciliation_health()
    await recover_scheduled_dispatches()
    await claim_and_dispatch_due_jobs()
    await _publish_reconciliation_health()
    ctx[_RECONCILIATION_TASK_KEY] = asyncio.create_task(_reconcile_agent_run_leases_forever())
    ctx[_TASK_RECONCILIATION_TASK_KEY] = asyncio.create_task(_reconcile_durable_tasks_forever())


async def _worker_shutdown(ctx):
    """关闭 worker 共享连接。"""

    if isinstance(ctx, dict):
        reconciliation_tasks = [
            ctx.pop(_RECONCILIATION_TASK_KEY, None),
            ctx.pop(_TASK_RECONCILIATION_TASK_KEY, None),
        ]
        reconciliation_tasks = [task for task in reconciliation_tasks if task is not None]
        for task in reconciliation_tasks:
            task.cancel()
        if reconciliation_tasks:
            await asyncio.gather(*reconciliation_tasks, return_exceptions=True)
    from yuxi.modules.agents.services.transport import close_queue_clients

    await close_queue_clients()
    await pg_manager.close()
