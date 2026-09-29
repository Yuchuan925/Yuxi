"""Run 的短期增量仍按 Redis Stream 保存并过期。"""

import json
import uuid

import pytest

from yuxi.services.agents.transport import RUN_EVENTS_STREAM_TTL_SECONDS, append_run_stream_event, get_redis_client
from yuxi.storage.redis import close_async_redis_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
async def isolated_run_events_redis_client():
    """只在当前测试的事件循环内复用 Redis 客户端。"""
    await close_async_redis_client()
    yield
    await close_async_redis_client()


async def test_stream_batch_preserves_payload_and_expiry():
    """从真实 Redis 回读事件身份、内容与 TTL。"""
    run_id = str(uuid.uuid4())
    key = f"run:events:{run_id}"
    redis = await get_redis_client()
    try:
        seq = await append_run_stream_event(run_id, "metadata", {"probe": "batch"}, thread_id="test-thread")
        rows = await redis.xrange(key)
        assert len(rows) == 1
        assert rows[0][0] == seq
        payload = json.loads(rows[0][1]["payload"])
        assert payload["run_id"] == run_id
        assert payload["thread_id"] == "test-thread"
        assert payload["payload"] == {"probe": "batch"}
        assert 0 < await redis.ttl(key) <= RUN_EVENTS_STREAM_TTL_SECONDS
    finally:
        await redis.delete(key)
