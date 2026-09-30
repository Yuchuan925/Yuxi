"""Worker 的 LangGraph 执行、输出持久化与状态投影边界。"""

from yuxi.infrastructure.observability.langfuse import flush_langfuse


import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from langchain.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command
from yuxi.modules.agents.runtime.base import json_safe
from yuxi.modules.agents.runtime.agent_backends import get_agent_backend
from yuxi.modules.agents.runtime.callbacks.model_request_timing import FirstModelRequestRecorder
from yuxi.modules.agents.runtime.context import BaseContext
from yuxi.modules.agents.runtime.state import AgentStatePayload
from yuxi.modules.models.utils import parse_assistant_message_body
from yuxi.modules.agents.repositories.definitions import AgentRepository
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.modules.agents.services.input_messages import AgentRunInputMessage
from yuxi.modules.agents.services.messages import save_messages_from_langgraph_state, save_partial_message
from yuxi.modules.agents.services.preparation import PreparedRunExecution
from yuxi.modules.agents.services.attachments import serialize_attachment
from yuxi.modules.agents.services.tracing import (
    LangfuseRunContext,
    attach_run_observation,
    build_run_context,
    finish_run_observation,
    get_trace_info,
    start_turn_observation,
)
from yuxi.modules.agents.services.model_audit import ModelMessageAuditCollector
from yuxi.modules.agents.services.tool_audit import ToolMessageAuditCollector
from yuxi.modules.workspace.services.bindings import resolve_conversation_workdir_path
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.threads import Conversation
from yuxi.modules.identity.models import User
from yuxi.shared.hashing import hash_id
from yuxi.infrastructure.observability.logging import logger
from yuxi.modules.agents.runtime.questions import normalize_questions as _normalize_interrupt_questions
from yuxi.modules.agents.runtime.thread_metadata import extract_thread_id as _metadata_thread_id


def _with_attachment_context(message: HumanMessage, attachments: list[dict]) -> HumanMessage:
    """把线程附件路径追加到本轮模型输入，不污染持久化用户消息。"""
    attachment_lines = [
        f"- {item.get('file_name') or '未知文件'}: {item['path']}"
        for item in attachments
        if isinstance(item.get("path"), str) and item["path"].strip()
    ]
    if not attachment_lines:
        return message

    context = "\n".join(
        [
            "<attachment_context>",
            "以下是本线程当前可用的历史附件。需要内容时，请使用 read_file 读取对应路径：",
            *attachment_lines,
            "</attachment_context>",
        ]
    )
    if isinstance(message.content, str):
        content: str | list = f"{message.content}\n\n{context}"
    else:
        content = [*message.content, {"type": "text", "text": context}]
    return message.model_copy(update={"content": content})


def _build_langfuse_run_context(
    *,
    current_user,
    thread_id: str,
    agent_id: str,
    turn_id: str,
    run_id: str,
    operation: str,
    backend_id: str | None = None,
    message_type: str | None = None,
    meta: dict | None = None,
) -> LangfuseRunContext:
    """为当前执行段建立同一 Turn 的 trace 上下文。"""
    return build_run_context(
        user_id=str(getattr(current_user, "uid", None) or getattr(current_user, "id", "")),
        thread_id=thread_id,
        agent_id=agent_id,
        turn_id=turn_id,
        run_id=run_id,
        operation=operation,
        backend_id=backend_id,
        message_type=message_type,
        username=getattr(current_user, "username", None),
        login_user_id=getattr(current_user, "uid", None),
        department_id=getattr(current_user, "department_id", None),
        parent_observation_id=(meta or {}).get("langfuse_root_observation_id"),
    )


def _build_model_message_audit_collector(meta: dict, thread_id: str) -> ModelMessageAuditCollector | None:
    """仅为具备完整 AgentRun 因果归属的 worker 流创建 Model 审计器。"""
    run_id = str(meta.get("run_id") or "").strip()
    worker_id = str(meta.get("worker_id") or "").strip()
    if not run_id or not worker_id:
        return None
    return ModelMessageAuditCollector(
        run_id=run_id,
        thread_id=thread_id,
        worker_id=worker_id,
    )


