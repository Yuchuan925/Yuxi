"""Public Thread 的输入事件与 SSE 编码。"""

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_db
from yuxi.api.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from yuxi.api.routers.public_v1.agents.schemas import (
    CancelEvent,
    CancelInputEvent,
    ContinueEvent,
    MessageEvent,
    ResumeEvent,
    ThreadEvent,
    ThreadEventCreate,
    TreeControlEvent,
    input_messages_to_domain,
)
from yuxi.api.sse import format_sse
from yuxi.modules.agents.services.events import stream_thread_events, validate_event_cursor
from yuxi.modules.agents.services.inputs import accept_message
from yuxi.modules.agents.services.scope import ActorScope
from yuxi.modules.agents.services.threads import cancel_input, continue_queue, get_thread_snapshot
from yuxi.modules.agents.services.turns import cancel_turn, resume_turn

router = APIRouter(dependencies=[Depends(require_public_context)])


@router.post("/threads/{thread_id}/events", status_code=202)
async def submit_public_event(
    thread_id: str,
    payload: ThreadEventCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """把单个 wire 事件提交到同一 Thread 用例。"""
    result = await submit_thread_event(
        db=db,
        scope=context.scope,
        thread_id=thread_id,
        event=payload.events[0],
        idempotency_key=idempotency_key,
    )
    return {"object": "yuxi.session.event.accepted", **result}


async def submit_thread_event(
    *, db: AsyncSession, scope: ActorScope, thread_id: str, event: ThreadEvent, idempotency_key: str
) -> dict:
    """把已规范化事件交给唯一的生命周期用例。"""
    if isinstance(event, TreeControlEvent):
        from yuxi.modules.agents.services.cooperation import control_tree

        result = await control_tree(
            db=db,
            scope=scope,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            stopped=event.type == "yuxi.session.tree.stop",
        )
    elif isinstance(event, MessageEvent):
        result = await accept_message(
            db=db,
            scope=scope,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            mode=event.yuxi.mode,
            messages=input_messages_to_domain(event.input),
            model_spec=event.yuxi.model_spec,
            tool_approval_mode=event.yuxi.tool_approval_mode,
            attachment_file_ids=event.yuxi.attachment_file_ids,
        )
    elif isinstance(event, ResumeEvent):
        result = await resume_turn(
            db=db,
            scope=scope,
            thread_id=thread_id,
            turn_id=event.turn_id,
            waitpoint_id=event.waitpoint_id,
            response=event.response.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
    elif isinstance(event, CancelEvent):
        result = await cancel_turn(
            db=db,
            scope=scope,
            thread_id=thread_id,
            turn_id=event.yuxi.turn_id,
            expected_run_id=event.yuxi.expected_run_id,
            idempotency_key=idempotency_key,
        )
    elif isinstance(event, ContinueEvent):
        result = await continue_queue(db=db, scope=scope, thread_id=thread_id, idempotency_key=idempotency_key)
    elif isinstance(event, CancelInputEvent):
        result = await cancel_input(
            db=db, scope=scope, thread_id=thread_id, input_id=event.input_id, idempotency_key=idempotency_key
        )
    return result


@router.get("/threads/{thread_id}/events")
async def observe_public_events(
    thread_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """校验作用域并在释放请求事务后订阅整个 Thread。"""
    await get_thread_snapshot(db=db, scope=context.scope, thread_id=thread_id)
    await db.close()
    return public_stream_response(scope=context.scope, thread_id=thread_id, after_cursor=last_event_id)


def public_stream_response(
    *,
    scope: ActorScope,
    thread_id: str,
    after_cursor: str | None,
    initial_event: dict | None = None,
) -> StreamingResponse:
    """仅在 HTTP 边界把结构化输出编码为 SSE。"""
    validate_event_cursor(after_cursor)

    async def events():
        """输出可选创建回执并订阅持久生命周期事件。"""
        if initial_event is not None:
            created = {
                "type": "yuxi.session.created",
                "event_id": initial_event["event_id"],
                "session_id": thread_id,
                "yuxi": initial_event,
            }
            yield format_sse(created, event=created["type"])
        async for cursor, event in stream_thread_events(scope=scope, thread_id=thread_id, after_cursor=after_cursor):
            yield format_sse(event, event=event["type"], event_id=cursor)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
