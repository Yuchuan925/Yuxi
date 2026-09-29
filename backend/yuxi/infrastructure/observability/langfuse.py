"""Langfuse SDK、远端观察与 URL 传输。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, UTC
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse
from yuxi.infrastructure.observability.logging import logger


_FALSE_VALUES = {"0", "false", "no", "off"}


_DEFAULT_LANGFUSE_BASE_URL = "https://cloud.langfuse.com"


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


def _utc_nanoseconds(value: datetime) -> str:
    """把持久 UTC 时间转换为 OTLP 纳秒时间戳。"""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return str(int(aware.timestamp() * 1_000_000_000))


def export_turn_root(
    *,
    trace_id: str,
    root_id: str,
    turn_id: str,
    thread_id: str,
    uid: str,
    status: str,
    created_at: datetime,
    finished_at: datetime,
) -> None:
    """以已持久化的因果 ID 向 Langfuse 导出完整 Turn 根跨度。"""
    from langfuse.api.opentelemetry.types import (
        OtelAttribute,
        OtelAttributeValue,
        OtelResourceSpan,
        OtelScope,
        OtelScopeSpan,
        OtelSpan,
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
            OtelResourceSpan(
                scope_spans=[
                    OtelScopeSpan(
                        scope=OtelScope(name="yuxi-agent-lifecycle"),
                        spans=[
                            OtelSpan(
                                trace_id=trace_id,
                                span_id=root_id,
                                name="agent.turn",
                                kind=1,
                                start_time_unix_nano=_utc_nanoseconds(created_at),
                                end_time_unix_nano=_utc_nanoseconds(finished_at),
                                attributes=attributes,
                                status={},
                            )
                        ],
                    )
                ]
            ),
        ],
        request_options={
            "timeout_in_seconds": 2,
            "max_retries": 0,
            "additional_headers": {"x-langfuse-ingestion-version": "4"},
        },
    )


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


try:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
except Exception:  # pragma: no cover - optional dependency during local test collection
    Langfuse = None  # type: ignore[assignment]
    CallbackHandler = None  # type: ignore[assignment]