def _build_tool_message_audit_collector(
    model_audit: ModelMessageAuditCollector | None,
) -> ToolMessageAuditCollector | None:
    """复用已校验的 AgentRun 因果归属创建 ToolMessage 审计器。"""
    if model_audit is None:
        return None
    return ToolMessageAuditCollector(
        run_id=model_audit.run_id,
        thread_id=model_audit.thread_id,
        worker_id=model_audit.worker_id,
    )


def _is_root_tool_audit_event(event: dict[str, Any], thread_id: str) -> bool:
    """只接受根 StreamMux 或已明确路由回当前线程的 Tool lifecycle。"""
    namespace = event.get("namespace") or []
    event_thread_id = event.get("thread_id")
    return event_thread_id == thread_id or (not namespace and not event_thread_id)


async def _flush_langfuse_best_effort(*, timeout: float = 2) -> None:
    """限制可选追踪网络等待，不延迟 Run 的持久终态与事件发布。"""
    try:
        await asyncio.wait_for(asyncio.to_thread(flush_langfuse), timeout=timeout)
    except TimeoutError:
        logger.warning("刷新 Langfuse 超时，后台导出仍会继续")


async def _persist_agent_run_langfuse_trace(*, db, meta: dict, run_context: LangfuseRunContext) -> None:
    """按 Thread→Turn→Run 顺序固定跨执行段的 trace 与观察身份。"""
    run_id = meta.get("run_id")
    worker_id = meta.get("worker_id")
    if not run_id or not worker_id or not run_context.trace_id:
        return

    try:
        root_thread_id = str(meta.get("runtime_scope_id") or meta.get("thread_id") or "")
        conversation = await ConversationRepository(db).lock_conversation_by_thread_id(root_thread_id)
        if conversation is None:
            raise ValueError("Langfuse 根 Thread 不存在")
        turn = await AgentTurnRepository(db).get_for_scope(
            turn_id=str(meta["turn_id"]),
            thread_id=root_thread_id,
            uid=str(meta["uid"]),
            app_id=conversation.app_id,
            for_update=True,
        )
        if turn is None:
            raise ValueError("Langfuse Turn 不存在")
        run_repo = AgentRunRepository(db)
        run = await run_repo.get_run(str(run_id))
        if run is None or run.turn_id != turn.id or run.runtime_scope_id != root_thread_id:
            raise ValueError("Langfuse Run 与 Turn 归属不一致")
        root_id = turn.langfuse_root_observation_id
        if root_id is None:
            root_id = start_turn_observation(run_context)
            if root_id:
                await AgentTurnRepository(db).set_langfuse_root_observation_id(turn, root_id)
        else:
            earlier_runs = await AgentTurnRepository(db).list_runs(turn.id)
            trace_id = next((item.langfuse_trace_id for item in earlier_runs if item.langfuse_trace_id), None)
            if trace_id is None:
                raise ValueError("Langfuse Turn 根观察缺少持久 trace")
            run_context.trace_id = trace_id
        if root_id:
            await run_repo.set_langfuse_trace_id(str(run_id), str(run_context.trace_id), worker_id=str(worker_id))
            observation_id = attach_run_observation(
                run_context,
                root_observation_id=root_id,
                existing_observation_id=run.langfuse_observation_id,
            )
            if observation_id and run.langfuse_observation_id is None:
                await run_repo.set_langfuse_observation_id(str(run_id), observation_id, worker_id=str(worker_id))
        await db.commit()
    except BaseException:
        await db.rollback()
        run_context.terminal_status = "failed"
        finish_run_observation(run_context)
        raise


def extract_agent_state(values: dict) -> AgentStatePayload:
    """从 LangGraph state 中提取 agent 状态"""
    if not isinstance(values, dict):
        return {"todos": [], "files": {}, "artifacts": [], "subagent_runs": [], "token_usage": None}

    # 直接获取，信任 state 的数据结构
    todos = values.get("todos")
    artifacts = values.get("artifacts")
    subagent_runs = values.get("subagent_runs")
    token_usage = values.get("token_usage")
    result: AgentStatePayload = {
        "todos": list(todos)[:20] if todos else [],
        "files": values.get("files") or {},
        "artifacts": list(artifacts) if artifacts else [],
        "subagent_runs": list(subagent_runs) if subagent_runs else [],
        "token_usage": dict(token_usage) if isinstance(token_usage, dict) else None,
    }

    return result


