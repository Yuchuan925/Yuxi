from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, UTC
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from yuxi.utils.logging_config import logger

try:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
except Exception:  # pragma: no cover - optional dependency during local test collection
    Langfuse = None  # type: ignore[assignment]
    CallbackHandler = None  # type: ignore[assignment]


_FALSE_VALUES = {"0", "false", "no", "off"}
_DEFAULT_LANGFUSE_BASE_URL = "https://cloud.langfuse.com"


@dataclass(slots=True)
class LangfuseRunContext:
    callbacks: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    trace_id: str | None = None
    root_observation_id: str | None = None
    run_observation_id: str | None = None
    run_observation: Any | None = None
    terminal_status: str | None = None


def is_langfuse_enabled() -> bool:
    enabled_raw = (os.getenv("LANGFUSE_ENABLED") or "true").strip().lower()
    if enabled_raw in _FALSE_VALUES:
        return False

    if Langfuse is None or CallbackHandler is None:
        return False

    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


@lru_cache(maxsize=1)
def get_langfuse_client() -> Langfuse | None:
    if not is_langfuse_enabled():
        return None

    kwargs: dict[str, Any] = {
        "public_key": os.getenv("LANGFUSE_PUBLIC_KEY"),
        "secret_key": os.getenv("LANGFUSE_SECRET_KEY"),
    }
    host = os.getenv("LANGFUSE_BASE_URL")
    if host:
        kwargs["host"] = host

    try:
        return Langfuse(**kwargs)
    except Exception as exc:
        logger.warning(f"初始化 Langfuse 客户端失败，将跳过 tracing: {exc}")
        return None


def build_trace_metadata(
    *,
    user_id: str,
    thread_id: str,
    agent_id: str,
    turn_id: str,
    run_id: str,
    operation: str,
    backend_id: str | None = None,
    message_type: str | None = None,
    username: str | None = None,
    login_user_id: str | None = None,
    department_id: int | str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "langfuse_user_id": user_id,
        "langfuse_session_id": thread_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "thread_id": thread_id,
        "agent_id": agent_id,
        "operation": operation,
        "source": "yuxi",
        "feature": "chat",
    }

    if backend_id:
        metadata["backend_id"] = backend_id
    if message_type:
        metadata["message_type"] = message_type
    if username:
        metadata["username"] = username
    if login_user_id:
        metadata["login_user_id"] = login_user_id
    if department_id is not None:
        metadata["department_id"] = str(department_id)
    if extra_metadata:
        metadata.update(extra_metadata)

    return metadata


def build_trace_tags(
    *,
    agent_id: str,
    operation: str,
    message_type: str | None = None,
    extra_tags: list[str] | None = None,
) -> list[str]:
    tags = ["yuxi", "chat", operation, f"agent:{agent_id}"]
    if message_type:
        tags.append(f"message_type:{message_type}")
    for tag in extra_tags or []:
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_run_context(
    *,
    user_id: str,
    thread_id: str,
    agent_id: str,
    turn_id: str,
    run_id: str,
    operation: str,
    backend_id: str | None = None,
    message_type: str | None = None,
    username: str | None = None,
    login_user_id: str | None = None,
    department_id: int | str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    extra_tags: list[str] | None = None,
    parent_observation_id: str | None = None,
) -> LangfuseRunContext:
    """为同一 Turn 的执行段复用 trace，并设置可持久恢复的父观察。"""
    metadata = build_trace_metadata(
        user_id=user_id,
        thread_id=thread_id,
        agent_id=agent_id,
        turn_id=turn_id,
        run_id=run_id,
        operation=operation,
        backend_id=backend_id,
        message_type=message_type,
        username=username,
        login_user_id=login_user_id,
        department_id=department_id,
        extra_metadata=extra_metadata,
    )
    tags = build_trace_tags(
        agent_id=agent_id,
        operation=operation,
        message_type=message_type,
        extra_tags=extra_tags,
    )

    client = get_langfuse_client()
    if client is None or CallbackHandler is None:
        return LangfuseRunContext(metadata=metadata, tags=tags)

    try:
        trace_id = client.create_trace_id(seed=turn_id)
        trace_context = {"trace_id": trace_id}
        if parent_observation_id:
            trace_context["parent_span_id"] = parent_observation_id
        handler = CallbackHandler(trace_context=trace_context)
    except Exception as exc:
        logger.warning("初始化 Langfuse Run 回调失败: %s", exc)
        return LangfuseRunContext(metadata=metadata, tags=tags)
    return LangfuseRunContext(callbacks=[handler], metadata=metadata, tags=tags, trace_id=trace_id)


