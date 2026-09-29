"""Session 名称到同一 Public Thread 用例的 HTTP 适配。"""

from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from yuxi.api.routers.public_v1.agents.events import public_stream_response, submit_thread_event
from yuxi.api.routers.public_v1.agents.schemas import (
    CancelEvent,
    CancelInputEvent,
    ContinueEvent,
    MessageEvent,
    ResumeEvent,
    ThreadCreate,
    ThreadEventCreate,
    WireModel,
    input_messages_to_domain,
)
from yuxi.api.dependencies.auth import get_db
from yuxi.modules.agents.services.inputs import create_thread, get_input_snapshot, thread_id_for_creation
from yuxi.modules.agents.services.threads import get_queue_snapshot, get_thread_snapshot, require_thread
from yuxi.modules.agents.services.turns import get_turn_snapshot, list_turn_messages

router = APIRouter(dependencies=[Depends(require_public_context)])


class SessionMessageEvent(MessageEvent):
    """Session 名称的普通消息输入。"""

    type: Literal["agent.session.input.message"]


class SessionResumeEvent(ResumeEvent):
    """Session 名称的等待恢复输入。"""

    type: Literal["yuxi.session.input.resume"]


class SessionCancelEvent(CancelEvent):
    """Session 名称的 Turn 取消输入。"""

    type: Literal["yuxi.session.input.cancel"]


class SessionContinueEvent(ContinueEvent):
    """Session 名称的队列继续输入。"""

    type: Literal["yuxi.session.input.continue"]


class SessionCancelInputEvent(CancelInputEvent):
    """Session 名称的排队 Input 取消。"""

    type: Literal["yuxi.session.input.cancel_input"]


SessionEvent = Annotated[
    SessionMessageEvent | SessionResumeEvent | SessionCancelEvent | SessionContinueEvent | SessionCancelInputEvent,
    Field(discriminator="type"),
]


class SessionEventCreate(WireModel):
    """单次只接收一个 Session 命名事件。"""

    events: list[SessionEvent] = Field(min_length=1, max_length=1)


@router.post("/sessions")
async def create_public_session(
    payload: ThreadCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """调用 Thread 创建用例并映射 Session 字段。"""
    if payload.stream and payload.input is None:
        raise HTTPException(status_code=422, detail="空 Session 不能以流式创建")
    result = await create_thread(
        db=db,
        scope=context.scope,
        agent_slug=payload.agent_id,
        thread_id=thread_id_for_creation(context.scope, idempotency_key),
        idempotency_key=idempotency_key,
        project_id=payload.project_id,
        title=payload.title,
        messages=input_messages_to_domain(payload.input) if payload.input else None,
        model_spec=payload.model_spec,
        tool_approval_mode=payload.tool_approval_mode,
        source="public_api",
        channel="api" if context.api_key else "web",
    )
    thread = await require_thread(db=db, scope=context.scope, thread_id=result["thread_id"])
    response = _session_response({**result, "title": thread.title, "project_id": thread.project_id})
    if not payload.stream:
        return response
    await db.close()
    return public_stream_response(
        scope=context.scope,
        thread_id=result["thread_id"],
        after_cursor=None,
        initial_event=response,
        session_alias=True,
    )


@router.get("/sessions/{session_id}")
async def retrieve_public_session(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """把 Session ID 作为 Thread ID 读取相同快照。"""
    result = await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return _session_response(result)


@router.post("/sessions/{session_id}/events", status_code=202)
async def submit_public_session_event(
    session_id: str,
    payload: SessionEventCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """仅替换 wire 事件名，共用 Thread 幂等键和用例。"""
    raw = payload.events[0].model_dump(mode="json")
    raw["type"] = raw["type"].replace("agent.session.", "agent.thread.").replace("yuxi.session.", "yuxi.thread.")
    event = ThreadEventCreate.model_validate({"events": [raw]}).events[0]
    result = await submit_thread_event(
        db=db,
        scope=context.scope,
        thread_id=session_id,
        event=event,
        idempotency_key=idempotency_key,
    )
    return {"object": "agent.session.event.accepted", "session_id": session_id, **result}


@router.get("/sessions/{session_id}/events")
async def observe_public_session_events(
    session_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """校验同一 Thread 权限后映射事件名并编码一次。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    await db.close()
    return public_stream_response(
        scope=context.scope, thread_id=session_id, after_cursor=last_event_id, session_alias=True
    )


@router.get("/sessions/{session_id}/queue")
async def retrieve_public_session_queue(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取同一 Thread 的队列事实。"""
    return await get_queue_snapshot(db=db, scope=context.scope, thread_id=session_id)


@router.get("/sessions/{session_id}/inputs/{input_id}")
async def retrieve_public_session_input(
    session_id: str,
    input_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按 Session 路径读取同一持久 Input。"""
    return await get_input_snapshot(db=db, scope=context.scope, thread_id=session_id, input_id=input_id)


@router.get("/sessions/{session_id}/turns/{turn_id}")
async def retrieve_public_session_turn(
    session_id: str,
    turn_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按 Session 路径读取同一 Turn。"""
    result = await get_turn_snapshot(db=db, scope=context.scope, thread_id=session_id, turn_id=turn_id)
    return {"object": "agent.session.turn", "session_id": session_id, **result}


@router.get("/sessions/{session_id}/turns/{turn_id}/items")
async def list_public_session_turn_items(
    session_id: str,
    turn_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按 Session 路径读取同一 Turn 的消息。"""
    items = await list_turn_messages(
        db=db,
        scope=context.scope,
        thread_id=session_id,
        turn_id=turn_id,
        after_id=after_id,
        limit=limit,
    )
    return {"items": items}


def _session_response(thread: dict) -> dict:
    """只在 HTTP 出口把 Thread 名称映射为 Session。"""
    return {
        "object": "agent.session",
        "id": thread["thread_id"],
        "session_id": thread["thread_id"],
        **{key: value for key, value in thread.items() if key not in {"thread_id", "id"}},
    }
