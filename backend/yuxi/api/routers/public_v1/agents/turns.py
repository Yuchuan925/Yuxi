"""Public Input、Turn 与消息查询。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.api.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from yuxi.api.dependencies.auth import get_db
from yuxi.modules.agents.services.inputs import get_input_snapshot
from yuxi.modules.agents.services.messages import get_thread_audits, get_thread_history
from yuxi.modules.agents.services.runs import get_run_snapshot
from yuxi.modules.agents.services.turns import get_turn_snapshot, list_turn_messages

router = APIRouter(dependencies=[Depends(require_public_context)])


@router.get("/threads/{thread_id}/history")
async def retrieve_public_history(
    thread_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取包含排队与取消输入事实的 Thread 历史。"""
    return await get_thread_history(db=db, scope=context.scope, thread_id=thread_id)


@router.get("/threads/{thread_id}/audits")
async def retrieve_public_audits(
    thread_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取当前 Thread 的有界模型与工具审计。"""
    return await get_thread_audits(db=db, scope=context.scope, thread_id=thread_id)


@router.get("/threads/{thread_id}/runs/{run_id}")
async def retrieve_public_run(
    thread_id: str,
    run_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按 Thread 作用域读取指定执行段。"""
    return await get_run_snapshot(db=db, scope=context.scope, thread_id=thread_id, run_id=run_id)


@router.get("/threads/{thread_id}/inputs/{input_id}")
async def retrieve_public_input(
    thread_id: str,
    input_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """断线后按 Input ID 查询固定消费归属。"""
    return await get_input_snapshot(db=db, scope=context.scope, thread_id=thread_id, input_id=input_id)


@router.get("/threads/{thread_id}/turns/{turn_id}")
async def retrieve_public_turn(
    thread_id: str,
    turn_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """读取一轮工作和明确的最终结果。"""
    return await get_turn_snapshot(db=db, scope=context.scope, thread_id=thread_id, turn_id=turn_id)


@router.get("/threads/{thread_id}/turns/{turn_id}/items")
async def list_public_turn_items(
    thread_id: str,
    turn_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按持久 Message ID 读取本轮的原始消息和输出。"""
    items = await list_turn_messages(
        db=db,
        scope=context.scope,
        thread_id=thread_id,
        turn_id=turn_id,
        after_id=after_id,
        limit=limit,
    )
    return {"items": items}