def start_turn_observation(context: LangfuseRunContext) -> str | None:
    """为跨进程 Turn 预留根 ID；终态后再导出覆盖整轮的观察。"""
    turn_id = context.metadata.get("turn_id")
    if get_langfuse_client() is None or not context.trace_id or not isinstance(turn_id, str) or not turn_id:
        return None
    return hashlib.blake2b(turn_id.encode(), digest_size=8).hexdigest()


def _utc_nanoseconds(value: datetime) -> str:
    """把持久 UTC 时间转换为 OTLP 纳秒时间戳。"""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return str(int(aware.timestamp() * 1_000_000_000))


def _export_turn_root(
    *, trace_id: str, root_id: str, turn_id: str, thread_id: str, uid: str,
    status: str, created_at: datetime, finished_at: datetime,
) -> None:
    """以已持久化的因果 ID 向 Langfuse 导出完整 Turn 根跨度。"""
    from langfuse.api.opentelemetry.types import (
        OtelAttribute, OtelAttributeValue, OtelResourceSpan, OtelScope,
        OtelScopeSpan, OtelSpan,
    )

    client = get_langfuse_client()
    if client is None:
        return
    attributes = [
        OtelAttribute(key=key, value=OtelAttributeValue(string_value=value))
        for key, value in (
            ("langfuse.observation.type", "agent"),
            ("langfuse.observation.level", "ERROR" if status == "failed" else "DEFAULT"),
            ("langfuse.observation.metadata.turn_id", turn_id),
            ("langfuse.observation.metadata.thread_id", thread_id),
            ("langfuse.observation.metadata.status", status),
            ("langfuse.user.id", uid),
            ("langfuse.session.id", thread_id),
        )
    ]
    client.api.opentelemetry.export_traces(
        resource_spans=[
            OtelResourceSpan(scope_spans=[OtelScopeSpan(
                scope=OtelScope(name="yuxi-agent-lifecycle"),
                spans=[OtelSpan(
                    trace_id=trace_id,
                    span_id=root_id,
                    name="agent.turn",
                    kind=1,
                    start_time_unix_nano=_utc_nanoseconds(created_at),
                    end_time_unix_nano=_utc_nanoseconds(finished_at),
                    attributes=attributes,
                    status={},
                )],
            )]),
        ],
        request_options={
            "timeout_in_seconds": 2,
            "max_retries": 0,
            "additional_headers": {"x-langfuse-ingestion-version": "4"},
        },
    )


async def finish_turn_observation_if_terminal(turn_id: str) -> None:
    """提交后读取 Turn 终态，再尽力导出跨 Run 和等待期的根观察。"""
    from yuxi.repositories.agents.turn import AgentTurnRepository
    from yuxi.storage.postgres.manager import pg_manager

    if get_langfuse_client() is None:
        return
    try:
        async with pg_manager.get_async_session_context() as db:
            repo = AgentTurnRepository(db)
            turn = await repo.get(turn_id)
            if (
                turn is None or turn.status not in {"completed", "failed", "cancelled"}
                or not turn.langfuse_root_observation_id or turn.finished_at is None
            ):
                return
            runs = await repo.list_runs(turn.id)
            trace_id = next((run.langfuse_trace_id for run in runs if run.langfuse_trace_id), None)
            if trace_id is None:
                return
            details = {
                "trace_id": trace_id,
                "root_id": turn.langfuse_root_observation_id,
                "turn_id": turn.id,
                "thread_id": turn.conversation_thread_id,
                "uid": turn.uid,
                "status": turn.status,
                "created_at": turn.created_at,
                "finished_at": max(turn.finished_at, datetime.now(UTC).replace(tzinfo=None)),
            }
        await asyncio.wait_for(asyncio.to_thread(_export_turn_root, **details), timeout=3)
    except Exception as exc:
        logger.warning("结束 Langfuse Turn 根观察失败: %s", exc)


