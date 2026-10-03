"""Turn 等待、恢复、取消及明确结果查询。"""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.infrastructure.observability.logging import logger
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.repositories.input_receipt import AgentInputReceiptRepository
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.services.scheduler import Dispatch, deliver
from yuxi.modules.agents.services.scope import ActorScope
from yuxi.modules.agents.services.threads import require_thread
from yuxi.modules.agents.services.tracing import finish_turn_observation_if_terminal
from yuxi.modules.agents.services.transport import publish_cancel_signals
from yuxi.modules.workspace.services.bindings import resolve_conversation_workdir_binding


async def resume_turn(
    *,
    db: AsyncSession,
    scope: ActorScope,
    thread_id: str,
    turn_id: str,
    waitpoint_id: str,
    response: dict,
    idempotency_key: str,
) -> dict:
    """一次性消费明确等待点，在同一 Turn 建立下一段恢复 Run。"""
    _check_key(idempotency_key)
    event_type = "yuxi.session.input.resume"
    intent_hash = _hash_intent(event_type, turn_id, waitpoint_id, response)
    receipt_repo = AgentInputReceiptRepository(db)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _accepted(existing)

    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id)
    # 与 Agent 删除保持 Agent → Conversation 的锁序。
    from yuxi.modules.agents.repositories.definitions import AgentRepository
    from yuxi.modules.identity.repositories.users import UserRepository

    user = await UserRepository(db).get_by_uid(scope.uid)
    agent = (
        None
        if user is None or user.is_deleted
        else await AgentRepository(db).get_visible_by_slug(
            slug=conversation.agent_id,
            user=user,
            kind="any",
            for_key_share=True,
        )
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 不可访问")
    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    if conversation.status not in {"active", "subagent"}:
        raise HTTPException(status_code=409, detail="Thread 已归档")
    turn_repo = AgentTurnRepository(db)
    turn = await turn_repo.get_for_scope(
        turn_id=turn_id, thread_id=thread_id, uid=scope.uid, app_id=scope.app_id, for_update=True
    )
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn 不存在")
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _accepted(existing)
    waitpoint = turn.waitpoint
    if turn.status != "waiting" or not waitpoint or waitpoint.get("id") != waitpoint_id:
        raise HTTPException(status_code=409, detail="等待点已变化或已被消费")
    previous = await AgentRunRepository(db).get_run(turn.current_run_id)
    if previous is None or previous.status != "interrupted" or waitpoint.get("run_id") != previous.id:
        raise HTTPException(status_code=409, detail="等待点与 interrupted Run 不一致")
    resume_value = _validate_resume_response(waitpoint, response)
    binding = await resolve_conversation_workdir_binding(conversation=conversation, uid=scope.uid, db=db)

    run_id = str(uuid.uuid4())
    message = Message(
        conversation_id=conversation.id,
        role="user",
        content=json.dumps(response, ensure_ascii=False),
        message_type="resume",
        extra_metadata={
            "resume": resume_value,
            "waitpoint_id": waitpoint_id,
            "rejected_tool_calls": [
                call["tool_call_id"]
                for call, decision in zip(waitpoint.get("calls", []), resume_value.get("decisions", []))
                if decision["type"] == "reject"
            ],
        },
        delivery_status="dispatched",
        turn_id=turn.id,
    )
    db.add(message)
    await db.flush()
    await AgentRunRepository(db).create_run(
        run_id=run_id,
        conversation_thread_id=thread_id,
        runtime_scope_id=previous.runtime_scope_id,
        agent_slug=previous.agent_slug,
        uid=scope.uid,
        turn_id=turn.id,
        app_id=scope.app_id,
        api_key_id=scope.api_key_id,
        input_payload=previous.input_payload or {},
        source=previous.source,
        channel=previous.channel,
        external_id=previous.external_id,
        origin_metadata=previous.origin_metadata or {},
        conversation_id=conversation.id,
        resume_from_run_id=previous.id,
        run_type="subagent" if previous.run_type == "subagent" else "resume",
        created_by_run_id=previous.created_by_run_id,
        subagent_thread_relation_id=previous.subagent_thread_relation_id,
    )
    message.run_id = run_id
    await AgentRunRepository(db).set_input_message(run_id, message.id)
    await turn_repo.set_current(turn, run_id=run_id)
    receipt = await receipt_repo.create(
        receipt_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        uid=scope.uid,
        app_id=scope.app_id,
        thread_id=thread_id,
        event_type=event_type,
        intent_hash=intent_hash,
        turn_id=turn.id,
        run_id=run_id,
    )
    await db.commit()
    await deliver(Dispatch(run_id=run_id, binding=binding))
    return _accepted(receipt)


async def cancel_turn(
    *,
    db: AsyncSession,
    scope: ActorScope,
    thread_id: str,
    turn_id: str | None,
    idempotency_key: str,
    expected_run_id: str | None = None,
) -> dict:
    """暂停并保留待消费输入，请求当前执行树收敛。"""
    _check_key(idempotency_key)
    event_type = "agent.session.input.cancel"
    intent_hash = _hash_intent(event_type, turn_id, expected_run_id)
    receipt_repo = AgentInputReceiptRepository(db)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _accepted(existing)

    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _accepted(existing)
    turn_repo = AgentTurnRepository(db)
    turn = (
        await turn_repo.get_for_scope(
            turn_id=turn_id, thread_id=thread_id, uid=scope.uid, app_id=scope.app_id, for_update=True
        )
        if turn_id is not None
        else await turn_repo.lock_active_for_thread(thread_id=thread_id, uid=scope.uid, app_id=scope.app_id)
    )
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn 不存在")
    if expected_run_id is not None and turn.current_run_id != expected_run_id:
        raise HTTPException(status_code=409, detail="当前 Run 已变化")
    if turn.status not in {"running", "waiting", "cancelling", "cancelled"}:
        raise HTTPException(status_code=409, detail="Turn 已结束，无法取消")

    cancelled_run_ids: list[str] = []
    descendants: list[tuple[str, str]] = []
    waiting_cleanup = False
    terminal_changed = False
    if turn.status != "cancelled":
        conversation.queue_paused = True
        waiting_cleanup = turn.status == "waiting" or (turn.status == "cancelling" and bool(turn.waitpoint))
        if turn.status != "cancelling":
            await turn_repo.set_cancelling(turn)
        run = await AgentRunRepository(db).get_run(turn.current_run_id)
        if run is None:
            raise ValueError("Turn 当前 Run 不存在")
        if run.status in {"pending", "running", "cancel_requested"}:
            run, cancelled_run_ids = await AgentRunRepository(db).request_cancel_execution_tree(
                run_id=run.id, uid=scope.uid, cascade_descendants=False
            )
            if run.status == "cancelled" and not run.runtime_cleanup_pending:
                await turn_repo.set_terminal(turn, status="cancelled")
                terminal_changed = True
        elif run.status == "cancelled":
            if not run.runtime_cleanup_pending:
                await turn_repo.set_terminal(turn, status="cancelled")
                terminal_changed = True
        elif run.status != "interrupted" or not waiting_cleanup:
            raise HTTPException(status_code=409, detail="当前 Run 已结束，取消目标已变化")

    if turn.status != "cancelled" or terminal_changed:
        descendants = await AgentRunRepository(db).cancel_active_execution_tree_descendants(run)
        cancelled_run_ids.extend(child_id for child_id, _ in descendants)

    receipt = await receipt_repo.create(
        receipt_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        uid=scope.uid,
        app_id=scope.app_id,
        thread_id=thread_id,
        event_type=event_type,
        intent_hash=intent_hash,
        turn_id=turn.id,
        run_id=turn.current_run_id,
    )
    await db.commit()
    if terminal_changed:
        await finish_turn_observation_if_terminal(turn.id)
    if cancelled_run_ids:
        await publish_cancel_signals(cancelled_run_ids)
    if waiting_cleanup:
        await settle_waiting_cancel(thread_id=thread_id, turn_id=turn.id, uid=scope.uid, app_id=scope.app_id)
    for child_id, child_thread_id in descendants:
        child = await AgentRunRepository(db).get_run(child_id)
        if child.status == "interrupted":
            await settle_waiting_cancel(
                thread_id=child_thread_id, turn_id=child.turn_id, uid=scope.uid, app_id=scope.app_id
            )
    return _accepted(receipt)


async def get_turn_snapshot(*, db: AsyncSession, scope: ActorScope, thread_id: str, turn_id: str) -> dict:
    """按明确 Turn 关系读取执行段、等待点和最终结果。"""
    await require_thread(db=db, scope=scope, thread_id=thread_id)
    turn_repo = AgentTurnRepository(db)
    turn = await turn_repo.get_for_scope(turn_id=turn_id, thread_id=thread_id, uid=scope.uid, app_id=scope.app_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn 不存在")
    from yuxi.modules.agents.services.public_items import serialize_public_run

    runs = await turn_repo.list_runs(turn.id)
    result_run = next((run for run in runs if run.id == turn.result_run_id), None)
    current_run = next((run for run in runs if run.id == turn.current_run_id), None)
    output = None
    if result_run is not None and result_run.output_message_id is not None:
        output_message = await db.get(Message, result_run.output_message_id)
        if output_message is None or output_message.run_id != result_run.id or output_message.turn_id != turn.id:
            raise ValueError("Turn 最终结果消息归属不一致")
        await db.refresh(output_message, attribute_names=["tool_calls"])
        from yuxi.modules.agents.services.public_items import serialize_public_items, serialize_public_run

        output = serialize_public_items(output_message, result_run, turn.result_run_id)
    audits = await turn_repo.list_model_usage_audits(turn.id)
    return {
        "turn_id": turn.id,
        "thread_id": thread_id,
        "status": turn.status,
        "current_run_id": turn.current_run_id,
        "result_run_id": turn.result_run_id,
        "waitpoint": turn.waitpoint,
        "runs": [serialize_public_run(run) for run in runs],
        "output": output,
        "usage": _summarize_turn_usage(audits),
        "error": (
            {"type": current_run.error_type, "message": current_run.error_message}
            if turn.status in {"failed", "cancelled"} and current_run is not None
            else None
        ),
    }


def _summarize_turn_usage(audits: list[Message]) -> dict:
    """每个 Model operation 只计一次，并明确缺失或未完成的用量。"""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    missing = 0
    counted = 0
    for audit in audits:
        usage = audit.usage if isinstance(audit.usage, dict) else {}
        valid = (
            bool(audit.operation_id)
            and audit.execution_status == "completed"
            and all(
                isinstance(usage.get(key), int) and not isinstance(usage[key], bool) and usage[key] >= 0
                for key in totals
            )
        )
        if not valid:
            missing += 1
            continue
        for key in totals:
            totals[key] += usage[key]
        counted += 1
    return {
        "available": counted > 0,
        "complete": bool(audits) and missing == 0,
        "operations": counted,
        "missing_operations": missing,
        **totals,
    }


async def list_turn_messages(
    *, db: AsyncSession, scope: ActorScope, thread_id: str, turn_id: str, after_id: str | None = None, limit: int = 50
) -> list[dict]:
    """读取本轮原始输入与明确绑定的用户可见输出。"""
    await require_thread(db=db, scope=scope, thread_id=thread_id)
    turn = await AgentTurnRepository(db).get_for_scope(
        turn_id=turn_id, thread_id=thread_id, uid=scope.uid, app_id=scope.app_id
    )
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn 不存在")
    from yuxi.modules.agents.repositories.public_items import PublicItemRepository
    from yuxi.modules.agents.services.public_items import serialize_public_items

    rows = await PublicItemRepository(db).list_items(
        thread_id=thread_id, uid=scope.uid, app_id=scope.app_id, turn_id=turn_id
    )
    items = [item for message, run, result_id in rows for item in serialize_public_items(message, run, result_id)]
    if after_id is not None:
        position = next((index for index, item in enumerate(items) if item["id"] == after_id), None)
        if position is None:
            raise HTTPException(status_code=400, detail="after_id 不是当前 Turn 的公开 item")
        items = items[position + 1 :]
    return items[:limit]


async def settle_waiting_cancel(*, thread_id: str, turn_id: str, uid: str, app_id: str | None) -> bool:
    """清理等待 checkpoint，再把 cancelling Turn 变为 cancelled。"""
    async with pg_manager.get_async_session_context() as db:
        scope = ActorScope(uid=uid, app_id=app_id)
        await require_thread(db=db, scope=scope, thread_id=thread_id)
        turn = await AgentTurnRepository(db).get_for_scope(turn_id=turn_id, thread_id=thread_id, uid=uid, app_id=app_id)
        if turn is None or turn.status != "cancelling" or not turn.waitpoint:
            return False
        run = await AgentRunRepository(db).get_run(turn.current_run_id)
        if run is None or run.status != "interrupted":
            return False
    try:
        await _clear_waitpoint_checkpoint(run)
    except Exception:
        logger.exception("Failed to clear cancelled Turn waitpoint: %s", turn_id)
        return False

    async with pg_manager.get_async_session_context() as db:
        conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
        turn = await AgentTurnRepository(db).get_for_scope(
            turn_id=turn_id, thread_id=thread_id, uid=uid, app_id=app_id, for_update=True
        )
        if turn is None or turn.status != "cancelling" or turn.current_run_id != run.id:
            return False
        if not conversation.queue_paused:
            raise ValueError("等待取消未保持队列暂停")
        await AgentTurnRepository(db).set_terminal(turn, status="cancelled")
    await finish_turn_observation_if_terminal(turn_id)
    return True


async def reconcile_cancelling_turns() -> list[str]:
    """补偿清理进程失联后仍占用线程的 cancelling Turn。"""
    async with pg_manager.get_async_session_context() as db:
        candidates = list((await db.execute(select(AgentTurn).where(AgentTurn.status == "cancelling"))).scalars())
    settled = []
    for candidate in candidates:
        run_id = candidate.current_run_id
        async with pg_manager.get_async_session_context() as db:
            run = await AgentRunRepository(db).get_run(run_id)
        if run is None:
            continue
        if run.status == "interrupted" and candidate.waitpoint:
            if await settle_waiting_cancel(
                thread_id=candidate.conversation_thread_id,
                turn_id=candidate.id,
                uid=candidate.uid,
                app_id=candidate.app_id,
            ):
                settled.append(candidate.id)
        elif run.status == "cancelled" and not run.runtime_cleanup_pending:
            async with pg_manager.get_async_session_context() as db:
                scope = ActorScope(uid=candidate.uid, app_id=candidate.app_id)
                await require_thread(db=db, scope=scope, thread_id=candidate.conversation_thread_id, lock=True)
                turn = await AgentTurnRepository(db).get_for_scope(
                    turn_id=candidate.id,
                    thread_id=candidate.conversation_thread_id,
                    uid=candidate.uid,
                    app_id=candidate.app_id,
                    for_update=True,
                )
                if turn is not None and turn.status == "cancelling":
                    await AgentTurnRepository(db).set_terminal(turn, status="cancelled")
                    settled.append(candidate.id)
            if candidate.id in settled:
                await finish_turn_observation_if_terminal(candidate.id)
    return settled


async def _clear_waitpoint_checkpoint(run: AgentRun) -> None:
    """移除未执行的工具调用，再推进等待节点而不执行工具。"""
    from yuxi.modules.agents.repositories.definitions import AgentRepository
    from yuxi.modules.agents.runtime.agent_backends import get_agent_backend
    from yuxi.modules.agents.runtime.sandbox.paths import runtime_workdir_path
    from yuxi.modules.workspace.services.bindings import resolve_conversation_workdir_binding

    async with pg_manager.get_async_session_context() as db:
        agent_item = await AgentRepository(db).get_by_slug(run.agent_slug)
        if agent_item is None:
            raise ValueError("等待点 Agent 不存在")
        backend = get_agent_backend(agent_item.backend_id)
        conversation = await require_thread(
            db=db,
            scope=ActorScope(uid=run.uid, app_id=run.app_id),
            thread_id=run.conversation_thread_id,
        )
        binding = await resolve_conversation_workdir_binding(conversation=conversation, uid=run.uid, db=db)

    context = backend.context_schema()
    context.update_config((agent_item.config_json or {}).get("context") or {})
    context.update(
        {
            "thread_id": run.conversation_thread_id,
            "uid": run.uid,
            "run_id": run.id,
            "worker_id": "waitpoint-cleanup",
            "runtime_scope_id": run.runtime_scope_id,
            "workdir_relative_path": binding.workdir_path,
            "workdir_path": runtime_workdir_path(binding.workdir_path),
        }
    )
    context.model = run.input_payload["model_spec"]
    context.tool_approval_mode = run.input_payload["tool_approval_mode"]
    # 持久 Run 与 Thread 的完整归属已验证；清理不取得资源使用授权。
    graph = await backend.get_graph(context=context, checkpoint_only=True)
    config = {"configurable": {"uid": run.uid, "thread_id": run.conversation_thread_id}}
    await _drain_waitpoint_checkpoint(graph, config)


async def _drain_waitpoint_checkpoint(graph, config: dict) -> None:
    """幂等跳过等待工具调用及其余待执行节点。"""
    from langchain_core.messages import AIMessage

    saved = await graph.aget_state(config)
    if saved.next:
        messages = saved.values.get("messages") or []
        if not messages or not isinstance(messages[-1], AIMessage):
            raise ValueError("等待 checkpoint 缺少未执行的工具调用")
        if len(saved.next) != 1:
            raise ValueError("等待 checkpoint 存在多个待清理节点")
        pending = messages[-1]
        if pending.tool_calls:
            await graph.aupdate_state(
                config,
                {"messages": [AIMessage(id=pending.id, content="[已取消]", tool_calls=[])]},
                as_node=saved.next[0],
            )
        elif pending.content != "[已取消]":
            raise ValueError("等待 checkpoint 缺少未执行的工具调用")
    from langgraph.graph import END

    await graph.aupdate_state(config, None, as_node=END)
    saved = await graph.aget_state(config)
    if saved.next or saved.interrupts:
        raise ValueError("等待 checkpoint 清理未收敛")


def _validate_resume_response(waitpoint: dict, response: dict) -> dict:
    """将结构化回答或审批核对为 LangGraph resume payload。"""
    if not isinstance(response, dict):
        raise HTTPException(status_code=422, detail="恢复响应必须是对象")
    if waitpoint["kind"] == "answer":
        if response.get("type") != "answer" or not isinstance(response.get("answers"), list):
            raise HTTPException(status_code=422, detail="等待点需要 answer 响应")
        questions = waitpoint.get("questions") or []
        answers = response["answers"]
        expected_ids = [question.get("question_id") for question in questions]
        received_ids = [item.get("question_id") for item in answers if isinstance(item, dict)]
        if len(answers) != len(questions) or received_ids != expected_ids:
            raise HTTPException(status_code=422, detail="回答必须覆盖等待点全部问题并保持顺序")
        if any(not isinstance(item.get("answer"), (str, list, dict)) for item in answers):
            raise HTTPException(status_code=422, detail="回答内容类型无效")
        return {item["question_id"]: item["answer"] for item in answers}

    if waitpoint["kind"] == "approval":
        if response.get("type") != "approval" or not isinstance(response.get("decisions"), list):
            raise HTTPException(status_code=422, detail="等待点需要 approval 响应")
        calls = waitpoint.get("calls") or []
        decisions = response["decisions"]
        received_ids = [item.get("call_id") for item in decisions if isinstance(item, dict)]
        if len(decisions) != len(calls) or received_ids != [call.get("call_id") for call in calls]:
            raise HTTPException(status_code=422, detail="审批必须覆盖等待点全部调用并保持顺序")
        if any(item.get("decision") not in call.get("allowed_decisions", []) for item, call in zip(decisions, calls)):
            raise HTTPException(status_code=422, detail="审批决定不在允许范围内")
        return {"decisions": [{"type": item["decision"]} for item in decisions]}

    raise HTTPException(status_code=409, detail="等待点类型无效")


def _check_key(key: str) -> None:
    """在控制边界校验 Idempotency-Key。"""
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key 长度必须为 1 至 128")


def _hash_intent(*parts) -> str:
    """为等待点或取消命令生成稳定意图指纹。"""
    value = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def _require_replay(receipt, event_type: str, intent_hash: str) -> None:
    """只允许同一命令和相同目标重放。"""
    if receipt.event_type != event_type or receipt.intent_hash != intent_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key 已用于其他输入")


def _accepted(receipt) -> dict:
    """返回首次固定的控制目标。"""
    return {
        "event_id": receipt.id,
        "thread_id": receipt.conversation_thread_id,
        "turn_id": receipt.turn_id,
        "run_id": receipt.run_id,
        "status": "accepted",
    }
