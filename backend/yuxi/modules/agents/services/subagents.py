"""子智能体线程关系和根 Turn 下的子 Run 创建。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.modules.agents.runtime.tool_approval import DEFAULT_TOOL_APPROVAL_MODE
from yuxi.modules.agents.repositories.definitions import AgentRepository
from yuxi.modules.agents.repositories.runs import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.modules.workspace.repositories.projects import ProjectRepository
from yuxi.modules.agents.repositories.subagents import SubagentThreadRepository
from yuxi.modules.agents.services.input_config import load_agent_run_context, resolve_agent_run_model_spec
from yuxi.modules.agents.services.input_messages import AgentRunInputMessage
from yuxi.modules.agents.services.transport import (
    enqueue_agent_run,
    list_recent_run_stream_events,
    publish_cancel_signals,
)
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.threads import SubagentThread
from yuxi.shared.datetime import format_utc_datetime
from yuxi.shared.hashing import hash_id, subagent_child_thread_id
from yuxi.infrastructure.observability.logging import logger


@dataclass(frozen=True)
class SubagentStartResult:
    run: AgentRun
    created: bool
    continuing: bool
    relation: SubagentThread


@dataclass
class SubagentRunBusy(Exception):
    thread_id: str
    active_run_id: str | None
    active_run_status: str | None
    message: str | None

    def to_payload(self) -> dict:
        return {
            "status": "busy",
            "thread_id": self.thread_id,
            "active_run_id": self.active_run_id,
            "active_run_status": self.active_run_status,
            "message": self.message,
        }


class AgentRunWaitTimeout(Exception):
    """等待子执行超时且 Run 仍未终结。"""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(f"AgentRun {result.get('agent_run_id')} 尚未终结")


def subagent_run_urls(run_id: str, thread_id: str) -> dict[str, str]:
    """生成子智能体 run 对外暴露的事件流和结果查询 URL。"""
    return {
        "events_url": f"/api/v1/agents/threads/{thread_id}/events",
        "result_url": f"/api/v1/agents/threads/{thread_id}/runs/{run_id}",
    }


def serialize_subagent_run_state(run: AgentRun) -> dict:
    """序列化给父智能体状态使用的子智能体 run 摘要。

    任务描述不在此冗余存储：其唯一来源是父对话里 `subagent_start`（或历史 `task`）工具调用的入参，
    前端面板按 tool_call_id 回填展示。
    """
    payload = run.input_payload
    if not isinstance(payload, dict):
        raise ValueError("subagent run 缺少 input_payload")
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("subagent run 缺少 runtime")
    tool_call_id = str(runtime.get("tool_call_id") or "").strip()
    if not tool_call_id:
        raise ValueError("subagent run 缺少 tool_call_id")

    state = {
        "id": tool_call_id,
        "run_id": run.id,
        "subagent_slug": run.agent_slug,
        "subagent_name": runtime.get("subagent_name"),
        "child_thread_id": run.conversation_thread_id,
        "status": run.status,
        "created_at": format_utc_datetime(run.created_at),
        "completed_at": format_utc_datetime(run.finished_at),
        "error": run.error_message,
        **subagent_run_urls(run.id, run.conversation_thread_id),
    }
    return {key: value for key, value in state.items() if value is not None}


async def get_agent_run_result(*, run_id: str, current_uid: str, db: AsyncSession) -> dict:
    """只从 Run 明确绑定的输出消息读取子执行结果。"""
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_uid))
    if run is None:
        return {
            "status": "failed",
            "agent_run_id": run_id,
            "output": "",
            "error": {"type": "run_not_found", "message": "运行任务不存在"},
        }
    output = await db.get(Message, run.output_message_id) if run.output_message_id else None
    if output is not None and (
        output.run_id != run.id or output.turn_id != run.turn_id or output.conversation_id != run.conversation_id
    ):
        raise ValueError("Run 输出消息归属不一致")
    result = {
        "status": run.status,
        "output": output.content if output else "",
        "agent_slug": run.agent_slug,
        "thread_id": run.conversation_thread_id,
        "conversation_id": run.conversation_id,
        "agent_run_id": run.id,
        "turn_id": run.turn_id,
        "final_message_id": output.id if output else None,
        "langfuse_trace_id": run.langfuse_trace_id,
        "token_usage": run.token_usage or {},
    }
    if run.error_type or run.error_message:
        result["error"] = {"type": run.error_type, "message": run.error_message}
    return result


async def get_agent_run_progress(run_id: str, *, message_limit: int = 3) -> dict:
    """从短期 Run 事件读取子工具的最近文本进度。"""
    try:
        events = await list_recent_run_stream_events(run_id, limit=100)
    except Exception as exc:
        logger.warning("读取 Run 进度失败: run=%s error=%s", run_id, exc)
        return {"last_seq": "0-0", "messages": []}
    messages: list[dict] = []
    for event in events:
        if event.get("event_type") != "messages":
            continue
        payload = event.get("payload", {}).get("payload", {})
        chunks = payload.get("items") if isinstance(payload.get("items"), list) else [payload.get("chunk")]
        for chunk in reversed(chunks):
            stream_event = chunk.get("stream_event") if isinstance(chunk, dict) else None
            if not isinstance(stream_event, dict):
                continue
            kind = stream_event.get("type")
            if kind == "message_delta":
                content = (
                    stream_event.get("content")
                    or stream_event.get("reasoning_content")
                )
                progress_kind = "assistant_message" if stream_event.get("content") else "assistant_reasoning"
            elif kind in {"tool_call", "tool_call_delta"}:
                tool_name = stream_event.get("name") or stream_event.get("tool_call_id") or "工具"
                content = f"调用工具 {tool_name}" if kind == "tool_call" else f"正在准备工具 {tool_name}"
                progress_kind = kind
            else:
                continue
            if content and str(content).strip():
                item = {"content": str(content).strip()[:800], "kind": progress_kind, "seq": event.get("seq")}
                for key in ("message_id", "tool_call_id"):
                    if stream_event.get(key):
                        item[key] = str(stream_event[key])
                messages.append(item)
            if len(messages) >= message_limit:
                break
        if len(messages) >= message_limit:
            break
    return {"last_seq": events[0]["seq"] if events else "0-0", "messages": list(reversed(messages))}


async def await_agent_run_result(*, run_id: str, current_uid: str) -> dict:
    """有限等待子 Run 终态；超时仍返回明确的非终态错误。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30 * 60
    while True:
        async with pg_manager.get_async_session_context() as db:
            result = await get_agent_run_result(run_id=run_id, current_uid=current_uid, db=db)
        if result["status"] in TERMINAL_RUN_STATUSES:
            return result
        if loop.time() >= deadline:
            raise AgentRunWaitTimeout(result)
        await asyncio.sleep(0.5)


