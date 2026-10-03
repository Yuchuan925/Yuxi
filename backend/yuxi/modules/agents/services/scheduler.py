"""Thread 锁下领取持久输入，并在提交后投递 Run。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.infrastructure.observability.logging import logger
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.threads import Conversation
from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.workspace.paths import ensure_bound_user_workdir
from yuxi.modules.workspace.services.bindings import WorkdirBinding, resolve_conversation_workdir_binding


@dataclass(frozen=True, slots=True)
class Dispatch:
    """记录 owning transaction 已创建、尚待提交投递的 Run。"""

    run_id: str
    binding: WorkdirBinding


async def claim_next_input(
    *, db: AsyncSession, conversation: Conversation, binding: WorkdirBinding | None = None
) -> Dispatch | None:
    """在已锁 Thread 上领取优先队头，原子建立 Turn 与首段 Run。"""
    if conversation.status not in {"active", "subagent"} or conversation.queue_paused:
        return None

    turn_repo = AgentTurnRepository(db)
    if await turn_repo.lock_active_for_thread(
        thread_id=conversation.thread_id, uid=conversation.uid, app_id=conversation.app_id
    ):
        return None

    input_repo = AgentInputRepository(db)
    head = await input_repo.get_queue_head(
        thread_id=conversation.thread_id, uid=conversation.uid, app_id=conversation.app_id
    )
    if head is None:
        return None
    if binding is None:
        binding = await resolve_conversation_workdir_binding(conversation=conversation, uid=conversation.uid, db=db)

    messages = await input_repo.list_messages(head.id)
    cutoff_seq = await input_repo.get_latest_receive_seq(head.id)
    if not messages or cutoff_seq is None:
        raise ValueError("队头 Input 缺少已接收消息")

    turn_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    turn = await turn_repo.create(
        turn_id=turn_id, thread_id=conversation.thread_id, uid=conversation.uid, app_id=conversation.app_id
    )
    await AgentRunRepository(db).create_run(
        run_id=run_id,
        conversation_thread_id=conversation.thread_id,
        runtime_scope_id=conversation.thread_id,
        agent_slug=head.agent_slug,
        uid=head.uid,
        turn_id=turn_id,
        input_id=head.id,
        app_id=head.app_id,
        api_key_id=head.api_key_id,
        input_payload=head.input_payload or {},
        source=head.source,
        channel=head.channel,
        external_id=head.external_id,
        origin_metadata=head.origin_metadata or {},
        conversation_id=conversation.id,
        run_type="subagent" if conversation.status == "subagent" else "chat",
        created_by_run_id=(head.origin_metadata or {}).get("created_by_run_id"),
        subagent_thread_relation_id=(head.origin_metadata or {}).get("subagent_thread_relation_id"),
    )
    await turn_repo.set_current(turn, run_id=run_id)
    await input_repo.consume(input_id=head.id, turn_id=turn_id, run_id=run_id, cutoff_seq=cutoff_seq)
    await AgentRunRepository(db).set_input_message(run_id, messages[0].id)
    return Dispatch(run_id=run_id, binding=binding)


async def dispatch_next_input(*, uid: str, agent_slug: str, thread_id: str) -> str | None:
    """自管事务领取可执行队头；提交并物化目录后才投递。"""
    async with pg_manager.get_async_session_context() as db:
        conversation = await ConversationRepository(db).lock_conversation_by_thread_id(thread_id)
        if (
            conversation is None
            or conversation.uid != uid
            or conversation.agent_id != agent_slug
            or conversation.status not in {"active", "subagent"}
        ):
            return None
        dispatch = await claim_next_input(db=db, conversation=conversation)

    if dispatch is None:
        return None
    await deliver(dispatch)
    return dispatch.run_id


async def deliver(dispatch: Dispatch) -> None:
    """仅在 owning transaction 提交之后物化目录并投递同一个 pending Run。"""
    if dispatch.binding.materialize_managed:
        ensure_bound_user_workdir(dispatch.binding.uid, dispatch.binding.workdir_path)
    from yuxi.modules.agents.services.transport import enqueue_agent_run

    await enqueue_agent_run(dispatch.run_id)


async def recover_pending_dispatches() -> None:
    """补投持久 pending Run，并领取崩溃后仍处于 ready 的队头。"""
    async with pg_manager.get_async_session_context() as db:
        pending = list((await db.execute(select(AgentRun).where(AgentRun.status == "pending"))).scalars())
        scopes = list(
            (
                await db.execute(
                    select(AgentInput.uid, AgentInput.agent_slug, AgentInput.conversation_thread_id)
                    .where(AgentInput.status == "pending")
                    .distinct()
                )
            ).all()
        )

    for run in pending:
        try:
            async with pg_manager.get_async_session_context() as db:
                conversation = await ConversationRepository(db).get_conversation_by_thread_id(
                    run.conversation_thread_id
                )
                expected_status = "subagent" if run.run_type == "subagent" else "active"
                if (
                    conversation is None
                    or conversation.uid != run.uid
                    or conversation.agent_id != run.agent_slug
                    or conversation.app_id != run.app_id
                    or conversation.status != expected_status
                ):
                    continue
                current = await AgentRunRepository(db).get_run(run.id)
                turn = await AgentTurnRepository(db).get_for_scope(
                    turn_id=run.turn_id, thread_id=run.conversation_thread_id, uid=run.uid, app_id=run.app_id
                )
                if current is None or current.status != "pending" or turn is None or turn.current_run_id != run.id:
                    continue
                binding = await resolve_conversation_workdir_binding(conversation=conversation, uid=run.uid, db=db)
            await deliver(Dispatch(run_id=run.id, binding=binding))
        except Exception:
            logger.exception("Failed to republish pending AgentRun: %s", run.id)

    for uid, agent_slug, thread_id in scopes:
        try:
            await dispatch_next_input(uid=uid, agent_slug=agent_slug, thread_id=thread_id)
        except Exception:
            logger.exception("Failed to recover ready AgentInput: %s", thread_id)
