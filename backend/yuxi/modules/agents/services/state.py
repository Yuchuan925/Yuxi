"""按授权 Thread 读取 LangGraph checkpoint 与持久执行关系。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from yuxi.infrastructure.observability.logging import logger
from yuxi.infrastructure.postgres.checkpointer import get_langgraph_checkpointer
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.sessions import SessionRepository
from yuxi.modules.agents.repositories.subagents import SubagentThreadRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.services.execution import build_pending_interrupt_payload, extract_agent_state
from yuxi.modules.agents.services.subagents import serialize_subagent_run_state
from yuxi.modules.identity.models import User


async def _read_checkpoint_state(*, uid: str, thread_id: str) -> tuple[dict, Any | None]:
    """读取完整 checkpoint 快照与同批中断；调用方先校验线程可见性。"""
    checkpointer = get_langgraph_checkpointer(pg_manager)
    saved = await checkpointer.aget_tuple({"configurable": {"uid": uid, "thread_id": thread_id, "checkpoint_ns": ""}})
    if saved is None:
        return {}, None

    # 面板只展示完整快照，pending writes 中的业务增量留给执行图合并。
    interrupt_info = None
    for _task_id, channel, interrupts in saved.pending_writes or []:
        if channel == "__interrupt__" and interrupts:
            interrupt_info = interrupts[0]
            break
    return saved.checkpoint["channel_values"], interrupt_info


async def get_agent_state_view(
    *,
    thread_id: str,
    current_user: User,
    db,
    app_id: str | None = None,
    include_messages: bool = False,
    include_relations: bool = True,
) -> dict:
    """按用户和 APP 作用域读取 checkpoint 及持久执行关系。"""
    current_uid = str(current_user.uid)
    session_repo = SessionRepository(db)
    run_repo = AgentRunRepository(db)
    agent_session = await session_repo.get_session_by_thread_id(thread_id)
    if agent_session:
        if agent_session.uid != str(current_uid) or agent_session.app_id != app_id or agent_session.status == "deleted":
            raise HTTPException(status_code=404, detail="对话线程不存在")

        latest_run = await run_repo.get_latest_run_by_thread_for_user(thread_id, current_uid)
        values, interrupt_info = await _read_checkpoint_state(uid=current_uid, thread_id=thread_id)
        response = {"agent_state": extract_agent_state(values)}
        if latest_run and latest_run.status == "interrupted" and interrupt_info:
            turn = await AgentTurnRepository(db).get_for_scope(
                turn_id=latest_run.turn_id,
                thread_id=thread_id,
                uid=current_uid,
                app_id=app_id,
            )
            if (
                turn
                and turn.status == "waiting"
                and turn.current_run_id == latest_run.id
                and turn.waitpoint
                and turn.waitpoint["run_id"] == latest_run.id
            ):
                response["interrupt"] = {
                    **build_pending_interrupt_payload(interrupt_info, thread_id),
                    "run_id": latest_run.id,
                }
        if include_relations:
            # checkpoint 保存模型上下文；页面加载以持久 Run 的身份与状态为准。
            child_runs = await run_repo.list_subagent_runs_for_session(agent_session.id, current_uid)
            response["agent_state"]["subagent_runs"] = [serialize_subagent_run_state(run) for run in child_runs]
            relation = await SubagentThreadRepository(db).get_by_child_session_for_user(
                agent_session.id,
                str(current_uid),
            )
            if relation:
                parent_session = await session_repo.get_session_by_id(relation.parent_session_record_id)
                if (
                    not parent_session
                    or parent_session.uid != str(current_uid)
                    or parent_session.app_id != app_id
                    or parent_session.status == "deleted"
                ):
                    raise HTTPException(status_code=404, detail="父对话线程不存在")
                response["parent_thread_id"] = parent_session.thread_id
                response["subagent_thread"] = relation.to_dict()
                latest_run = await run_repo.get_latest_subagent_run_by_thread_for_user(
                    thread_id,
                    str(current_uid),
                )
                if latest_run:
                    try:
                        response["subagent_run"] = serialize_subagent_run_state(latest_run)
                    except ValueError as exc:
                        logger.error(f"子智能体运行记录格式异常: thread_id={thread_id}, run_id={latest_run.id}, {exc}")
                        raise HTTPException(status_code=500, detail="子智能体运行记录格式异常") from exc
        if include_messages:
            from yuxi.modules.agents.repositories.public_items import PublicItemRepository
            from yuxi.modules.agents.services.public_items import serialize_public_items

            rows = await PublicItemRepository(db).list_items(thread_id=thread_id, uid=current_uid, app_id=app_id)
            response["items"] = [
                item for message, run, result_id in rows for item in serialize_public_items(message, run, result_id)
            ]
        return response

    # 子智能体线程在创建时必然同时写入子对话与线程关系（见 SubagentRunService.start），
    # 由上面的 agent_session 分支统一处理；走到这里说明该 thread 没有对应对话，即线程不存在。
    raise HTTPException(status_code=404, detail="对话线程不存在")
