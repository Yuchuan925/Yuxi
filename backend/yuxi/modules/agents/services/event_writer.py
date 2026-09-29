"""AgentRun worker 职责。"""

from __future__ import annotations

import time
from dataclasses import field
from yuxi.modules.agents.services.transport import (
    append_run_stream_event,
)
from yuxi.infrastructure.observability.logging import logger
from yuxi.modules.agents.runtime.thread_metadata import extract_thread_id


LOADING_FLUSH_INTERVAL_MS = 100


LOADING_FLUSH_MAX_CHARS = 512


_ALL_THREADS = object()


class _ThreadBuffer:
    items: list[dict] = field(default_factory=list)
    chars: int = 0
    last_flush: float = field(default_factory=time.monotonic)


class ChunkedEventWriter:
    def __init__(self, run_id: str, thread_id: str | None, interval_ms: int = 100, max_chars: int = 512):
        self.run_id = run_id
        self.thread_id = thread_id
        self.interval_seconds = interval_ms / 1000
        self.max_chars = max_chars
        self.thread_buffers: dict[str | None, _ThreadBuffer] = {}

    def _target_thread_id(self, thread_id: str | None = None) -> str | None:
        return thread_id or self.thread_id

    async def append(self, chunk: dict, *, thread_id: str | None = None):
        target_thread_id = self._target_thread_id(thread_id or extract_thread_id(chunk))
        buffer = self.thread_buffers.setdefault(target_thread_id, _ThreadBuffer())
        buffer.items.append(chunk)
        buffer.chars += _loading_chunk_size(chunk)

        if _flush_loading_chunk_immediately(chunk):
            await self.flush(target_thread_id)
            return
        if (time.monotonic() - buffer.last_flush) >= self.interval_seconds or buffer.chars >= self.max_chars:
            await self.flush(target_thread_id)

    async def flush(self, thread_id: str | None | object = _ALL_THREADS):
        if thread_id is _ALL_THREADS:
            for target_thread_id in list(self.thread_buffers):
                await self.flush(target_thread_id)
            return

        buffer = self.thread_buffers.get(thread_id)
        if not buffer or not buffer.items:
            return
        await append_run_event_best_effort(
            self.run_id,
            "messages",
            {"items": buffer.items},
            thread_id=thread_id,
        )
        buffer.items = []
        buffer.chars = 0
        buffer.last_flush = time.monotonic()


async def append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
    await append_run_stream_event(run_id, event_type, payload, thread_id=thread_id)


async def append_run_event_best_effort(
    run_id: str,
    event_type: str,
    payload: dict,
    *,
    thread_id: str | None = None,
) -> bool:
    """发布短期事件；失败只记录，不能阻断 PostgreSQL 状态收敛。"""

    try:
        await append_run_event(run_id, event_type, payload, thread_id=thread_id)
    except Exception:
        logger.warning(
            f"Failed to publish non-authoritative AgentRun event: run={run_id}, event={event_type}",
            exc_info=True,
        )
        return False
    return True


async def flush_writer_best_effort(writer: ChunkedEventWriter) -> None:
    """尽力发布缓冲事件，不让 Redis 可用性决定 durable transition。"""

    try:
        await writer.flush()
    except Exception:
        logger.warning(f"Failed to flush non-authoritative AgentRun events: run={writer.run_id}", exc_info=True)


def _loading_chunk_size(chunk: dict) -> int:
    response = chunk.get("response")
    total = len(response) if isinstance(response, str) else 0
    stream_event = chunk.get("stream_event")
    if not isinstance(stream_event, dict):
        return total

    for key in ("content", "reasoning_content", "additional_reasoning_content", "args_delta"):
        value = stream_event.get(key)
        if isinstance(value, str):
            total += len(value)
    return total


def contains_model_output(chunk: dict) -> bool:
    """识别非空模型文本、推理文本或工具调用数据。"""
    stream_event = chunk.get("stream_event")
    if not isinstance(stream_event, dict):
        return False

    event_type = stream_event.get("type")
    if event_type == "message_delta":
        return any(
            isinstance(stream_event.get(key), str) and bool(stream_event[key])
            for key in ("content", "reasoning_content", "additional_reasoning_content")
        )
    if event_type in {"tool_call", "tool_call_delta"}:
        return any(
            stream_event.get(key) is not None and stream_event.get(key) != "" and stream_event.get(key) != {}
            for key in ("name", "args", "args_delta")
        )
    return False


def _flush_loading_chunk_immediately(chunk: dict) -> bool:
    stream_event = chunk.get("stream_event")
    return isinstance(stream_event, dict) and stream_event.get("type") == "tool_call"


def chunk_thread_id(chunk: dict, fallback: str | None) -> str | None:
    return extract_thread_id(chunk, fallback)


def map_chunk_to_run_event(chunk: dict) -> tuple[str, dict]:
    status = chunk.get("status") or "event"
    if status == "loading":
        return "messages", {"chunk": chunk}
    if status == "agent_state":
        return "custom", {"name": "yuxi.agent_state", "chunk": chunk, "agent_state": chunk.get("agent_state") or {}}
    if status in {"ask_user_question_required", "human_approval_required", "interrupted"}:
        reason = "human_approval" if status == "human_approval_required" else status
        return "interrupt", {"reason": reason, "chunk": chunk}
    if status == "warning":
        return "custom", {"name": "yuxi.warning", "chunk": chunk}
    if status == "error":
        return "error", {"chunk": chunk, "retryable": bool(chunk.get("retryable"))}
    if status == "finished":
        return "end", {"status": "completed", "chunk": chunk}
    return "custom", {"name": f"yuxi.{status}", "chunk": chunk}


async def append_end_event(run_id: str, status: str, *, thread_id: str | None, payload: dict | None = None):
    end_payload = {"status": status}
    if payload:
        end_payload.update(payload)
    await append_run_event_best_effort(run_id, "end", end_payload, thread_id=thread_id)