def attach_run_observation(
    context: LangfuseRunContext, *, root_observation_id: str, existing_observation_id: str | None = None
) -> str | None:
    """将本 Run 的模型和工具回调绑定到同一 Turn 根观察。"""
    client = get_langfuse_client()
    if client is None or CallbackHandler is None or not context.trace_id:
        return None
    context.root_observation_id = root_observation_id
    observation_id = existing_observation_id
    if observation_id is None:
        try:
            observation = client.start_observation(
                trace_context={"trace_id": context.trace_id, "parent_span_id": root_observation_id},
                name="agent.run",
                as_type="agent",
                metadata={
                    "turn_id": context.metadata.get("turn_id"),
                    "run_id": context.metadata.get("run_id"),
                    "operation": context.metadata.get("operation"),
                },
            )
            context.run_observation = observation
            observation_id = str(observation.id)
        except Exception as exc:
            logger.warning("创建 Langfuse Run 观察失败: %s", exc)
            return None
    try:
        callback = CallbackHandler(trace_context={"trace_id": context.trace_id, "parent_span_id": observation_id})
    except Exception as exc:
        logger.warning("初始化 Langfuse 观察回调失败: %s", exc)
        if context.run_observation is not None:
            context.terminal_status = "abandoned"
            finish_run_observation(context)
        return None
    context.run_observation_id = observation_id
    context.callbacks = [callback]
    return observation_id


def finish_run_observation(context: LangfuseRunContext | None) -> None:
    """记录当前 Run 的终态并结束本进程创建的观察。"""
    if context is None or context.run_observation is None:
        return
    try:
        status = context.terminal_status or "abandoned"
        context.run_observation.update(
            metadata={"run_id": context.metadata.get("run_id"), "status": status},
            level="ERROR" if status in {"failed", "abandoned"} else "DEFAULT",
        )
        context.run_observation.end()
        context.run_observation = None
    except Exception as exc:
        logger.warning("结束 Langfuse Run 观察失败: %s", exc)


def get_trace_info(run_context: LangfuseRunContext | None) -> dict[str, Any]:
    if run_context is None:
        return {}

    metadata = run_context.metadata or {}
    trace_id = run_context.trace_id
    if not trace_id and run_context.callbacks:
        trace_id = getattr(run_context.callbacks[0], "last_trace_id", None)

    if not trace_id:
        return {}

    trace_info = {
        "langfuse_trace_id": trace_id,
        "langfuse_user_id": metadata.get("langfuse_user_id"),
        "langfuse_session_id": metadata.get("langfuse_session_id"),
    }

    # Do not fetch trace_url on the request critical path. Langfuse resolves the
    # project id via a remote API call, which can add noticeable latency when the
    # base URL is slow or unreachable. If a trace URL is still needed, fetch it
    # later via get_trace_url_by_id_async() and patch message metadata asynchronously.
    return trace_info


def submit_user_feedback_score(
    *,
    trace_id: str,
    feedback_id: int,
    message_id: int,
    conversation_id: int,
    uid: str,
    rating: str,
    reason: str | None = None,
) -> bool:
    client = get_langfuse_client()
    if client is None:
        return False

    value = 1 if rating == "like" else 0
    try:
        client.create_score(
            trace_id=trace_id,
            score_id=f"yuxi-message-feedback-{feedback_id}",
            name="user-feedback",
            value=value,
            data_type="BOOLEAN",
            comment=reason,
            metadata={
                "source": "yuxi",
                "feedback_id": feedback_id,
                "message_id": message_id,
                "conversation_id": conversation_id,
                "uid": uid,
                "rating": rating,
            },
        )
        client.flush()
        return True
    except Exception as exc:
        logger.warning(f"提交 Langfuse 用户反馈评分失败，将保留本地反馈: {exc}")
        return False


def _http_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed_url = urlparse(url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            return None
        default_port = 443 if parsed_url.scheme == "https" else 80
        return parsed_url.scheme, parsed_url.hostname.casefold(), parsed_url.port or default_port
    except ValueError:
        return None


async def get_trace_url_by_id_async(trace_id: str, *, timeout: float = 5.0) -> str | None:
    """按 trace ID 惰性解析已配置 Langfuse 源站的页面 URL。"""
    trace_id = str(trace_id or "").strip()
    if not trace_id:
        return None

    client = get_langfuse_client()
    if client is None:
        return None

    try:
        trace_url = await asyncio.wait_for(
            asyncio.to_thread(client.get_trace_url, trace_id=trace_id),
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning(f"解析 Langfuse trace URL 失败: {type(exc).__name__}")
        return None

    if not isinstance(trace_url, str):
        return None

    trace_url = trace_url.strip()
    configured_origin = _http_origin(os.getenv("LANGFUSE_BASE_URL") or _DEFAULT_LANGFUSE_BASE_URL)
    if configured_origin is None or _http_origin(trace_url) != configured_origin:
        logger.warning("Langfuse 返回了非配置源站的 trace URL，已拒绝")
        return None
    return trace_url


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is None:
        return

    try:
        client.flush()
    except Exception as exc:
        logger.warning(f"刷新 Langfuse 事件失败: {exc}")
