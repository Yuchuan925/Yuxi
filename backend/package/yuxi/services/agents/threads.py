"""Thread 查询、归档和显式队列控制用例。"""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.agents.input import AgentInputRepository
from yuxi.repositories.agents.input_receipt import AgentInputReceiptRepository
from yuxi.repositories.agents.turn import AgentTurnRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agents.input_config import resolve_agent_run_model_spec, resolve_agent_run_tool_approval_mode
from yuxi.services.agents.scheduler import claim_follow_up, deliver
from yuxi.services.agents.scope import ActorScope
from yuxi.services.workdir_service import resolve_conversation_workdir_path
from yuxi.storage.postgres.models_business import AGENT_RUN_TERMINAL_STATUSES, Conversation
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now_naive


async def require_thread(*, db: AsyncSession, scope: ActorScope, thread_id: str, lock: bool = False) -> Conversation:
    """按用户与 APP 完整作用域读取 Thread，可选择取得调度锁。"""
    repo = ConversationRepository(db)
    conversation = (
        await repo.lock_conversation_by_thread_id(thread_id)
        if lock
        else await repo.get_conversation_by_thread_id(thread_id)
    )
    if (
        conversation is None
        or conversation.uid != scope.uid
        or conversation.app_id != scope.app_id
        or conversation.status not in {"active", "archived", "subagent"}
    ):
        raise HTTPException(status_code=404, detail="Thread 不存在")
    return conversation


