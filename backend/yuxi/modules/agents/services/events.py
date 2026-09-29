"""以持久接收和执行序号跨 Run 订阅 Thread 事件。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import HTTPException

from yuxi.modules.agents.repositories.runs import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.modules.agents.repositories.input_receipt import AgentInputReceiptRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.services.scope import ActorScope
from yuxi.modules.agents.services.threads import require_thread
from yuxi.modules.agents.services.transport import list_recent_run_stream_events, list_run_stream_events
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.infrastructure.observability.logging import logger
import os
from yuxi.shared.datetime import utc_now_naive

SSE_HEARTBEAT_SECONDS = int(os.getenv("RUN_SSE_HEARTBEAT_SECONDS", "15"))
SSE_MAX_CONNECTION_MINUTES = int(os.getenv("RUN_SSE_MAX_CONNECTION_MINUTES", "30"))

_CURSOR = re.compile(r"^1\.(\d+)\.(\d+)\.(\d+-\d+)\.([012])$")


@dataclass(slots=True)
class _Cursor:
    """分别保存 PG 接收、PG Run 和当前 Redis Run 的位置。"""

    receipt_seq: int = 0
    run_seq: int = 0
    event_seq: str = "0-0"
    run_phase: int = 0

    def encode(self) -> str:
        """生成适合 SSE Last-Event-ID 的紧凑游标。"""
        return f"1.{self.receipt_seq}.{self.run_seq}.{self.event_seq}.{self.run_phase}"


def _parse_cursor(raw: str | None) -> _Cursor:
    """只接受本协议生成的无状态游标。"""
    if not raw:
        return _Cursor()
    match = _CURSOR.fullmatch(raw)
    if match is None:
        raise HTTPException(status_code=422, detail="事件游标无效")
    receipt_seq, run_seq, event_seq, run_phase = match.groups()
    return _Cursor(int(receipt_seq), int(run_seq), event_seq, int(run_phase))


def validate_event_cursor(raw: str | None) -> None:
    """在 HTTP 响应头发送前拒绝无效的恢复游标。"""
    _parse_cursor(raw)


def _event(
    *, type: str, thread_id: str, cursor: _Cursor, payload: dict, turn_id=None, input_id=None, run_id=None
) -> dict:
    """生成统一的结构化 Thread 事件。"""
    return {
        "type": type,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "input_id": input_id,
        "run_id": run_id,
        "cursor": cursor.encode(),
        "payload": payload,
    }


async def stream_thread_events(
    *, scope: ActorScope, thread_id: str, after_cursor: str | None = None
) -> AsyncIterator[dict]:
    """短事务重读 PG 边界并转发当前 Run 的短期增量。"""
    cursor = _parse_cursor(after_cursor)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SSE_MAX_CONNECTION_MINUTES * 60
    next_heartbeat = loop.time() + SSE_HEARTBEAT_SECONDS
    resynced_positions: set[tuple[str, str]] = set()
    while loop.time() < deadline:
        async with pg_manager.get_async_session_context() as db:
            await require_thread(db=db, scope=scope, thread_id=thread_id)
            receipts = await AgentInputReceiptRepository(db).list_after_sequence(
                uid=scope.uid,
                app_id=scope.app_id,
                thread_id=thread_id,
                after_sequence=cursor.receipt_seq,
            )
            runs = await AgentRunRepository(db).list_thread_runs_after_sequence(
                thread_id=thread_id,
                uid=scope.uid,
                app_id=scope.app_id,
                after_sequence=max(0, cursor.run_seq - 1),
            )
            turn_repo = AgentTurnRepository(db)
            run_facts = [
                (
                    run,
                    await turn_repo.get_for_scope(
                        turn_id=run.turn_id,
                        thread_id=run.runtime_scope_id,
                        uid=scope.uid,
                        app_id=scope.app_id,
                    ),
                )
                for run in runs
            ]

        emitted = False
        for receipt in receipts:
            cursor.receipt_seq = receipt.receive_seq
            if receipt.event_type in {"agent.thread.create", "agent.thread.input.message"}:
                event_type = "agent.thread.input.received" if receipt.input_id else "agent.thread.created"
            else:
                event_type = {
                    "yuxi.thread.input.cancel_input": "agent.thread.input.cancelled",
                    "yuxi.thread.input.continue": "agent.thread.queue.continued",
                    "yuxi.thread.input.cancel": "agent.thread.turn.cancelling",
                    "yuxi.thread.input.resume": "agent.thread.turn.resumed",
                }.get(receipt.event_type, "agent.thread.control.accepted")
            yield _event(
                type=event_type,
                thread_id=thread_id,
                cursor=cursor,
                input_id=receipt.input_id,
                turn_id=receipt.turn_id,
                run_id=receipt.run_id,
                payload={"event_id": receipt.id, "receive_seq": receipt.receive_seq},
            )
            emitted = True

        for run, turn in run_facts:
            sequence = run.execution_seq
            if sequence is None or sequence < cursor.run_seq or (sequence == cursor.run_seq and cursor.run_phase == 2):
                continue
            if sequence > cursor.run_seq:
                cursor.run_seq = sequence
                cursor.event_seq = "0-0"
                cursor.run_phase = 0
                if run.input_id:
                    yield _event(
                        type="agent.thread.input.consumed",
                        thread_id=thread_id,
                        cursor=cursor,
                        turn_id=run.turn_id,
                        input_id=run.input_id,
                        run_id=run.id,
                        payload={"status": "consumed"},
                    )
                    emitted = True
            try:
                events = await list_run_stream_events(run.id, after_seq=cursor.event_seq)
            except Exception as exc:
                logger.warning("读取 Run 增量失败: run=%s error=%s", run.id, exc)
                events = []
            if cursor.event_seq != "0-0":
                try:
                    oldest = await list_run_stream_events(run.id, after_seq="0-0", limit=1)
                except Exception:
                    oldest = []
                expired = not oldest or _redis_id(oldest[0]["seq"]) > _redis_id(cursor.event_seq)
            else:
                expired = run.status in TERMINAL_RUN_STATUSES and not events
            position = (run.id, cursor.event_seq)
            if expired and position not in resynced_positions:
                resynced_positions.add(position)
                from yuxi.modules.agents.services.messages import get_thread_history

                async with pg_manager.get_async_session_context() as db:
                    snapshot = await get_thread_history(db=db, scope=scope, thread_id=thread_id)
                yield _event(
                    type="agent.thread.resync",
                    thread_id=thread_id,
                    cursor=cursor,
                    turn_id=run.turn_id,
                    input_id=run.input_id,
                    run_id=run.id,
                    payload={"reason": "run_events_expired", "snapshot": snapshot, "resume_cursor": cursor.encode()},
                )
                emitted = True
            for item in events:
                cursor.event_seq = item["seq"]
                envelope = item.get("payload") or {}
                yield _event(
                    type="agent.thread.output",
                    thread_id=thread_id,
                    cursor=cursor,
                    turn_id=run.turn_id,
                    input_id=run.input_id,
                    run_id=run.id,
                    payload={**(envelope.get("payload") or {}), "event": item.get("event_type")},
                )
                emitted = True
            if len(events) == 200:
                break
            if run.status in TERMINAL_RUN_STATUSES:
                if run.runtime_cleanup_pending:
                    break
                try:
                    latest = await list_recent_run_stream_events(run.id, limit=1)
                except Exception:
                    latest = []
                has_end = bool(latest and latest[-1]["event_type"] == "end")
                finished_age = (utc_now_naive() - run.finished_at).total_seconds() if run.finished_at else 0
                if not has_end and not expired and finished_age < 2:
                    break
                if cursor.run_phase == 0:
                    cursor.run_phase = 2 if run.run_type == "subagent" else 1
                    yield _event(
                        type=f"agent.thread.run.{run.status}",
                        thread_id=thread_id,
                        cursor=cursor,
                        turn_id=run.turn_id,
                        input_id=run.input_id,
                        run_id=run.id,
                        payload={
                            "status": run.status,
                            "error_type": run.error_type,
                            "error_message": run.error_message,
                        },
                    )
                    emitted = True
                if run.run_type == "subagent":
                    continue
                if (
                    turn is not None
                    and turn.current_run_id == run.id
                    and turn.status in {"waiting", "completed", "failed", "cancelled"}
                ):
                    cursor.run_phase = 2
                    yield _event(
                        type=f"agent.thread.turn.{turn.status}",
                        thread_id=thread_id,
                        cursor=cursor,
                        turn_id=turn.id,
                        input_id=run.input_id,
                        run_id=run.id,
                        payload={
                            "status": turn.status,
                            "result_run_id": turn.result_run_id,
                            "waitpoint": turn.waitpoint,
                        },
                    )
                    emitted = True
                    continue
                if turn is not None and turn.current_run_id != run.id:
                    cursor.run_phase = 2
                    continue
                break
            break

        if emitted:
            next_heartbeat = loop.time() + SSE_HEARTBEAT_SECONDS
            continue
        if loop.time() >= next_heartbeat:
            yield _event(type="agent.thread.heartbeat", thread_id=thread_id, cursor=cursor, payload={})
            next_heartbeat = loop.time() + SSE_HEARTBEAT_SECONDS
        await asyncio.sleep(0.2)


def _redis_id(value: str) -> tuple[int, int]:
    """按 Redis Stream ID 的数值顺序比较裁剪边界。"""
    millisecond, sequence = value.split("-", 1)
    return int(millisecond), int(sequence)
