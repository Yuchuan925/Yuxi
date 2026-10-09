"""Public Session 创建、读取与归档入口。"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_db
from yuxi.api.routers.public_v1.agents.responses import PUBLIC_ERRORS, SSE_RESPONSE
from yuxi.api.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from yuxi.api.routers.public_v1.agents.schemas import SessionCreate, ThreadUpdate, input_messages_to_domain
from yuxi.modules.agents.services.inputs import create_thread, thread_id_for_creation
from yuxi.modules.agents.services.threads import (
    archive_thread,
    get_queue_snapshot,
    get_thread_snapshot,
    list_threads,
    mark_thread_viewed,
    require_thread,
    search_threads,
    update_thread,
)

router = APIRouter(dependencies=[Depends(require_public_context)], responses=PUBLIC_ERRORS)


@router.get("/sessions")
async def list_public_threads(
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按完整用户与 APP 作用域列出 Thread。"""
    return await list_threads(db=db, scope=context.scope, agent_slug=agent_id, limit=limit, offset=offset)


@router.post("/sessions", summary="创建智能体会话", responses={200: SSE_RESPONSE})
async def create_public_thread(
    payload: SessionCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """创建会话并原子接收可选的首批消息。

    Idempotency-Key 标识本次意图；相同请求重试不会重复创建或执行。
    200 证明持久创建或幂等重放，input_id 用于查询本次输入的消费归属。
    排队时 turn_id/run_id 为空；执行结果通过明确的 Turn 查询取得。
    stream=true 返回长期 SSE，客户端确认目标 Turn 终态后主动关闭订阅。
    """
    if payload.stream and payload.input is None:
        raise HTTPException(status_code=422, detail="空 Thread 不能以流式创建")
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
    response = {
        "object": "agent.session",
        **result,
        "id": result["thread_id"],
        "title": thread.title,
        "project_id": thread.project_id,
    }
    if not payload.stream:
        return response
    from yuxi.api.routers.public_v1.agents.events import public_stream_response

    await db.close()
    return public_stream_response(
        scope=context.scope,
        thread_id=result["thread_id"],
        after_cursor=None,
        initial_event=response,
    )


@router.get("/sessions/search")
async def search_public_threads(
    q: str = Query(..., min_length=1, max_length=200),
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """仅在当前 APP 命名空间搜索历史消息。"""
    return await search_threads(
        query=q,
        agent_slug=agent_id,
        db=db,
        scope=context.scope,
        limit=limit,
        offset=offset,
    )


@router.get("/sessions/{session_id}", summary="读取会话状态")
async def retrieve_public_thread(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取会话及最近一轮工作的持久快照。

    Session ID 与内部 Thread UUID 相同。current_turn 用于查看当前或最近工作；
    本次提交的结果需根据 Input 归属定位 Turn，不能从相邻轮次推断。
    """
    result = await get_thread_snapshot(db=db, scope=context.scope, thread_id=session_id)
    return {"object": "agent.session", **result, "id": session_id}


@router.post("/sessions/{session_id}/viewed")
async def mark_public_thread_viewed(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """在作用域校验后记录用户已查看的当前执行段。"""
    return await mark_thread_viewed(thread_id=session_id, scope=context.scope, db=db)


@router.patch("/sessions/{session_id}")
async def update_public_thread(
    session_id: str,
    payload: ThreadUpdate,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """更新 Thread 的展示字段和后续输入审批模式。"""
    return await update_thread(
        db=db,
        scope=context.scope,
        thread_id=session_id,
        title=payload.title,
        is_pinned=payload.is_pinned,
        tool_approval_mode=payload.tool_approval_mode,
        model_spec=payload.model_spec,
    )


@router.post("/sessions/{session_id}/archive")
async def archive_public_thread(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """检查在途工作后归档 Thread，保留历史事实。"""
    return await archive_thread(db=db, scope=context.scope, thread_id=session_id)


@router.get("/sessions/{session_id}/queue")
async def retrieve_public_queue(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取当前 Thread 的持久 follow-up 队列。"""
    return await get_queue_snapshot(db=db, scope=context.scope, thread_id=session_id)
