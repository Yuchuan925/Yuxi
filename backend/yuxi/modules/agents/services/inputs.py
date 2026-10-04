"""Thread 输入的持久接收、配置冻结和幂等回执。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.models.inputs import AgentInputReceipt
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.repositories.definitions import AgentRepository
from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.input_receipt import AgentInputReceiptRepository
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.sessions import SessionRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.runtime.agent_backends import AgentBackendNotFoundError, get_agent_backend
from yuxi.modules.agents.services.input_config import (
    resolve_agent_run_config,
    resolve_agent_run_model_spec,
    resolve_agent_run_tool_approval_mode,
)
from yuxi.modules.agents.services.input_messages import AgentRunInputMessage
from yuxi.modules.agents.services.public_items import serialize_public_items
from yuxi.modules.agents.services.scheduler import Dispatch, claim_next_input, deliver
from yuxi.modules.agents.services.scope import ActorScope
from yuxi.modules.agents.services.threads import require_thread
from yuxi.modules.identity.models import User
from yuxi.modules.workspace.paths import ensure_bound_user_workdir
from yuxi.modules.workspace.repositories.projects import ProjectRepository
from yuxi.modules.workspace.services.bindings import resolve_session_workdir_binding
from yuxi.modules.workspace.services.projects import create_implicit_project
from yuxi.shared.hashing import hash_id


def thread_id_for_creation(scope: ActorScope, idempotency_key: str) -> str:
    """以接收身份和幂等键生成跨 Thread/Session 别名稳定的 Thread ID。"""
    _check_idempotency_key(idempotency_key)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"yuxi-thread:{scope.uid}:{scope.app_id}:{idempotency_key}"))


async def create_thread(
    *,
    db: AsyncSession,
    scope: ActorScope,
    agent_slug: str,
    thread_id: str,
    idempotency_key: str,
    project_id: str | None = None,
    title: str | None = None,
    messages: list[AgentRunInputMessage] | None = None,
    model_spec: str | None = None,
    tool_approval_mode: str | None = None,
    attachment_file_ids: list[str] | None = None,
    source: str = "public_api",
    channel: str = "api",
    external_id: str | None = None,
    origin_metadata: dict | None = None,
) -> dict:
    """在一个事务中创建 Thread，并可同时接收与领取首条输入。"""
    _check_idempotency_key(idempotency_key)
    input_messages = list(messages or [])
    intent_hash = _intent_hash(
        "yuxi.session.create",
        agent_slug,
        project_id,
        title,
        model_spec,
        tool_approval_mode,
        attachment_file_ids or [],
        external_id,
        origin_metadata or {},
        [_message_intent(item) for item in input_messages],
    )
    receipt_repo = AgentInputReceiptRepository(db)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, "yuxi.session.create", intent_hash)
        return _accepted(existing)

    user = await db.scalar(select(User).where(User.uid == scope.uid))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    agent_item = await AgentRepository(db).get_visible_by_slug(
        slug=agent_slug, user=user, kind="main", for_key_share=True
    )
    if agent_item is None:
        raise HTTPException(status_code=404, detail="智能体不存在")

    existing_thread = await SessionRepository(db).get_session_by_thread_id(thread_id)
    if existing_thread is not None:
        existing = await receipt_repo.get_for_scope(
            uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            _require_replay(existing, "yuxi.session.create", intent_hash)
            return _accepted(existing)
        raise HTTPException(status_code=409, detail="Thread ID 已存在")
    thread_metadata = {"source": source, "channel": channel}
    if model_spec is not None:
        thread_metadata["model_spec"] = await resolve_agent_run_model_spec(model_spec, None, db)
    if tool_approval_mode is not None:
        thread_metadata["tool_approval_mode"] = resolve_agent_run_tool_approval_mode(tool_approval_mode, None)
    try:
        async with db.begin_nested():
            project = (
                await ProjectRepository(db).lock_active_for_user(project_id, scope.uid)
                if project_id
                else await create_implicit_project(uid=scope.uid, db=db)
            )
            if project is None:
                raise HTTPException(status_code=404, detail="Project 不存在或不可访问")
            agent_session = await SessionRepository(db).add_session(
                uid=scope.uid,
                agent_id=agent_slug,
                title=title,
                thread_id=thread_id,
                metadata=thread_metadata,
                project_id=project.id,
                creation_request_id=hash_id("thread:", f"{scope.uid}:{scope.app_id}:{idempotency_key}", length=64),
                app_id=scope.app_id,
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != "23505":
            raise
        existing = await receipt_repo.get_for_scope(
            uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            _require_replay(existing, "yuxi.session.create", intent_hash)
            return _accepted(existing)
        raise HTTPException(status_code=409, detail="Thread 创建冲突") from exc
    binding = await resolve_session_workdir_binding(agent_session=agent_session, uid=scope.uid, db=db, project=project)
    if input_messages:
        receipt, dispatch = await accept_locked(
            db=db,
            scope=scope,
            agent_session=agent_session,
            idempotency_key=idempotency_key,
            event_type="yuxi.session.create",
            intent_hash=intent_hash,
            mode="follow_up",
            messages=input_messages,
            model_spec=model_spec,
            tool_approval_mode=tool_approval_mode,
            attachment_file_ids=attachment_file_ids or [],
            source=source,
            channel=channel,
            external_id=external_id,
            origin_metadata=origin_metadata,
            binding=binding,
        )
    else:
        receipt = await receipt_repo.create(
            receipt_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            uid=scope.uid,
            app_id=scope.app_id,
            thread_id=thread_id,
            event_type="yuxi.session.create",
            intent_hash=intent_hash,
        )
        dispatch = None
    await db.commit()
    if binding.materialize_managed:
        ensure_bound_user_workdir(binding.uid, binding.workdir_path)
    if dispatch is not None:
        await deliver(dispatch)
    return _accepted(receipt)


async def accept_message(
    *,
    db: AsyncSession,
    scope: ActorScope,
    thread_id: str,
    idempotency_key: str,
    mode: Literal["follow_up", "steer"] | None,
    messages: list[AgentRunInputMessage],
    model_spec: str | None = None,
    tool_approval_mode: str | None = None,
    attachment_file_ids: list[str] | None = None,
    source: str = "public_api",
    channel: str = "api",
    external_id: str | None = None,
    origin_metadata: dict | None = None,
) -> dict:
    """锁定 Thread 后接收消息，提交新事实，再投递已领取的 Run。"""
    _check_idempotency_key(idempotency_key)
    if not messages:
        raise HTTPException(status_code=422, detail="输入消息不能为空")
    if mode not in {None, "follow_up", "steer"}:
        raise HTTPException(status_code=422, detail="不支持的输入模式")
    intent_hash = _intent_hash(
        "agent.session.input.message",
        mode,
        model_spec,
        tool_approval_mode,
        attachment_file_ids or [],
        external_id,
        origin_metadata or {},
        [_message_intent(item) for item in messages],
    )
    receipt_repo = AgentInputReceiptRepository(db)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, "agent.session.input.message", intent_hash)
        return _accepted(existing)

    agent_session = await require_thread(db=db, scope=scope, thread_id=thread_id, lock=True)
    existing = await receipt_repo.get_for_scope(
        uid=scope.uid, app_id=scope.app_id, thread_id=thread_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        _require_replay(existing, "agent.session.input.message", intent_hash)
        return _accepted(existing)

    receipt, dispatch = await accept_locked(
        db=db,
        scope=scope,
        agent_session=agent_session,
        idempotency_key=idempotency_key,
        event_type="agent.session.input.message",
        intent_hash=intent_hash,
        mode=mode,
        messages=messages,
        model_spec=model_spec,
        tool_approval_mode=tool_approval_mode,
        attachment_file_ids=attachment_file_ids or [],
        source=source,
        channel=channel,
        external_id=external_id,
        origin_metadata=origin_metadata,
    )
    await db.commit()
    if dispatch is not None:
        await deliver(dispatch)
    return _accepted(receipt)


async def get_input_snapshot(*, db: AsyncSession, scope: ActorScope, thread_id: str, input_id: str) -> dict:
    """按完整作用域回读 Input 的接收与消费事实。"""
    agent_session = await SessionRepository(db).get_session_by_thread_id(thread_id)
    if agent_session is None or agent_session.uid != scope.uid or agent_session.app_id != scope.app_id:
        raise HTTPException(status_code=404, detail="Thread 不存在")
    input_item = await AgentInputRepository(db).get_for_scope(
        input_id=input_id, thread_id=thread_id, uid=scope.uid, app_id=scope.app_id
    )
    if input_item is None:
        raise HTTPException(status_code=404, detail="Input 不存在")
    messages = await AgentInputRepository(db).list_messages(input_id)
    for message in messages:
        await db.refresh(message, attribute_names=["tool_calls"])
    return {
        "input_id": input_item.id,
        "thread_id": thread_id,
        "kind": input_item.kind,
        "status": input_item.status,
        "turn_id": input_item.turn_id,
        "run_id": input_item.consumed_run_id,
        "received_seq": input_item.received_seq,
        "cutoff_seq": input_item.cutoff_seq,
        "items": [item for message in messages for item in serialize_public_items(message, None, None)],
    }


async def accept_locked(
    *,
    db: AsyncSession,
    scope: ActorScope,
    agent_session: Session,
    idempotency_key: str,
    event_type: str,
    intent_hash: str,
    mode: Literal["follow_up", "steer"] | None,
    messages: list[AgentRunInputMessage],
    model_spec: str | None,
    tool_approval_mode: str | None,
    attachment_file_ids: list[str],
    source: str,
    channel: str,
    external_id: str | None,
    origin_metadata: dict | None,
    binding=None,
    frozen_payload: dict | None = None,
) -> tuple[AgentInputReceipt, Dispatch | None]:
    """在调用方事务与 Thread 锁内保存回执、消息和 Input。"""
    if agent_session.status not in {"active", "subagent"}:
        raise HTTPException(status_code=409, detail="Thread 已归档")
    turn_repo = AgentTurnRepository(db)
    active_turn = await turn_repo.lock_active_for_thread(
        thread_id=agent_session.thread_id, uid=scope.uid, app_id=scope.app_id
    )
    if active_turn is not None and active_turn.status in {"waiting", "cancelling"}:
        raise HTTPException(status_code=409, detail="当前 Turn 正在等待控制输入或取消清理")

    mode = mode or ("steer" if active_turn is not None else "follow_up")
    input_repo = AgentInputRepository(db)
    if mode == "steer" and (model_spec is not None or tool_approval_mode is not None):
        raise HTTPException(status_code=422, detail="Steer 不指定模型或审批配置")
    input_item = (
        await input_repo.get_pending_steer(thread_id=agent_session.thread_id, uid=scope.uid, app_id=scope.app_id)
        if mode == "steer"
        else None
    )

    if input_item is None:
        user = await db.scalar(select(User).where(User.uid == scope.uid))
        agent_item = await AgentRepository(db).get_visible_by_slug(
            slug=agent_session.agent_id, user=user, kind="subagent" if agent_session.status == "subagent" else "main"
        )
        if agent_item is None:
            raise HTTPException(status_code=404, detail="智能体不存在")
        try:
            backend = get_agent_backend(agent_item.backend_id)
        except AgentBackendNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        requested_model = model_spec or (agent_session.extra_metadata or {}).get("model_spec")
        requested_approval = tool_approval_mode or (agent_session.extra_metadata or {}).get("tool_approval_mode")
        resolved_model, approval_mode = await resolve_agent_run_config(
            requested_model, requested_approval, agent_item, backend, db
        )
        input_payload = frozen_payload or {"model_spec": resolved_model, "tool_approval_mode": approval_mode}
        if agent_session.status == "subagent" and frozen_payload is None:
            previous = await AgentRunRepository(db).get_latest_subagent_run_by_thread_for_user(
                agent_session.thread_id, scope.uid
            )
            if previous is None:
                raise ValueError("子 Thread 缺少已授权委派")
            input_payload["runtime"] = dict(previous.input_payload["runtime"])
            input_payload["delegated_attachments"] = list(previous.input_payload.get("delegated_attachments", []))
            # Thread 关系提供执行授权，新用户 Turn 不继承旧 Turn 的委派来源。
            origin_metadata = {"subagent_thread_relation_id": previous.subagent_thread_relation_id}

        input_item = await input_repo.create(
            input_id=str(uuid.uuid4()),
            thread_id=agent_session.thread_id,
            uid=scope.uid,
            app_id=scope.app_id,
            api_key_id=scope.api_key_id,
            agent_slug=agent_session.agent_id,
            kind=mode,
            input_payload=input_payload,
            source=source,
            channel=channel,
            external_id=external_id,
            origin_metadata=origin_metadata,
        )

    receipt = await AgentInputReceiptRepository(db).create(
        receipt_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        uid=scope.uid,
        app_id=scope.app_id,
        thread_id=agent_session.thread_id,
        event_type=event_type,
        intent_hash=intent_hash,
        input_id=input_item.id,
    )
    persisted_messages = []
    attachment_ids: list[str] = list(attachment_file_ids)
    for message in messages:
        metadata = {**message.extra_metadata, "raw_message": message.raw_message(), "input_id": input_item.id}
        attachment_ids.extend(metadata.get("attachment_file_ids") or [])
        persisted = Message(
            session_record_id=agent_session.id,
            role="user",
            content=message.content,
            message_type=message.message_type,
            image_content=message.image_content,
            extra_metadata=metadata,
            delivery_status="queued",
        )
        db.add(persisted)
        persisted_messages.append(persisted)
    await db.flush()
    await input_repo.add_messages(
        input_id=input_item.id, receipt_id=receipt.id, message_ids=[message.id for message in persisted_messages]
    )
    if attachment_ids:
        bound = await SessionRepository(db).bind_attachments_to_input(
            agent_session.id, input_item.id, list(dict.fromkeys(attachment_ids))
        )
        requested_ids = {str(file_id).strip() for file_id in attachment_ids}
        if not requested_ids.issubset({item.get("file_id") for item in bound}):
            raise HTTPException(status_code=422, detail="附件不存在或已绑定其他输入")
        for message in persisted_messages:
            message.extra_metadata = {**message.extra_metadata, "attachments": bound}

    dispatch = None
    if active_turn is None and not agent_session.queue_paused:
        dispatch = await claim_next_input(db=db, agent_session=agent_session, binding=binding)
        if dispatch is not None:
            await db.refresh(receipt)
    return receipt, dispatch


def _check_idempotency_key(idempotency_key: str) -> None:
    """在接收边界校验客户端幂等键。"""
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key 长度必须为 1 至 128")


def _message_intent(message: AgentRunInputMessage) -> dict:
    """提取用于幂等核对的规范化消息意图。"""
    return {
        "content": message.content,
        "message_type": message.message_type,
        "image_content": message.image_content,
        "raw_message": message.raw_message(),
        "metadata": message.extra_metadata,
    }


def _intent_hash(*parts) -> str:
    """对已规范化输入建立跨 Thread/Session 别名共用的意图指纹。"""
    value = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def _require_replay(receipt: AgentInputReceipt, event_type: str, intent_hash: str) -> None:
    """只允许同一命令与同一规范化意图重放。"""
    if receipt.event_type != event_type or receipt.intent_hash != intent_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key 已用于其他输入")


def _accepted(receipt: AgentInputReceipt) -> dict:
    """返回已持久接收事实和已固定的消费归属。"""
    return {
        "event_id": receipt.id,
        "input_id": receipt.input_id,
        "thread_id": receipt.thread_id,
        "turn_id": receipt.turn_id,
        "run_id": receipt.run_id,
        "status": "accepted",
    }
