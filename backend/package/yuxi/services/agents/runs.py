"""同一事务内收敛 Run、Turn 与 steer 接管。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.agents.input import AgentInputRepository
from yuxi.repositories.agents.turn import AgentTurnRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agents.scope import ActorScope
from yuxi.storage.postgres.models_business import AgentRun, AgentTurn, Conversation


@dataclass(frozen=True, slots=True)
class RunSettlement:
    """返回本事务固定的 Run 结束原因及后续投递目标。"""

    status: str
    changed: bool
    next_run_id: str | None = None


async def should_yield_for_steer(run_id: str) -> bool:
    """供 Agent 图安全钩子查询待接管输入；终态仍在 Thread 锁内裁决。"""
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        run = await AgentRunRepository(db).get_run(run_id)
        if run is None or run.run_type == "subagent" or run.status != "running":
            return False
        turn = await AgentTurnRepository(db).get(run.turn_id)
        if turn is None or turn.status != "running" or turn.current_run_id != run_id:
            return False
        pending = await AgentInputRepository(db).get_pending_steer(
            thread_id=run.conversation_thread_id,
            uid=run.uid,
            app_id=run.app_id,
            turn_id=turn.id,
            for_update=False,
        )
        return pending is not None


async def settle_checkpoint(
    *,
    db: AsyncSession,
    run: AgentRun,
    worker_id: str | None,
    status: str,
    token_usage: dict | None,
    waitpoint: dict | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> RunSettlement:
    """在已锁 Thread 的输出事务内决定 yielded、waiting 或整轮终态。"""
    if run.run_type == "subagent":
        terminal, changed = await AgentRunRepository(db).set_terminal_status(
            run.id,
            status=status,
            token_usage=token_usage,
            worker_id=worker_id,
            error_type=error_type,
            error_message=error_message,
        )
        return RunSettlement(status=terminal.status if terminal else status, changed=changed)

    turn_repo = AgentTurnRepository(db)
    turn = await turn_repo.get_for_scope(
        turn_id=run.turn_id,
        thread_id=run.conversation_thread_id,
        uid=run.uid,
        app_id=run.app_id,
        for_update=True,
    )
    if turn is None or turn.current_run_id != run.id:
        raise ValueError("Run 不是目标 Turn 的当前执行段")
    input_repo = AgentInputRepository(db)
    run_repo = AgentRunRepository(db)
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(run.conversation_thread_id)
    if conversation is None or conversation.uid != run.uid or conversation.app_id != run.app_id:
        raise ValueError("Run 的 Thread 归属不一致")

    if status == "completed" and turn.status == "running":
        pending = await input_repo.get_pending_steer(
            thread_id=run.conversation_thread_id,
            uid=run.uid,
            app_id=run.app_id,
            turn_id=turn.id,
        )
        if pending is not None:
            terminal, changed = await run_repo.set_terminal_status(
                run.id, status="yielded", token_usage=token_usage, worker_id=worker_id
            )
            if terminal is None or not changed:
                raise ValueError("Steer 接管前当前 Run 所有权已失效")
            next_run_id = await _consume_steer(
                db=db, conversation=conversation, turn=turn, previous=run, pending=pending
            )
            return RunSettlement(status="yielded", changed=True, next_run_id=next_run_id)

    if status == "interrupted" and turn.status == "running":
        if not waitpoint or waitpoint.get("run_id") != run.id:
            raise ValueError("等待终态缺少绑定当前 Run 的等待点")
        terminal, changed = await run_repo.set_terminal_status(
            run.id,
            status="interrupted",
            token_usage=token_usage,
            worker_id=worker_id,
            error_type=error_type,
            error_message=error_message,
        )
        if terminal is None or not changed:
            raise ValueError("等待终态的 Run 所有权已失效")
        await turn_repo.set_waiting(turn, run_id=run.id, waitpoint=waitpoint)
        return RunSettlement(status="interrupted", changed=True)

    if status == "completed" and turn.status == "running":
        terminal, changed = await run_repo.set_terminal_status(
            run.id, status="completed", token_usage=token_usage, worker_id=worker_id
        )
        if terminal is None or not changed:
            raise ValueError("完成终态的 Run 所有权已失效")
        await turn_repo.set_terminal(turn, status="completed", result_run_id=run.id)
        return RunSettlement(status="completed", changed=True)

    if status in {"failed", "cancelled"}:
        terminal, changed = await run_repo.set_terminal_status(
            run.id,
            status=status,
            token_usage=token_usage,
            worker_id=worker_id,
            error_type=error_type,
            error_message=error_message,
        )
        if terminal is None or not changed:
            return RunSettlement(status=terminal.status if terminal else status, changed=False)
        await input_repo.cancel_pending_for_turn(turn_id=turn.id)
        conversation.queue_paused = True
        if status == "failed":
            await turn_repo.set_terminal(turn, status="failed")
        elif turn.status != "cancelling":
            await turn_repo.set_cancelling(turn)
        return RunSettlement(status=status, changed=True)

    raise ValueError(f"Turn 状态 {turn.status} 不能接受 Run 终态 {status}")


async def get_run_snapshot(*, db: AsyncSession, scope: ActorScope, thread_id: str, run_id: str) -> dict:
    """按 Thread/Turn/APP 归属读取执行段及持久输出。"""
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
    if conversation is None or conversation.uid != scope.uid or conversation.app_id != scope.app_id:
        raise HTTPException(status_code=404, detail="Run 不存在")
    run = await AgentRunRepository(db).get_run(run_id)
    if run is None or run.uid != scope.uid or run.app_id != scope.app_id or run.conversation_thread_id != thread_id:
        raise HTTPException(status_code=404, detail="Run 不存在")
    turn = await AgentTurnRepository(db).get_for_scope(
        turn_id=run.turn_id,
        thread_id=run.runtime_scope_id,
        uid=scope.uid,
        app_id=scope.app_id,
    )
    if turn is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    result = run.to_dict()
    if run.output_message_id is not None:
        from yuxi.storage.postgres.models_business import Message

        message = await db.get(Message, run.output_message_id)
        if message is None or message.run_id != run.id or message.turn_id != run.turn_id:
            raise ValueError("Run 输出消息归属不一致")
        await db.refresh(message, attribute_names=["tool_calls"])
        result["output"] = message.to_dict()
    else:
        result["output"] = None
    result["langfuse_url"] = None
    if run.langfuse_trace_id:
        from yuxi.services.langfuse_service import get_trace_url_by_id_async

        result["langfuse_url"] = await get_trace_url_by_id_async(run.langfuse_trace_id)
    return result


async def get_run_langfuse_link(*, db: AsyncSession, scope: ActorScope, thread_id: str, run_id: str) -> dict:
    """按根 Thread 作用域解析同一 Turn 的 Langfuse trace 链接。"""
    snapshot = await get_run_snapshot(db=db, scope=scope, thread_id=thread_id, run_id=run_id)
    if not snapshot.get("langfuse_trace_id"):
        return {"run_id": run_id, "available": False, "reason": "trace_not_available"}
    url = snapshot.get("langfuse_url")
    if not url:
        return {"run_id": run_id, "available": False, "reason": "langfuse_unavailable"}
    return {"run_id": run_id, "available": True, "url": url}


async def _consume_steer(
    *, db: AsyncSession, conversation: Conversation, turn: AgentTurn, previous: AgentRun, pending
) -> str:
    """封闭本轮 pending steer 的消息批次并建立下一执行段。"""
    input_repo = AgentInputRepository(db)
    messages = await input_repo.list_messages(pending.id)
    cutoff_seq = await input_repo.get_latest_receive_seq(pending.id)
    if not messages or cutoff_seq is None:
        raise ValueError("Steer Input 缺少已接收消息")
    run_id = str(uuid.uuid4())
    await AgentRunRepository(db).create_run(
        run_id=run_id,
        conversation_thread_id=conversation.thread_id,
        runtime_scope_id=previous.runtime_scope_id,
        agent_slug=previous.agent_slug,
        uid=previous.uid,
        turn_id=turn.id,
        input_id=pending.id,
        app_id=previous.app_id,
        api_key_id=pending.api_key_id,
        input_payload=pending.input_payload or {},
        source=pending.source,
        channel=pending.channel,
        external_id=pending.external_id,
        origin_metadata=pending.origin_metadata or {},
        conversation_id=conversation.id,
        resume_from_run_id=previous.id,
        run_type="chat",
        input_message_id=messages[0].id,
    )
    await AgentTurnRepository(db).set_current(turn, run_id=run_id)
    await input_repo.consume(input_id=pending.id, turn_id=turn.id, run_id=run_id, cutoff_seq=cutoff_seq)
    return run_id