async def list_threads(
    *,
    db: AsyncSession,
    scope: ActorScope,
    agent_slug: str | None = None,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """按完整 APP 作用域列出会话，不混入其他空间。"""
    if status not in {"active", "archived"}:
        raise HTTPException(status_code=422, detail="不支持的 Thread 状态")
    items = await ConversationRepository(db).list_conversations(
        uid=scope.uid,
        app_id=scope.app_id,
        agent_id=agent_slug,
        status=status,
        limit=limit,
        offset=offset,
        exclude_sources=("subagent",),
    )
    latest = await AgentRunRepository(db).get_latest_top_level_runs_for_threads(
        scope.uid, [item.thread_id for item in items]
    )
    return [_thread_public(item, latest.get(item.thread_id)) for item in items]


async def search_threads(
    *,
    db: AsyncSession,
    scope: ActorScope,
    query: str,
    agent_slug: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """仅搜索当前用户与 APP 命名空间中的历史消息。"""
    normalized_query = query.strip()
    if not normalized_query:
        return {"items": [], "has_more": False, "limit": limit, "offset": offset}

    search_items, has_more = await ConversationRepository(db).search_conversations_by_message_content(
        uid=scope.uid,
        app_id=scope.app_id,
        agent_id=agent_slug,
        query=normalized_query,
        limit=limit,
        offset=offset,
    )
    items = []
    for item in search_items:
        conversation = item["conversation"]
        items.append(
            {
                "id": conversation.thread_id,
                "thread_id": conversation.thread_id,
                "uid": conversation.uid,
                "agent_id": conversation.agent_id,
                "title": conversation.title,
                "is_pinned": bool(conversation.is_pinned),
                "created_at": format_utc_datetime(conversation.created_at),
                "updated_at": format_utc_datetime(conversation.updated_at),
                "metadata": conversation.extra_metadata or {},
                "matched_count": item.get("matched_count", 0),
                "message_id": item.get("message_id"),
                "latest_match_at": format_utc_datetime(item.get("latest_match_at")),
                "snippets": [
                    {
                        "message_id": snippet.get("message_id"),
                        "content": snippet.get("content") or "",
                        "created_at": format_utc_datetime(snippet.get("created_at")),
                    }
                    for snippet in item.get("snippets", [])
                ],
            }
        )
    return {"items": items, "has_more": has_more, "limit": limit, "offset": offset}


async def mark_thread_viewed(*, db: AsyncSession, scope: ActorScope, thread_id: str) -> dict:
    """仅将当前作用域最新的终态顶层 Run 标为已查看。"""
    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    run_map = await AgentRunRepository(db).get_latest_top_level_runs_for_threads(scope.uid, [thread_id])
    run_id, run_status = run_map.get(thread_id, (None, None))
    if run_id and run_status in AGENT_RUN_TERMINAL_STATUSES:
        conversation = await ConversationRepository(db).mark_thread_viewed(thread_id, run_id)
    thread_status = _thread_public(conversation, (run_id, run_status))["thread_status"]
    workdir_path = await resolve_conversation_workdir_path(conversation=conversation, uid=scope.uid, db=db)
    return {
        "id": conversation.thread_id,
        "uid": conversation.uid,
        "agent_id": conversation.agent_id,
        "title": conversation.title,
        "is_pinned": bool(conversation.is_pinned),
        "project_id": conversation.project_id,
        "workdir_path": workdir_path,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "metadata": conversation.extra_metadata or {},
        "thread_status": thread_status,
    }


async def update_thread(
    *,
    db: AsyncSession,
    scope: ActorScope,
    thread_id: str,
    title: str | None = None,
    is_pinned: bool | None = None,
    tool_approval_mode: str | None = None,
    model_spec: str | None = None,
) -> dict:
    """在已授权 Thread 上更新标题或置顶标记。"""
    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    if title is not None:
        normalized = title.strip()
        if not normalized or len(normalized) > 255:
            raise HTTPException(status_code=422, detail="标题长度必须为 1 至 255")
        conversation.title = normalized
    if is_pinned is not None:
        conversation.is_pinned = is_pinned
    if tool_approval_mode is not None or model_spec is not None:
        if conversation.status != "active":
            raise HTTPException(status_code=409, detail="非活跃 Thread 不能修改执行配置")
        metadata = dict(conversation.extra_metadata or {})
        if tool_approval_mode is not None:
            metadata["tool_approval_mode"] = resolve_agent_run_tool_approval_mode(tool_approval_mode, None)
        if model_spec is not None:
            metadata["model_spec"] = await resolve_agent_run_model_spec(model_spec, None, db)
        conversation.extra_metadata = metadata
    conversation.updated_at = utc_now_naive()
    await db.commit()
    return _thread_public(conversation)


async def archive_thread(*, db: AsyncSession, scope: ActorScope, thread_id: str) -> dict:
    """执行树、运行时清理和待处理输入全部结束后才归档 Thread。"""
    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    if conversation.status == "subagent":
        raise HTTPException(status_code=409, detail="子智能体 Thread 不能单独归档")
    if conversation.status == "archived":
        return _thread_public(conversation)
    active_turn = await AgentTurnRepository(db).lock_active_for_thread(
        thread_id=thread_id, uid=scope.uid, app_id=scope.app_id
    )
    inputs = await AgentInputRepository(db).list_pending_follow_ups(
        thread_id=thread_id, uid=scope.uid, app_id=scope.app_id
    )
    active_run = await AgentRunRepository(db).get_active_run_by_runtime_scope_for_user(
        runtime_scope_id=thread_id, uid=scope.uid
    )
    if active_turn is not None or inputs or active_run is not None:
        raise HTTPException(status_code=409, detail="Thread 仍有活跃执行或待处理输入")
    conversation.status = "archived"
    conversation.updated_at = utc_now_naive()
    await db.commit()
    return _thread_public(conversation)


async def get_thread_snapshot(*, db: AsyncSession, scope: ActorScope, thread_id: str) -> dict:
    """从持久 Thread、Turn 与队列事实生成可刷新快照。"""
    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id)
    turn_repo = AgentTurnRepository(db)
    turn = await turn_repo.get_active_for_thread(thread_id=thread_id, uid=scope.uid, app_id=scope.app_id)
    if turn is None:
        turn = await turn_repo.get_latest_for_thread(thread_id=thread_id, uid=scope.uid, app_id=scope.app_id)
    current_run = await AgentRunRepository(db).get_run(turn.current_run_id) if turn and turn.current_run_id else None
    queue = await AgentInputRepository(db).list_pending_follow_ups(
        thread_id=thread_id, uid=scope.uid, app_id=scope.app_id
    )
    latest = await AgentRunRepository(db).get_latest_top_level_runs_for_threads(scope.uid, [thread_id])
    return {
        **_thread_public(conversation, latest.get(thread_id)),
        "current_turn": _turn_summary(turn, current_run),
        "queue_paused": bool(conversation.queue_paused),
        "queued_input_count": len(queue),
    }


async def get_queue_snapshot(*, db: AsyncSession, scope: ActorScope, thread_id: str) -> dict:
    """展示尚未创建 Turn 的 follow-up 输入与独立暂停标记。"""
    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id)
    input_repo = AgentInputRepository(db)
    items = await input_repo.list_pending_follow_ups(thread_id=thread_id, uid=scope.uid, app_id=scope.app_id)
    messages_by_input = await input_repo.list_messages_for_inputs([item.id for item in items])
    active = await AgentTurnRepository(db).get_active_for_thread(
        thread_id=thread_id, uid=scope.uid, app_id=scope.app_id
    )
    return {
        "thread_id": thread_id,
        "queue_paused": bool(conversation.queue_paused),
        "status": "paused" if conversation.queue_paused else "running" if active else "ready",
        "inputs": [
            {
                "input_id": item.id,
                "status": item.status,
                "kind": item.kind,
                "turn_id": item.turn_id,
                "run_id": item.consumed_run_id,
                "received_seq": item.received_seq,
                "content": "\n".join(message.content for message in messages_by_input[item.id]),
            }
            for item in items
        ],
    }