def _agent_state_signature(agent_state: AgentStatePayload | dict | None) -> str:
    if not agent_state:
        return ""
    try:
        return json.dumps(agent_state, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(agent_state)


def _current_run_token_usage(agent_state: AgentStatePayload | dict | None, run_id: str | None) -> dict:
    """提取只属于当前 Run 的用量；缺失时保留明确的不可用事实。"""

    token_usage = agent_state.get("token_usage") if isinstance(agent_state, dict) else None
    if isinstance(token_usage, dict) and run_id and token_usage.get("current_run_id") == run_id:
        run_usage = token_usage.get("run")
        if isinstance(run_usage, dict):
            return dict(run_usage)
    return {"available": False}


def _metadata_namespace(metadata: dict | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    namespace = metadata.get("namespace")
    if isinstance(namespace, list):
        return [str(item) for item in namespace]
    return []


def _validate_subagent_attachment_root(*, root_conversation, conversation, uid: str) -> None:
    """确保 SubAgent 只读取同一 Project 根 Conversation 的附件。"""
    if (
        root_conversation is None
        or root_conversation.uid != uid
        or root_conversation.project_id != conversation.project_id
    ):
        raise ValueError("子智能体根 Conversation 的 Project Workdir 不可用")


def _stream_message_key(metadata: dict | None, namespace: list[str], thread_id: str | None) -> tuple[str, str]:
    if not isinstance(metadata, dict):
        return thread_id or "", "/".join(namespace)
    return thread_id or "", str(metadata.get("run_id") or metadata.get("langgraph_node") or "/".join(namespace))


def _stream_message_id(
    message_ids: dict[tuple[str, str], str],
    key: tuple[str, str],
    preferred: str | None = None,
) -> str:
    if preferred:
        message_ids[key] = preferred
        return preferred
    return message_ids.setdefault(key, str(uuid.uuid4()))


def _message_chunk_yuxi_events(
    msg_dict: dict[str, Any],
    *,
    message_id: str,
    thread_id: str | None,
    namespace: list[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    route = {"thread_id": thread_id, "namespace": namespace}
    body = parse_assistant_message_body(msg_dict.get("content", ""))

    message_event: dict[str, Any] = {"type": "message_delta", "message_id": message_id, **route}
    message_event.update({key: value for key, value in body.items() if value})
    if len(message_event) > 4:
        events.append(message_event)

    tool_call_chunks = msg_dict.get("tool_call_chunks")
    if isinstance(tool_call_chunks, list):
        for tool_call_chunk in tool_call_chunks:
            if not isinstance(tool_call_chunk, dict):
                continue
            args_delta = tool_call_chunk.get("args")
            if args_delta is None:
                args_delta = ""
            elif not isinstance(args_delta, str):
                args_delta = json.dumps(args_delta, ensure_ascii=False)
            if not tool_call_chunk.get("id") and not tool_call_chunk.get("name") and not args_delta:
                continue
            events.append(
                {
                    "type": "tool_call_delta",
                    "message_id": message_id,
                    "tool_call_id": tool_call_chunk.get("id"),
                    "name": tool_call_chunk.get("name") or None,
                    "args_delta": args_delta,
                    "index": tool_call_chunk.get("index") if tool_call_chunk.get("index") is not None else 0,
                    **route,
                }
            )
    return events


def _protocol_event_yuxi_event(
    event: dict[str, Any],
    *,
    message_id: str | None,
    thread_id: str | None,
    namespace: list[str],
) -> dict[str, Any] | None:
    event_name = event.get("event")
    if event_name in {"message-start", "content-block-start", "message-finish"} or not message_id:
        return None

    route = {"thread_id": thread_id, "namespace": namespace}
    if event_name == "content-block-delta":
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        text = delta.get("text")
        if delta.get("type") == "text-delta" and isinstance(text, str) and text:
            return {"type": "message_delta", "message_id": message_id, "content": text, **route}
        reasoning = delta.get("reasoning")
        if delta.get("type") == "reasoning-delta" and isinstance(reasoning, str) and reasoning:
            return {"type": "message_delta", "message_id": message_id, "reasoning_content": reasoning, **route}
        return None

    if event_name == "content-block-finish":
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        if content.get("type") != "tool_call" or not content.get("id") and not content.get("name"):
            return None
        return {
            "type": "tool_call",
            "message_id": message_id,
            "tool_call_id": content.get("id"),
            "name": content.get("name"),
            "args": content.get("args") if content.get("args") is not None else {},
            "index": event.get("index") if event.get("index") is not None else 0,
            **route,
        }

    return None


def _context_compression_payload(payload: Any) -> dict | None:
    if isinstance(payload, dict) and payload.get("type") == "yuxi.context_compression":
        return payload
    return None


def _stream_event_response(event: dict[str, Any]) -> str:
    if event.get("type") != "message_delta":
        return ""
    return str(event.get("content") or "")


def _message_payload_yuxi_events(
    msg: Any,
    *,
    metadata: dict[str, Any],
    namespace: list[str],
    thread_id: str | None,
    protocol_message_ids: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    message_key = _stream_message_key(metadata, namespace, thread_id)
    if isinstance(msg, dict) and isinstance(msg.get("event"), str):
        preferred_message_id = str(msg["id"]) if msg.get("event") == "message-start" and msg.get("id") else None
        message_id = _stream_message_id(protocol_message_ids, message_key, preferred_message_id)
        stream_event = _protocol_event_yuxi_event(
            msg,
            message_id=message_id,
            thread_id=thread_id,
            namespace=namespace,
        )
        return [stream_event] if stream_event else []

    if isinstance(msg, AIMessageChunk) or hasattr(msg, "model_dump"):
        msg_dict = msg.model_dump()
    elif isinstance(msg, dict):
        msg_dict = dict(msg)
    else:
        msg_dict = {"content": str(msg)}

    message_id = str(msg_dict.get("id") or _stream_message_id(protocol_message_ids, message_key))
    return _message_chunk_yuxi_events(
        msg_dict,
        message_id=message_id,
        thread_id=thread_id,
        namespace=namespace,
    )


async def _persist_model_request_timing(
    recorder: FirstModelRequestRecorder | None,
    meta: dict,
) -> None:
    """在 Run 终态事件发布前持久化首次模型请求时间。"""
    if recorder is not None:
        await recorder.persist(
            run_id=str(meta.get("run_id") or ""),
            worker_id=str(meta.get("worker_id") or ""),
        )


def _extract_interrupt_info(state) -> Any | None:
    """从 LangGraph state 中提取中断信息"""
    if hasattr(state, "tasks") and state.tasks:
        for task in state.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                return task.interrupts[0]

    interrupt_data = state.values.get("__interrupt__")
    if isinstance(interrupt_data, list) and interrupt_data:
        return interrupt_data[0]

    return None


def _coerce_interrupt_payload(info: Any) -> dict:
    """将 LangGraph interrupt 对象转换为 dict 结构。"""
    if isinstance(info, dict):
        return info

    payload = getattr(info, "value", None)
    if isinstance(payload, dict):
        return payload

    questions = getattr(info, "questions", None)
    source = getattr(info, "source", None)
    result: dict[str, Any] = {}
    if isinstance(questions, list):
        result["questions"] = questions
    if isinstance(source, str) and source.strip():
        result["source"] = source
    return result


def _build_ask_user_question_payload(payload: dict, thread_id: str) -> dict[str, Any]:
    """将已标准化的 interrupt payload 转换为 ask_user_question_required 载荷。"""

    questions = _normalize_interrupt_questions(payload.get("questions"))

    if not questions:
        questions = [
            {
                "question_id": str(uuid.uuid4()),
                "question": "请选择一个选项",
                "options": [],
                "multi_select": False,
                "allow_other": True,
            }
        ]

    source = str(payload.get("source") or payload.get("tool_name") or "interrupt")

    return {
        "questions": questions,
        "source": source,
        "thread_id": thread_id,
    }


def _build_tool_approval_payload(payload: dict, thread_id: str) -> dict[str, Any] | None:
    """将已标准化的 interrupt payload 转换为 tool_approval_required 载荷。"""
    action_requests = payload.get("action_requests")
    review_configs = payload.get("review_configs")
    if not isinstance(action_requests, list) or not isinstance(review_configs, list):
        return None
    if not action_requests or len(action_requests) != len(review_configs):
        return None
    return {
        "approval": {
            "action_requests": json_safe(action_requests),
            "review_configs": json_safe(review_configs),
        },
        "thread_id": thread_id,
    }


def build_pending_interrupt_payload(info: Any, thread_id: str) -> dict[str, Any]:
    """将 checkpoint 中断信息转换为前端可恢复的统一载荷。"""
    coerced = _coerce_interrupt_payload(info)
    approval_payload = _build_tool_approval_payload(coerced, thread_id)
    if approval_payload:
        return {"status": "human_approval_required", **approval_payload}

    question_payload = _build_ask_user_question_payload(coerced, thread_id)
    return {"status": "ask_user_question_required", **question_payload}


def _interrupt_terminal_details(chunk: dict[str, Any]) -> tuple[str, str]:
    """从结构化中断增量提取持久终态的类型与摘要。"""
    status = str(chunk.get("status") or "interrupted")
    if status == "human_approval_required":
        return status, "需要用户审批工具操作"
    questions = chunk.get("questions")
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        question = str(questions[0].get("question") or "").strip()
        if question:
            return status, question
    return status, str(chunk.get("message") or "需要用户回答问题")


def _waitpoint_from_interrupt_chunk(chunk: dict[str, Any], run_id: str) -> dict:
    """为具体 interrupted Run 固定等待点和审批调用身份。"""
    if chunk.get("status") == "human_approval_required":
        approval = dict(chunk.get("approval") or {})
        actions = approval.get("action_requests") or []
        configs = approval.get("review_configs") or []
        calls = [
            {
                **action,
                "call_id": hash_id("call_", f"{run_id}:{index}", length=64),
                "allowed_decisions": configs[index].get("allowed_decisions", []),
            }
            for index, action in enumerate(actions)
        ]
        kind = "approval"
        questions = []
    else:
        kind = "answer"
        questions = chunk.get("questions") or []
        calls = []
    return {
        "id": hash_id("wait_", run_id, length=64),
        "run_id": run_id,
        "kind": kind,
        "calls": calls,
        "questions": questions,
    }


async def _resolve_agent_runtime(
    *,
    db,
    user: User,
    requested_agent_slug: str | None,
    thread_id: str,
    prepared_execution: PreparedRunExecution,
    agent_kind: Literal["main", "subagent"] = "main",
) -> tuple[Agent, Any, BaseContext, Conversation]:
    """校验执行时的线程与 Agent 权限，使用 worker 已固化的配置。"""
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
    expected_status = "subagent" if agent_kind == "subagent" else "active"
    if not conversation or conversation.uid != str(user.uid) or conversation.status != expected_status:
        raise ValueError("对话线程不存在")
    # Conversation.agent_id 是历史字段名，实际保存的是 Agent.slug。
    if requested_agent_slug and requested_agent_slug != conversation.agent_id:
        raise ValueError("已有线程已绑定智能体，不能切换")
    await resolve_conversation_workdir_path(conversation=conversation, uid=str(user.uid), db=db)

    agent_item = await AgentRepository(db).get_visible_by_slug(slug=conversation.agent_id, user=user, kind=agent_kind)
    if not agent_item:
        raise ValueError("智能体不存在或无权限访问")

    backend = get_agent_backend(agent_item.backend_id)

    if agent_item.backend_id != prepared_execution.backend_id:
        raise ValueError("智能体后端在执行准备后发生变化")
    return agent_item, backend, prepared_execution.context, conversation


async def check_and_handle_interrupts(
    state,
    make_chunk,
    meta: dict,
    thread_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """从本轮最终 checkpoint 生成结构化中断增量。"""
    if not state or not state.values:
        return
    interrupt_info = _extract_interrupt_info(state)
    if interrupt_info:
        pending_interrupt = build_pending_interrupt_payload(interrupt_info, thread_id)
        status = pending_interrupt.pop("status")
        meta["interrupt"] = pending_interrupt
        yield make_chunk(status=status, meta=meta, **pending_interrupt)


@dataclass(frozen=True)
class RunExecutionResult:
    """向 worker 交付已持久化的最终 checkpoint 与业务终态增量。"""

    checkpoint: Any
    chunk: dict[str, Any]


async def stream_agent_chat(
    *,
    agent_slug: str,
    thread_id: str,
    meta: dict,
    input_messages: list[AgentRunInputMessage],
    current_user,
    db,
    prepared_execution: PreparedRunExecution,
    on_prepared: Callable[[], Awaitable[None]] | None = None,
    model_request_recorder: FirstModelRequestRecorder | None = None,
) -> AsyncIterator[dict[str, Any] | RunExecutionResult]:
    """以持久 Input 执行普通或子智能体 Run。"""
    stream = _stream_agent_execution(
        thread_id=thread_id,
        meta=meta,
        current_user=current_user,
        db=db,
        prepared_execution=prepared_execution,
        on_prepared=on_prepared,
        model_request_recorder=model_request_recorder,
        agent_slug=agent_slug,
        input_messages=input_messages,
    )
    async with aclosing(stream):
        async for event in stream:
            yield event


async def stream_agent_resume(
    *,
    thread_id: str,
    resume_input: Any,
    meta: dict,
    current_user,
    db,
    prepared_execution: PreparedRunExecution,
    on_prepared: Callable[[], Awaitable[None]] | None = None,
    model_request_recorder: FirstModelRequestRecorder | None = None,
) -> AsyncIterator[dict[str, Any] | RunExecutionResult]:
    """以已验证的等待点控制输入恢复同一 Turn。"""
    stream = _stream_agent_execution(
        thread_id=thread_id,
        meta=meta,
        current_user=current_user,
        db=db,
        prepared_execution=prepared_execution,
        on_prepared=on_prepared,
        model_request_recorder=model_request_recorder,
        resume_input=resume_input,
        is_resume=True,
    )
    async with aclosing(stream):
        async for event in stream:
            yield event


async def _stream_agent_execution(
    *,
    thread_id: str,
    meta: dict,
    current_user,
    db,
    prepared_execution: PreparedRunExecution,
    on_prepared: Callable[[], Awaitable[None]] | None,
    model_request_recorder: FirstModelRequestRecorder | None,
    agent_slug: str | None = None,
    input_messages: list[AgentRunInputMessage] | None = None,
    resume_input: Any = None,
    is_resume: bool = False,
) -> AsyncIterator[dict[str, Any] | RunExecutionResult]:
    """用同一图事件循环执行 chat 和 resume，最终结果携带 checkpoint。"""
    meta = dict(meta or {})
    if not thread_id or not meta.get("run_id") or not meta.get("turn_id"):
        raise ValueError("执行需要已持久化的 Thread、Turn 和 Run")
    if not is_resume and not input_messages:
        raise ValueError("Run 缺少已消费的输入消息")

    start_time = asyncio.get_event_loop().time()
    langfuse_run: LangfuseRunContext | None = None
    accumulated_content: list[str] = []
    trace_info: dict[str, Any] = {}

    def make_chunk(content=None, **kwargs) -> dict[str, Any]:
        """构造尚未经过 Redis/SSE 序列化的执行增量。"""
        chunk_thread_id = kwargs.pop("thread_id", None) or meta.get("thread_id") or thread_id
        if "meta" in kwargs:
            kwargs["meta"] = dict(kwargs["meta"])
        return {
            "turn_id": meta["turn_id"],
            "run_id": meta["run_id"],
            "response": content,
            "thread_id": chunk_thread_id,
            **kwargs,
        }

    if is_resume:
        yield make_chunk(status="init", meta=meta)

    try:
        agent_item, agent, context, conversation = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_slug=None if is_resume else agent_slug,
            thread_id=thread_id,
            agent_kind="subagent" if meta.get("run_type") == "subagent" else "main",
            prepared_execution=prepared_execution,
        )
    except ValueError as exc:
        yield make_chunk(status="error", error_type="invalid_agent", error_message=str(exc), meta=meta)
        return

    conv_repo = ConversationRepository(db)
    if is_resume:
        graph_input = Command(resume=resume_input)
        message_type = "resume"
        operation = "agent_chat_resume"
    else:
        assert input_messages is not None
        query = "\n".join(message.content for message in input_messages)
        image_content = next((message.image_content for message in input_messages if message.image_content), None)
        message_type = input_messages[0].message_type if len(input_messages) == 1 else "message_batch"
        graph_input = [message.require_langchain_message() for message in input_messages]
        operation = "agent_chat_stream"
        meta.update({"query": query, "has_image": bool(image_content)})

    meta.update(
        {
            "agent_slug": agent_item.slug,
            "backend_id": agent_item.backend_id,
            "thread_id": thread_id,
            "uid": current_user.uid,
        }
    )

    try:
        langfuse_run = _build_langfuse_run_context(
            current_user=current_user,
            thread_id=thread_id,
            agent_id=agent_item.slug,
            backend_id=agent_item.backend_id,
            turn_id=meta["turn_id"],
            run_id=meta["run_id"],
            operation=operation,
            message_type=message_type,
            meta=meta,
        )
        await _persist_agent_run_langfuse_trace(db=db, meta=meta, run_context=langfuse_run)

        if not is_resume:
            runtime_scope_id = context.runtime_scope_id
            attachment_conversation = conversation
            if meta.get("run_type") == "subagent":
                attachment_conversation = await conv_repo.get_conversation_by_thread_id(runtime_scope_id)
                _validate_subagent_attachment_root(
                    root_conversation=attachment_conversation,
                    conversation=conversation,
                    uid=str(current_user.uid),
                )
            thread_attachment_records = await conv_repo.get_attachments(attachment_conversation.id)
            input_attachment_records = (
                await conv_repo.get_attachments_by_input_id(conversation.id, meta["input_id"])
                if meta.get("input_id")
                else []
            )
            input_attachments = [
                serialize_attachment(attachment, thread_id=thread_id) for attachment in input_attachment_records
            ]
            thread_attachments = [
                serialize_attachment(attachment, thread_id=thread_id) for attachment in thread_attachment_records
            ]
            graph_input[-1] = _with_attachment_context(graph_input[-1], thread_attachments)
            init_msg = {
                "role": "user",
                "content": query,
                "type": "human",
                "message_type": message_type,
                "extra_metadata": {"input_id": meta.get("input_id"), "attachments": input_attachments},
            }
            if image_content:
                init_msg["image_content"] = image_content
            yield make_chunk(status="init", meta=meta, msg=init_msg)

        # 执行图期间不占用业务数据库事务；checkpoint 使用独立 PostgreSQL checkpointer。
        await db.commit()
        callbacks = list(langfuse_run.callbacks)
        if model_request_recorder is not None:
            callbacks.append(model_request_recorder)
        graph_kwargs = {
            "context": context,
            "callbacks": callbacks,
            "metadata": langfuse_run.metadata,
            "tags": langfuse_run.tags,
            "run_name": agent_item.name or agent_item.slug,
            "on_prepared": on_prepared,
        }
        stream_source = (
            agent.stream_resume_with_state(graph_input, **graph_kwargs)
            if is_resume
            else agent.stream_messages_with_state(graph_input, **graph_kwargs)
        )

        final_state = None
        last_agent_state_signature = ""
        protocol_message_ids: dict[tuple[str, str], str] = {}
        model_audit = _build_model_message_audit_collector(meta, thread_id)
        tool_audit = _build_tool_message_audit_collector(model_audit)

        async with aclosing(stream_source):
            async for mode, payload in stream_source:
                if mode == "checkpoint":
                    final_state = payload
                    continue
                if mode == "values":
                    agent_state = extract_agent_state(payload if isinstance(payload, dict) else {})
                    signature = _agent_state_signature(agent_state)
                    if signature and signature != last_agent_state_signature:
                        last_agent_state_signature = signature
                        yield make_chunk(status="agent_state", agent_state=agent_state, meta=meta)
                    continue
                if mode == "custom":
                    compression = _context_compression_payload(payload)
                    if compression is not None:
                        yield make_chunk(status="context_compression", compression=compression, meta=meta)
                    continue
                if mode == "stream_event":
                    event_payload = payload if isinstance(payload, dict) else {}
                    event_thread_id = event_payload.get("thread_id")
                    if (
                        tool_audit is not None
                        and event_payload.get("method") == "tools"
                        and _is_root_tool_audit_event(event_payload, thread_id)
                    ):
                        await tool_audit.consume(event_payload)
                    yield make_chunk(
                        status="stream_event",
                        event=event_payload,
                        namespace=event_payload.get("namespace") or [],
                        meta=meta,
                        thread_id=event_thread_id,
                    )
                    continue
                if mode != "messages":
                    continue

                msg, metadata = payload
                metadata = dict(metadata or {})
                namespace = _metadata_namespace(metadata)
                chunk_thread_id = _metadata_thread_id(metadata, thread_id if not namespace else None)
                if namespace and not chunk_thread_id:
                    continue
                is_subagent_chunk = bool(chunk_thread_id and chunk_thread_id != thread_id)
                if model_audit is not None and not is_subagent_chunk:
                    await model_audit.consume(msg, metadata)
                stream_events = _message_payload_yuxi_events(
                    msg,
                    metadata=metadata,
                    namespace=namespace,
                    thread_id=chunk_thread_id,
                    protocol_message_ids=protocol_message_ids,
                )
                for stream_event in stream_events:
                    content = _stream_event_response(stream_event)
                    if not is_subagent_chunk:
                        if is_resume or content:
                            trace_info = get_trace_info(langfuse_run)
                        if content and not is_resume:
                            accumulated_content.append(content)
                    yield make_chunk(
                        content=content,
                        stream_event=stream_event,
                        metadata=metadata,
                        status="loading",
                        thread_id=chunk_thread_id,
                    )

        if final_state is None:
            raise ValueError("Agent 执行流缺少最终 checkpoint")
        trace_info = get_trace_info(langfuse_run)
        interrupt_chunk = None
        async for chunk in check_and_handle_interrupts(final_state, make_chunk, meta, thread_id):
            interrupt_chunk = chunk
            break

        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
        agent_state = extract_agent_state(final_state.values)
        final_signature = _agent_state_signature(agent_state)
        if final_signature and final_signature != last_agent_state_signature:
            yield make_chunk(status="agent_state", agent_state=agent_state, meta=meta)

        await _persist_model_request_timing(model_request_recorder, meta)
        waitpoint = None
        interrupt_error_type = None
        interrupt_error_message = None
        if interrupt_chunk is not None:
            interrupt_error_type, interrupt_error_message = _interrupt_terminal_details(interrupt_chunk)
            waitpoint = _waitpoint_from_interrupt_chunk(interrupt_chunk, meta["run_id"])
            interrupt_chunk.update({"waitpoint_id": waitpoint["id"], "waitpoint": waitpoint})

        try:
            terminal_status = await save_messages_from_langgraph_state(
                state=final_state,
                thread_id=thread_id,
                conv_repo=conv_repo,
                trace_info=trace_info,
                run_id=meta["run_id"],
                turn_id=meta["turn_id"],
                worker_id=meta.get("worker_id"),
                complete_run=interrupt_chunk is None,
                interrupt_run=interrupt_chunk is not None,
                interrupt_error_type=interrupt_error_type,
                interrupt_error_message=interrupt_error_message,
                token_usage=_current_run_token_usage(agent_state, meta["run_id"]),
                waitpoint=waitpoint,
            )
        except Exception:
            logger.exception("最终输出持久化或绑定失败")
            yield make_chunk(
                status="error",
                error_type="output_persistence_error",
                error_message="最终输出持久化或绑定失败",
                meta=meta,
            )
            return

        langfuse_run.terminal_status = terminal_status
        terminal_chunk = interrupt_chunk or make_chunk(
            status="yielded" if terminal_status == "yielded" else "finished",
            meta=meta,
            terminal_committed=bool(terminal_status),
        )
        yield RunExecutionResult(checkpoint=final_state, chunk=terminal_chunk)

    except (asyncio.CancelledError, ConnectionError) as exc:
        logger.warning(f"Agent 执行流中断: {exc}")
        await _persist_model_request_timing(model_request_recorder, meta)
        yield make_chunk(status="interrupted", message="对话恢复已中断" if is_resume else "对话已中断", meta=meta)
    except Exception as exc:
        logger.exception(f"Agent 执行失败: {exc}")
        error_message = f"Error during resume: {exc}" if is_resume else f"Error streaming messages: {exc}"
        async with pg_manager.get_async_session_context() as new_db:
            await save_partial_message(
                ConversationRepository(new_db),
                thread_id,
                full_msg=AIMessage(content="".join(accumulated_content)) if accumulated_content else None,
                error_message=error_message,
                error_type="resume_error" if is_resume else "unexpected_error",
                trace_info=trace_info,
                run_id=meta["run_id"],
                turn_id=meta["turn_id"],
                worker_id=meta.get("worker_id"),
            )
        await _persist_model_request_timing(model_request_recorder, meta)
        yield make_chunk(
            status="error",
            error_type="resume_error" if is_resume else "unexpected_error",
            error_message=error_message,
            meta=meta,
        )
    finally:
        finish_run_observation(langfuse_run)
        await _flush_langfuse_best_effort()