async def request_cancel_agent_run(*, run_id: str, current_uid: str, db: AsyncSession):
    """供已验证父 Run 关系的子工具取消目标子执行。"""
    repo = AgentRunRepository(db)
    run = await repo.get_run_for_user(run_id, str(current_uid))
    if run is None or run.run_type != "subagent":
        raise HTTPException(status_code=404, detail="子执行不存在")
    run, cancelled_ids = await repo.request_cancel_execution_tree(
        run_id=run_id, uid=str(current_uid), cascade_descendants=False
    )
    await db.commit()
    await publish_cancel_signals(cancelled_ids)
    return run


class SubagentRunService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.run_repo = AgentRunRepository(db)
        self.conv_repo = ConversationRepository(db)
        self.project_repo = ProjectRepository(db)
        self.thread_repo = SubagentThreadRepository(db)

    async def start(
        self,
        *,
        uid: str,
        created_by_run_id: str,
        agent_item: Agent,
        input_message: AgentRunInputMessage,
        tool_call_id: str,
        requested_thread_id: str | None = None,
    ) -> SubagentStartResult:
        """启动或继续一个后台子智能体 run，并在新建时入队 worker。"""

        creator_run = await self.run_repo.get_run_for_user(created_by_run_id, uid)
        if not creator_run:
            raise ValueError("父运行任务不存在")
        root_snapshot = await self.conv_repo.get_conversation_by_thread_id(creator_run.runtime_scope_id)
        if (
            root_snapshot is None
            or root_snapshot.uid != uid
            or root_snapshot.app_id != creator_run.app_id
            or root_snapshot.status != "active"
        ):
            raise ValueError("父运行的根 Thread 不存在")
        # 与 Agent 删除和普通 Thread 创建同序：Agent → Project → Thread。
        locked_agent = await AgentRepository(self.db).get_by_slug(agent_item.slug, for_key_share=True)
        if locked_agent is None or locked_agent.id != agent_item.id or not locked_agent.is_subagent:
            raise ValueError("子智能体不存在")
        agent_item = locked_agent
        project = await self.project_repo.lock_active_for_user(root_snapshot.project_id, uid)
        if project is None:
            raise ValueError("父运行任务的 Project 不存在")
        root_thread = await self.conv_repo.lock_conversation_by_thread_id(creator_run.runtime_scope_id)
        if (
            root_thread is None
            or root_thread.uid != uid
            or root_thread.app_id != creator_run.app_id
            or root_thread.status != "active"
            or root_thread.id != creator_run.conversation_id
            or root_thread.thread_id != creator_run.conversation_thread_id
            or root_thread.project_id != project.id
        ):
            raise ValueError("父运行的根 Thread 不存在")
        turn = await AgentTurnRepository(self.db).get_for_scope(
            turn_id=creator_run.turn_id,
            thread_id=root_thread.thread_id,
            uid=uid,
            app_id=creator_run.app_id,
            for_update=True,
        )
        if turn is None or turn.current_run_id != creator_run.id or turn.status != "running":
            raise ValueError("父运行的 Turn 不再接受子执行")
        creator_run = await self.run_repo.lock_run_for_user(created_by_run_id, uid)
        if getattr(creator_run, "status", "running") != "running":
            raise ValueError("父运行已结束，不能再创建子智能体")
        if getattr(creator_run, "run_type", None) == "subagent":
            raise ValueError("子智能体不能创建子智能体")

        child_thread_id = str(requested_thread_id or "").strip()
        continuing = bool(child_thread_id)  # 表示是继续运行已有子智能体线程，而非新建子线程
        if not child_thread_id:
            child_thread_id = subagent_child_thread_id(
                creator_run.conversation_thread_id,
                agent_item.slug,
                tool_call_id,
            )

        # 1. 确保子线程有对应 conversation，必要时创建 subagent 对话
        # 2. 确保父子线程关系存在，必要时创建 SubagentThread 记录；relation 是后台子 run 的线程归属来源
        relation = await self._ensure_thread_relation(
            child_thread_id=child_thread_id,
            uid=uid,
            agent_item=agent_item,
            creator_run=creator_run,
            continuing=continuing,
        )

        # 创建数据库记录
        run, created = await self._create_run_record(
            input_message=input_message,
            current_uid=uid,
            creator_run=creator_run,
            relation=relation,
            agent_item=agent_item,
            tool_call_id=tool_call_id,
        )

        # 创建成功后入队 worker 执行；幂等命中已有 run 时不重复入队。
        if created:
            await self.db.commit()
            await enqueue_agent_run(run.id)

        return SubagentStartResult(
            run=run,
            created=created,
            continuing=continuing,
            relation=relation,
        )

    async def get_run_for_creator(self, *, uid: str, created_by_run_id: str, run_id: str) -> AgentRun:
        """在父 run 作用域内读取子智能体 run，防止工具访问其它对话的子任务。"""
        execution_pair = await self.run_repo.get_subagent_run_with_creator(
            uid=uid,
            created_by_run_id=created_by_run_id,
            run_id=run_id,
        )
        if not execution_pair:
            raise ValueError("子智能体运行不存在或不属于当前父运行")
        _creator_run, run = execution_pair
        return run

    async def _create_run_record(
        self,
        *,
        input_message: AgentRunInputMessage,
        current_uid: str,
        creator_run: AgentRun,
        relation: SubagentThread,
        agent_item: Agent,
        tool_call_id: str,
    ) -> tuple[AgentRun, bool]:
        """创建后台子智能体 run，并把规范化输入消息保存为该 run 的输入。"""
        if not input_message.content:
            raise HTTPException(status_code=422, detail="input_message 不能为空")

        child_conversation = await self.conv_repo.get_conversation_by_thread_id(relation.child_thread_id)
        if child_conversation is None or child_conversation.id != relation.child_conversation_id:
            raise ValueError("subagent thread relation 与本次运行不匹配")
        run_id = hash_id("subrun:", f"{creator_run.id}:{relation.child_thread_id}:{tool_call_id}", length=64)
        existing = await self.run_repo.get_run(run_id)
        if existing is not None:
            if existing.created_by_run_id != creator_run.id or existing.subagent_thread_relation_id != relation.id:
                raise ValueError("子执行幂等键冲突")
            return existing, False
        busy = await self.run_repo.get_active_run_by_thread_for_user(
            agent_slug=relation.subagent_slug,
            conversation_thread_id=relation.child_thread_id,
            uid=current_uid,
        )
        if busy is not None:
            raise SubagentRunBusy(relation.child_thread_id, busy.id, busy.status, "子智能体线程已有执行")
        if creator_run.conversation_id != relation.parent_conversation_id:
            raise ValueError("subagent thread relation 与本次运行不匹配")

        from yuxi.modules.agents.runtime.builtin import get_agent_backend

        context = load_agent_run_context(agent_item, get_agent_backend(agent_item.backend_id))
        resolved_model_spec = await resolve_agent_run_model_spec(
            getattr(context, "model", None),
            creator_run.input_payload.get("model_spec"),
            self.db,
        )
        runtime_payload = {
            "tool_call_id": tool_call_id,
            "subagent_name": agent_item.name,
            "parent_thread_id": creator_run.conversation_thread_id,
        }
        input_payload = {
            "model_spec": resolved_model_spec,
            "tool_approval_mode": creator_run.input_payload.get("tool_approval_mode", DEFAULT_TOOL_APPROVAL_MODE),
            "runtime": {key: value for key, value in runtime_payload.items() if value is not None},
        }
        subagent_input_message = input_message.with_metadata(
            {
                "source": "subagent",
                "raw_message": input_message.raw_message(),
            }
        )
        persisted_input_message = await self.conv_repo.add_message(
            conversation_id=child_conversation.id,
            role="user",
            content=subagent_input_message.content,
            message_type=subagent_input_message.message_type,
            extra_metadata=subagent_input_message.extra_metadata,
            image_content=subagent_input_message.image_content,
            turn_id=creator_run.turn_id,
            delivery_status="dispatched",
            commit=False,
        )
        run = await self.run_repo.create_run(
            run_id=run_id,
            agent_slug=relation.subagent_slug,
            conversation_thread_id=relation.child_thread_id,
            runtime_scope_id=getattr(creator_run, "runtime_scope_id", None) or creator_run.conversation_thread_id,
            uid=current_uid,
            turn_id=creator_run.turn_id,
            app_id=creator_run.app_id,
            api_key_id=creator_run.api_key_id,
            conversation_id=child_conversation.id,
            run_type="subagent",
            input_payload=input_payload,
            input_message_id=persisted_input_message.id,
            created_by_run_id=creator_run.id,
            subagent_thread_relation_id=relation.id,
            source="subagent",
            channel="internal",
        )
        persisted_input_message.run_id = run.id
        await self.db.flush()
        return run, True

    async def _ensure_child_conversation(
        self,
        *,
        child_thread_id: str,
        uid: str,
        agent_item: Agent,
        creator_run: AgentRun,
        parent_project_id: str,
    ):
        """确保子线程有对应 conversation；新线程会创建标记为 subagent 的对话。"""
        conversation = await self.conv_repo.get_conversation_by_thread_id(child_thread_id)
        if conversation:
            if conversation.uid != str(uid) or conversation.app_id != creator_run.app_id:
                raise ValueError("子智能体线程不存在")
            if conversation.status != "subagent":
                raise ValueError(f"子智能体线程 {child_thread_id} 已被普通对话占用")
            if conversation.agent_id != agent_item.slug:
                raise ValueError(f"子智能体线程 {child_thread_id} 属于智能体 {conversation.agent_id}")
            if conversation.project_id != parent_project_id:
                raise ValueError("子智能体线程与父对话的 Workdir 不一致")
            return conversation

        conversation = await self.conv_repo.add_conversation(
            uid=uid,
            agent_id=agent_item.slug,
            title=f"SubAgent: {agent_item.name}",
            thread_id=child_thread_id,
            metadata={
                "source": "subagent",
                "parent_thread_id": creator_run.conversation_thread_id,
                "created_by_run_id": creator_run.id,
                "parent_conversation_id": creator_run.conversation_id,
                "subagent_slug": agent_item.slug,
            },
            project_id=parent_project_id,
            app_id=creator_run.app_id,
        )
        conversation.status = "subagent"
        await self.db.flush()
        return conversation

    def _validate_thread_relation(
        self,
        relation: SubagentThread,
        *,
        child_thread_id: str,
        agent_item: Agent,
        creator_run: AgentRun,
    ) -> None:
        """校验已有子线程关系仍属于当前父对话和子智能体。"""
        if relation.parent_conversation_id != creator_run.conversation_id:
            raise ValueError(f"子智能体线程 {child_thread_id}：线程不属于当前对话")
        if relation.subagent_slug != agent_item.slug:
            raise ValueError(f"子智能体线程 {child_thread_id} 属于子智能体 {relation.subagent_slug or '未知'}")

    async def _ensure_thread_relation(
        self,
        *,
        child_thread_id: str,
        uid: str,
        agent_item: Agent,
        creator_run: AgentRun,
        continuing: bool,
    ) -> SubagentThread:
        """读取或创建父子线程关系；relation 是后台子 run 的线程归属来源。"""
        if creator_run.conversation_id is None:
            raise ValueError("父运行任务缺少 conversation_id，无法创建子智能体线程关系")
        parent_conversation = await self.conv_repo.get_conversation_by_id(creator_run.conversation_id)
        if parent_conversation is None or parent_conversation.uid != str(uid):
            raise ValueError("父运行任务的 Conversation 不存在")
        parent_project = await self.project_repo.lock_active_for_user(
            parent_conversation.project_id,
            str(uid),
        )
        if parent_project is None:
            raise ValueError("父运行任务的 Project 不存在")
        parent_conversation = await self.conv_repo.lock_conversation_by_thread_id(creator_run.conversation_thread_id)
        if (
            parent_conversation is None
            or parent_conversation.id != creator_run.conversation_id
            or parent_conversation.uid != str(uid)
            or parent_conversation.status != "active"
            or parent_conversation.app_id != creator_run.app_id
            or parent_conversation.project_id != parent_project.id
        ):
            raise ValueError("父运行任务的 Conversation 不存在")
        parent_project_id = parent_project.id

        existing = await self.thread_repo.get_by_child_thread_for_user(child_thread_id, uid)
        if existing:
            self._validate_thread_relation(
                existing,
                child_thread_id=child_thread_id,
                agent_item=agent_item,
                creator_run=creator_run,
            )
            child_conversation = await self.conv_repo.get_conversation_by_id(existing.child_conversation_id)
            if (
                child_conversation is None
                or child_conversation.uid != str(uid)
                or child_conversation.status != "subagent"
                or child_conversation.app_id != creator_run.app_id
            ):
                raise ValueError("子智能体线程不存在")
            if child_conversation.project_id != parent_project_id:
                raise ValueError("子智能体线程与父对话的 Workdir 不一致")
            return existing
        if continuing:
            raise ValueError(f"无法继续子智能体线程 {child_thread_id}：当前对话中没有找到对应的运行记录")
        child_conversation = await self._ensure_child_conversation(
            child_thread_id=child_thread_id,
            uid=uid,
            agent_item=agent_item,
            creator_run=creator_run,
            parent_project_id=parent_project_id,
        )
        return await self.thread_repo.create(
            uid=uid,
            parent_conversation_id=creator_run.conversation_id,
            child_conversation_id=child_conversation.id,
            child_thread_id=child_thread_id,
            subagent_slug=agent_item.slug,
            created_by_run_id=creator_run.id,
        )
