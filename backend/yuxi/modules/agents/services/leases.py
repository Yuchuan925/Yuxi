"""AgentRun worker 职责。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select, text

from yuxi.infrastructure.observability.logging import logger
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.threads import Conversation
from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.runs import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.runtime.sandbox.provider import get_sandbox_provider
from yuxi.modules.agents.services.event_writer import publish_run_settlement
from yuxi.modules.agents.services.scheduler import dispatch_next_input
from yuxi.modules.agents.services.tracing import finish_turn_observation_if_terminal
from yuxi.modules.agents.services.transport import (
    publish_cancel_signals,
)
from yuxi.modules.workspace.services.bindings import (
    resolve_conversation_workdir_path,
)

RUN_LEASE_SECONDS = 120


WORKER_ID = f"worker-{uuid.uuid4().hex}"


async def release_runtime_if_idle(run: AgentRun) -> bool:
    """在 PostgreSQL cleanup fence 内串行销毁根 execution runtime。"""
    runtime_scope_id = str(getattr(run, "runtime_scope_id", None) or run.conversation_thread_id)
    async with pg_manager.get_async_session_context() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"yuxi-runtime-cleanup:{run.uid}:{runtime_scope_id}"},
        )
        current = await db.scalar(select(AgentRun).where(AgentRun.id == run.id).with_for_update())
        if current is None:
            raise RuntimeError(f"Run {run.id} 不存在，不能确认 runtime cleanup Owner")
        if not current.runtime_cleanup_pending:
            return True
        result = await db.execute(
            select(AgentRun.id)
            .where(
                AgentRun.runtime_scope_id == runtime_scope_id,
                AgentRun.id != current.id,
                AgentRun.status.notin_(TERMINAL_RUN_STATUSES),
            )
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return False
        conversation = await db.scalar(select(Conversation).where(Conversation.id == current.conversation_id))
        if conversation is None or conversation.uid != str(current.uid):
            raise RuntimeError(f"Run {run.id} 的 Conversation 身份不一致")
        workdir_path = await resolve_conversation_workdir_path(
            conversation=conversation,
            uid=str(current.uid),
            db=db,
        )
        await asyncio.to_thread(
            get_sandbox_provider().release,
            runtime_scope_id,
            uid=str(current.uid),
            clear_cache_on_delete_failure=True,
            workdir_path=workdir_path,
        )
        current.runtime_cleanup_pending = False
        await db.flush()
    return True


async def mark_run_running(run_id: str, worker_id: str) -> bool:
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        _, acquired = await repo.mark_running(
            run_id,
            worker_id=worker_id,
            lease_seconds=RUN_LEASE_SECONDS,
        )
        return acquired


async def renew_run_lease(run_id: str, worker_id: str) -> bool:
    """在独立事务中续租；owner 或 lease 已失效时返回 False。"""
    async with pg_manager.get_async_session_context() as db:
        return await AgentRunRepository(db).renew_lease(
            run_id,
            worker_id=worker_id,
            lease_seconds=RUN_LEASE_SECONDS,
        )


async def run_attempt_finished(run_id: str, worker_id: str) -> bool:
    """确认终态由当前最后一次 attempt 提交，不能把其他 Owner 的终态当作成功收尾。"""
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        run = await repo.get_run(run_id)
        if run is None or run.status not in TERMINAL_RUN_STATUSES:
            return False
        attempts = await repo.list_run_attempts(run_id)
        if not attempts:
            return False
        attempt = attempts[-1]
        return (
            attempt.worker_id == worker_id
            and attempt.outcome == run.status
            and attempt.finished_at is not None
            and attempt.finished_at == run.finished_at
        )


async def release_run_lease_for_retry(run_id: str, worker_id: str) -> bool:
    """释放当前 attempt 的 lease，允许下一次 ARQ attempt 使用新 token。"""
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        released = await repo.release_lease_for_retry(run_id, worker_id=worker_id)
    return released


async def reconcile_expired_run_leases(*, now: datetime | None = None) -> list[str]:
    """收敛过期 Run ownership；重复或并发执行只返回本次实际转换的 Run。"""
    async with pg_manager.get_async_session_context() as db:
        candidates = await AgentRunRepository(db).list_expired_lease_candidates(now=now)
    reconciled: list[str] = []
    terminal_turn_ids: list[str] = []
    cancelled_descendants: list[tuple[str, str]] = []
    for run_id, root_thread_id, uid, app_id in candidates:
        async with pg_manager.get_async_session_context() as db:
            conversation = await ConversationRepository(db).lock_conversation_by_thread_id(root_thread_id)
            if conversation is None or conversation.uid != uid or conversation.app_id != app_id:
                continue
            repo = AgentRunRepository(db)
            candidate = await repo.get_run(run_id)
            if candidate is None:
                continue
            turn = await AgentTurnRepository(db).get_for_scope(
                turn_id=candidate.turn_id,
                thread_id=root_thread_id,
                uid=uid,
                app_id=app_id,
                for_update=True,
            )
            if turn is None:
                raise ValueError("失联 Run 的根 Turn 不存在")
            await AgentInputRepository(db).get_pending_steer(thread_id=root_thread_id, uid=uid, app_id=app_id)
            run, descendants = await repo.reconcile_expired_lease(run_id, now=now)
            if run is None:
                continue
            if turn.current_run_id == run.id:
                conversation.queue_paused = True
                await AgentTurnRepository(db).set_terminal(turn, status="failed")
                terminal_turn_ids.append(turn.id)
            reconciled.append(run.id)
            cancelled_descendants.extend(descendants)
    await publish_cancel_signals([child_id for child_id, _thread_id in cancelled_descendants])
    for turn_id in terminal_turn_ids:
        await finish_turn_observation_if_terminal(turn_id)
    await reconcile_pending_runtime_cleanups()
    return reconciled


async def reconcile_pending_runtime_cleanups() -> list[str]:
    """重试 PostgreSQL 持久拥有的 runtime cleanup，并在成功后发布终态。"""
    async with pg_manager.get_async_session_context() as db:
        pending_runs = await AgentRunRepository(db).list_pending_runtime_cleanups()
    cleaned: list[str] = []
    for run in pending_runs:
        try:
            if not await release_runtime_if_idle(run):
                continue
        except Exception:
            logger.error("Failed to reconcile execution-tree runtime cleanup: run=%s", run.id, exc_info=True)
            continue
        if run.status in TERMINAL_RUN_STATUSES:
            await publish_run_settlement(run.id, run.status, thread_id=run.conversation_thread_id)
        if run.status == "completed":
            await dispatch_next_input(
                uid=run.uid,
                agent_slug=run.agent_slug,
                thread_id=run.conversation_thread_id,
            )
        cleaned.append(run.id)
    from yuxi.modules.agents.services.turns import reconcile_cancelling_turns

    await reconcile_cancelling_turns()
    return cleaned
