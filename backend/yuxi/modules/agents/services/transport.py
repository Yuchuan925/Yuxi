"""Agent Run 的 ARQ 投递、Redis 增量和取消提示传输。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

from yuxi.workers.health import WORKER_HEALTH_KEY
from yuxi.infrastructure.redis import close_async_redis_client, create_arq_redis_pool, get_async_redis_client
from yuxi.infrastructure.observability.logging import logger

RUN_CANCEL_KEY_TTL_SECONDS = int(os.getenv("RUN_CANCEL_KEY_TTL_SECONDS", "1800"))
RUN_EVENTS_STREAM_TTL_SECONDS = int(os.getenv("RUN_EVENTS_STREAM_TTL_SECONDS", "7200"))
RUN_EVENTS_STREAM_MAXLEN = int(os.getenv("RUN_EVENTS_STREAM_MAXLEN", "0"))
RUN_RECONCILIATION_SECONDS = 30
WORKER_RECONCILIATION_HEALTH_KEY = f"{WORKER_HEALTH_KEY}:lease-reconciliation"
WORKER_RECONCILIATION_HEALTH_TTL_SECONDS = RUN_RECONCILIATION_SECONDS * 2 + 5

_arq_pool = None


def _cancel_key(run_id: str) -> str:
    """生成当前 Run 的取消提示键。"""
    return f"run:cancel:{run_id}"


def _event_stream_key(run_id: str) -> str:
    """生成当前 Run 的短期事件流键。"""
    return f"run:events:{run_id}"


def _is_valid_stream_seq(value: str) -> bool:
    """识别 Redis Stream 游标。"""
    major, sep, minor = value.partition("-")
    if sep != "-":
        return False
    return major.isdigit() and minor.isdigit()


def normalize_after_seq(after_seq: str | None) -> str:
    """将无效或缺失游标归一到事件流起点。"""
    if after_seq is None:
        return "0-0"

    text = str(after_seq).strip()
    if not text:
        return "0-0"

    if _is_valid_stream_seq(text):
        return text
    return "0-0"


def build_run_event_envelope(
    *,
    run_id: str,
    event_type: str,
    payload: dict | None = None,
    thread_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    """构造单条 Run 事件的传输封套。"""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "thread_id": thread_id,
        "event": event_type,
        "payload": payload or {},
        "created_at": created_at or datetime.now(tz=UTC).isoformat(),
    }


def _payload_thread_id(payload: dict | None) -> str | None:
    """从事件块提取所属 Thread。"""
    chunk = payload.get("chunk") if isinstance(payload, dict) else None
    if not isinstance(chunk, dict):
        return None
    thread_id = chunk.get("thread_id")
    return thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None


async def get_redis_client():
    """取得当前进程共用的短期传输连接。"""
    return await get_async_redis_client()


async def publish_worker_health(key: str, worker_id: str, ttl_seconds: int) -> None:
    """按调用方指定的有界 TTL 续租 worker 能力事实。"""
    redis = await get_redis_client()
    await redis.set(key, worker_id, ex=ttl_seconds)


async def get_arq_pool():
    """复用当前进程的 ARQ 连接池。"""
    global _arq_pool
    if _arq_pool is not None:
        return _arq_pool

    _arq_pool = await create_arq_redis_pool()
    return _arq_pool


async def enqueue_agent_run(run_id: str) -> None:
    """只投递 owning transaction 已提交的 Run。"""
    queue = await get_arq_pool()
    await queue.enqueue_job("process_agent_run", run_id, _job_id=f"run:{run_id}")


async def publish_cancel_signal(run_id: str) -> None:
    """尽力发布带过期时间的取消提示。"""
    try:
        redis = await get_redis_client()
        key = _cancel_key(run_id)
        await redis.set(key, "1", ex=RUN_CANCEL_KEY_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"Failed to publish cancel signal for run {run_id}: {e}")


async def publish_cancel_signals(run_ids: list[str]) -> None:
    """并发发布一组 best-effort Run 取消信号。"""
    await asyncio.gather(*(publish_cancel_signal(run_id) for run_id in run_ids))


async def _read_cancel_signal(run_id: str) -> bool:
    """读取当前 Run 的取消提示。"""
    redis = await get_redis_client()
    return bool(await redis.get(_cancel_key(run_id)))


async def wait_for_cancel_signal(run_id: str, poll_interval_seconds: float = 1.0) -> bool:
    """按固定间隔读取 Redis key，直到收到取消或 watcher 被关闭。"""

    poll_interval_seconds = max(0.0, float(poll_interval_seconds))
    loop = asyncio.get_running_loop()
    key_failure_logged = False

    while True:
        attempt_started_at = loop.time()
        try:
            if await _read_cancel_signal(run_id):
                return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not key_failure_logged:
                logger.warning(f"Failed to read cancel signal for run {run_id}: {e}")
                key_failure_logged = True
        else:
            key_failure_logged = False

        remaining = poll_interval_seconds - (loop.time() - attempt_started_at)
        await asyncio.sleep(max(0.0, remaining))


async def clear_cancel_signal(run_id: str) -> None:
    """尽力清除当前 Run 的取消提示。"""
    try:
        redis = await get_redis_client()
        key = _cancel_key(run_id)
        await redis.delete(key)
    except Exception as e:
        logger.warning(f"Failed to clear cancel signal for run {run_id}: {e}")


async def append_run_stream_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None) -> str:
    """写入事件并续期当前 Run 的 Redis Stream。"""
    redis = await get_redis_client()
    key = _event_stream_key(run_id)
    now = datetime.now(tz=UTC)
    now_ms = int(now.timestamp() * 1000)
    event_thread_id = thread_id or _payload_thread_id(payload)
    envelope = build_run_event_envelope(
        run_id=run_id,
        event_type=event_type,
        payload=payload or {},
        thread_id=event_thread_id,
        created_at=now.isoformat(),
    )
    fields = {
        "event_type": event_type,
        "payload": json.dumps(envelope, ensure_ascii=False),
        "ts": str(now_ms),
    }

    kwargs = {}
    if RUN_EVENTS_STREAM_MAXLEN > 0:
        kwargs["maxlen"] = RUN_EVENTS_STREAM_MAXLEN
        kwargs["approximate"] = True

    # 同一连接顺序发出写入和续期，省去两次往返之间的事件循环等待。
    async with redis.pipeline(transaction=False) as pipeline:
        pipeline.xadd(key, fields, **kwargs)
        pipeline.expire(key, RUN_EVENTS_STREAM_TTL_SECONDS)
        event_id, _ = await pipeline.execute()
    return str(event_id)


def _decode_run_stream_row(run_id: str, event_id: str, fields: dict) -> dict | None:
    """解码单条 Redis Stream 事件；非当前协议版本的事件直接丢弃。"""
    payload_raw = fields.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except Exception:
        payload = None

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        logger.warning("Dropping malformed run event: run_id=%s, event_id=%s", run_id, event_id)
        return None

    event_type = fields.get("event_type") or "message"
    ts_value = fields.get("ts")
    return {
        "seq": str(event_id),
        "event_type": event_type,
        "payload": payload,
        "ts": int(ts_value) if ts_value else None,
    }


async def list_run_stream_events(
    run_id: str,
    *,
    after_seq: str = "0-0",
    limit: int = 200,
) -> list[dict]:
    """按游标读取当前 Run 的后续事件。"""
    redis = await get_redis_client()
    key = _event_stream_key(run_id)
    start = "-" if after_seq in {"0-0", ""} else f"({after_seq}"
    rows = await redis.xrange(key, min=start, max="+", count=limit)
    events = []

    for event_id, fields in rows:
        event = _decode_run_stream_row(run_id, event_id, fields)
        if event is not None:
            events.append(event)
    return events


async def list_recent_run_stream_events(run_id: str, *, limit: int = 100) -> list[dict]:
    """从 Redis Stream 反向读取最近的 run events，返回顺序为新到旧。"""
    redis = await get_redis_client()
    key = _event_stream_key(run_id)
    rows = await redis.xrevrange(key, max="+", min="-", count=limit)
    events = []

    for event_id, fields in rows:
        event = _decode_run_stream_row(run_id, event_id, fields)
        if event is not None:
            events.append(event)
    return events


async def get_last_run_stream_seq(run_id: str) -> str:
    """读取当前 Run 最新事件游标。"""
    redis = await get_redis_client()
    key = _event_stream_key(run_id)
    rows = await redis.xrevrange(key, max="+", min="-", count=1)
    if not rows:
        return "0-0"
    event_id, _ = rows[0]
    return str(event_id)


async def close_queue_clients() -> None:
    """关闭当前进程复用的 ARQ 与 Redis 连接。"""
    global _arq_pool
    if _arq_pool is not None:
        try:
            await _arq_pool.close()
        except Exception:
            pass
        _arq_pool = None
    await close_async_redis_client()
