"""e2e 测试共享的 HTTP 辅助函数。

多个 e2e 文件重复的 Agent 清理、Public Thread 归档、SSE 消费与状态轮询集中在此，
避免同构 helper 在多份测试文件中漂移。
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

RUN_TIMEOUT_SECONDS = int(os.getenv("E2E_RUN_TIMEOUT_SECONDS", "240"))
QUOTA_EXHAUSTED_MARKERS = ("Error code: 429", "Token Plan 用量上限")


def skip_if_external_quota(payload: object) -> None:
    """真实模型外部配额/限流（HTTP 429）时显式跳过，避免把外部额度当代码回归。

    只匹配明确的 429 措辞；其余失败仍按断言失败处理，不用 skip 掩盖回归。
    """
    text = str(payload or "")
    if any(marker in text for marker in QUOTA_EXHAUSTED_MARKERS):
        pytest.skip(f"外部模型配额/限流（HTTP 429），跳过真实模型断言：{text[:120]}")


def postgres_dsn() -> str:
    return os.getenv("POSTGRES_URL", "postgresql+asyncpg://postgres:postgres@postgres:5432/yuxi").replace("+asyncpg", "")


async def wait_model_provider_cache() -> None:
    """等待新测试供应商的跨进程缓存传播；不替代模型执行与持久结果验证。"""
    from yuxi.modules.models.providers.cache import _CACHE_TTL_SECONDS

    await asyncio.sleep(_CACHE_TTL_SECONDS + 0.1)


async def wait_for_consumed_input(client: httpx.AsyncClient, headers: dict, receipt: dict) -> dict:
    """校验接收回执不带执行归属，再从真实 Input 查询领取结果。"""
    assert receipt["input_id"] and receipt["turn_id"] is None and receipt["run_id"] is None, receipt
    session_id = receipt["session_id"]
    async with asyncio.timeout(60):
        while True:
            response = await client.get(f"/api/v1/agents/sessions/{session_id}/inputs/{receipt['input_id']}", headers=headers)
            assert response.status_code == 200, response.text
            snapshot = response.json()
            assert snapshot["status"] != "cancelled", snapshot
            if snapshot["status"] == "consumed":
                assert snapshot["run_id"] and snapshot["turn_id"] and snapshot["items"], snapshot
                return {**snapshot, "session_id": session_id}
            await asyncio.sleep(0.1)


async def delete_agent(client: httpx.AsyncClient, headers: dict[str, str], slug: str) -> None:
    """等待终态 Run 的异步清理完成后删除测试智能体。"""
    async with asyncio.timeout(30):
        while True:
            response = await client.delete(f"/api/agent/{slug}", headers=headers)
            if response.status_code != 409 or response.json().get("detail") != "智能体仍有活跃执行或待处理输入":
                assert response.status_code in {200, 404}, response.text
                return
            await asyncio.sleep(0.2)


async def iter_public_thread_events(client: httpx.AsyncClient, headers: dict[str, str], thread_id: str):
    """解析 Public Thread SSE 的结构化事件。"""
    async with client.stream("GET", f"/api/v1/agents/sessions/{thread_id}/events", headers=headers) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                yield json.loads(line[6:])


async def archive_public_thread(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    thread_id: str,
    *,
    turn_id: str | None = None,
) -> None:
    """取消仍活跃的测试 Turn，等待收敛后归档 Thread。"""
    if turn_id:
        turn_url = f"/api/v1/agents/sessions/{thread_id}/turns/{turn_id}"
        try:
            async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
                snapshot = await client.get(turn_url, headers=headers)
                assert snapshot.status_code == 200, snapshot.text
                if snapshot.json()["status"] not in {"completed", "failed", "cancelled"}:
                    cancel = await client.post(
                        f"/api/v1/agents/sessions/{thread_id}/events",
                        headers={**headers, "Idempotency-Key": f"e2e-cleanup-{turn_id}"},
                        json={"events": [{"type": "agent.session.input.cancel", "yuxi": {"turn_id": turn_id}}]},
                    )
                    assert cancel.status_code in {202, 409}, cancel.text
                    while True:
                        snapshot = await client.get(turn_url, headers=headers)
                        assert snapshot.status_code == 200, snapshot.text
                        if snapshot.json()["status"] in {"completed", "failed", "cancelled"}:
                            break
                        await asyncio.sleep(1)
        except TimeoutError:
            pytest.fail(f"测试 Turn 取消后未收敛: {turn_id}")
    async with asyncio.timeout(30):
        while True:
            archive = await client.post(f"/api/v1/agents/sessions/{thread_id}/archive", headers=headers)
            if archive.status_code != 409 or archive.json().get("detail") != "Thread 仍有活跃执行或待处理输入":
                assert archive.status_code == 200, archive.text
                return
            await asyncio.sleep(0.2)
