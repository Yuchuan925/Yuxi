"""Public Input、Turn 与消息查询。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.dependencies.auth import get_db
from yuxi.api.routers.public_v1.agents.responses import PUBLIC_ERRORS
from yuxi.api.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from yuxi.modules.agents.services.inputs import get_input_snapshot
from yuxi.modules.agents.services.messages import get_thread_audits, get_thread_history
from yuxi.modules.agents.services.runs import get_run_snapshot
from yuxi.modules.agents.services.turns import get_turn_snapshot, list_turn_messages

router = APIRouter(dependencies=[Depends(require_public_context)], responses=PUBLIC_ERRORS)


@router.get("/sessions/{session_id}/history")
async def retrieve_public_history(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取包含排队与取消输入事实的 Thread 历史。"""
    return await get_thread_history(db=db, scope=context.scope, thread_id=session_id)


@router.get("/sessions/{session_id}/audits")
async def retrieve_public_audits(
    session_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取当前 Thread 的有界模型与工具审计。"""
    return await get_thread_audits(db=db, scope=context.scope, thread_id=session_id)


@router.get("/sessions/{session_id}/runs/{run_id}")
async def retrieve_public_run(
    session_id: str,
    run_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按 Thread 作用域读取指定执行段。"""
    return await get_run_snapshot(db=db, scope=context.scope, thread_id=session_id, run_id=run_id)


@router.get("/sessions/{session_id}/inputs/{input_id}")
async def retrieve_public_input(
    session_id: str,
    input_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """断线后按 Input ID 查询固定消费归属。"""
    return await get_input_snapshot(db=db, scope=context.scope, thread_id=session_id, input_id=input_id)


@router.get("/sessions/{session_id}/turns/{turn_id}")
async def retrieve_public_turn(
    session_id: str,
    turn_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取一轮工作和明确的最终结果。"""
    return await get_turn_snapshot(db=db, scope=context.scope, thread_id=session_id, turn_id=turn_id)


@router.get("/sessions/{session_id}/turns/{turn_id}/items")
async def list_public_turn_items(
    session_id: str,
    turn_id: str,
    after_id: str | None = Query(default=None, description="上一页末尾的公开 item ID。"),
    limit: int = Query(default=100, ge=1, le=100, description="每页公开 item 数量，范围 1–100。"),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按稳定 item ID 分页读取本轮的公开输入和输出。"""
    items = await list_turn_messages(
        db=db,
        scope=context.scope,
        thread_id=session_id,
        turn_id=turn_id,
        after_id=after_id,
        limit=limit,
    )
    return {"items": items}