async def continue_queue(*, db: AsyncSession, scope: ActorScope, thread_id: str, idempotency_key: str) -> dict:
    """显式解除失败或取消后的暂停，并在同一事务领取队头。"""
    _check_key(idempotency_key)
    event_type = "yuxi.thread.input.continue"
    intent_hash = _hash_intent(event_type)
    receipt_repo = AgentInputReceiptRepository(db)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _control_accepted(existing)

    conversation = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _control_accepted(existing)
    if not conversation.queue_paused:
        raise HTTPException(status_code=409, detail="队列未暂停")
    if await AgentTurnRepository(db).lock_active_for_thread(thread_id=thread_id, uid=scope.uid, app_id=scope.app_id):
        raise HTTPException(status_code=409, detail="当前 Turn 尚未结束")

    conversation.queue_paused = False
    dispatch = await claim_follow_up(db=db, conversation=conversation)
    receipt = await receipt_repo.create(
        receipt_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        uid=scope.uid,
        app_id=scope.app_id,
        thread_id=thread_id,
        event_type=event_type,
        intent_hash=intent_hash,
        run_id=dispatch.run_id if dispatch else None,
    )
    await db.commit()
    if dispatch:
        await deliver(dispatch)
    return _control_accepted(receipt)


async def cancel_input(
    *, db: AsyncSession, scope: ActorScope, thread_id: str, input_id: str, idempotency_key: str
) -> dict:
    """取消未领取的 Input，不伪造尚未存在的 Turn。"""
    _check_key(idempotency_key)
    event_type = "yuxi.thread.input.cancel_input"
    intent_hash = _hash_intent(event_type, input_id)
    receipt_repo = AgentInputReceiptRepository(db)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, event_type, intent_hash)
        return _control_accepted(existing)

    await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    input_repo = AgentInputRepository(db)
    input_item = await input_repo.get_for_scope(
        input_id=input_id, thread_id=thread_id, uid=scope.uid, app_id=scope.app_id, for_update=True
    )
    if input_item is None:
        raise HTTPException(status_code=404, detail="Input 不存在")
    if input_item.status != "pending":
        raise HTTPException(status_code=409, detail="Input 已被领取或取消")
    await input_repo.cancel(input_item)
    receipt = await receipt_repo.create(
        receipt_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        uid=scope.uid,
        app_id=scope.app_id,
        thread_id=thread_id,
        event_type=event_type,
        intent_hash=intent_hash,
        input_id=input_id,
        turn_id=input_item.turn_id,
    )
    await db.commit()
    return _control_accepted(receipt)


def _turn_summary(turn, run) -> dict | None:
    """只投影明确关联的 Turn 与当前 Run。"""
    if turn is None:
        return None
    return {
        "turn_id": turn.id,
        "status": turn.status,
        "run_id": turn.current_run_id,
        "run_status": run.status if run else None,
        "waitpoint": turn.waitpoint,
        "result_run_id": turn.result_run_id,
    }


def _thread_public(conversation: Conversation, latest_run: tuple[str, str] | None = None) -> dict:
    """仅投影 Public Thread 字段，并以最新顶层 Run 计算侧边栏状态。"""
    run_id, run_status = latest_run if latest_run else (None, None)
    if run_id is None or run_id == conversation.last_viewed_run_id:
        thread_status = "done"
    elif run_status in {"completed", "failed", "cancelled", "yielded", "interrupted"}:
        thread_status = "ready"
    else:
        thread_status = "loading"
    return {
        "id": conversation.thread_id,
        "thread_id": conversation.thread_id,
        "agent_id": conversation.agent_id,
        "status": conversation.status,
        "title": conversation.title,
        "is_pinned": bool(conversation.is_pinned),
        "project_id": conversation.project_id,
        "created_at": format_utc_datetime(conversation.created_at),
        "updated_at": format_utc_datetime(conversation.updated_at),
        "metadata": conversation.extra_metadata or {},
        "thread_status": thread_status,
    }


def _check_key(key: str) -> None:
    """校验控制命令的幂等键。"""
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key 长度必须为 1 至 128")


def _hash_intent(*parts) -> str:
    """对控制命令建立跨协议别名一致的意图指纹。"""
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _require_replay(receipt, event_type: str, intent_hash: str) -> None:
    """拒绝同一键改变控制命令或目标。"""
    if receipt.event_type != event_type or receipt.intent_hash != intent_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key 已用于其他输入")


def _control_accepted(receipt) -> dict:
    """返回控制事件首次固定的目标事实。"""
    return {
        "event_id": receipt.id,
        "thread_id": receipt.conversation_thread_id,
        "input_id": receipt.input_id,
        "turn_id": receipt.turn_id,
        "run_id": receipt.run_id,
        "status": "accepted",
    }
