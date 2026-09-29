"""真实 PostgreSQL 上的 FIFO 领取竞争与 pending 投递补偿。"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.integration.services.test_agent_input_schema import _create_schema, _drop_schema
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.agents.input import AgentInputRepository
from yuxi.repositories.agents.input_receipt import AgentInputReceiptRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agents import inputs, runs, scheduler, threads, turns
from yuxi.services.agents.scope import ActorScope
from yuxi.services.agents.input_messages import build_chat_input_message
from yuxi.services.workdir_service import WorkdirBinding
from yuxi.storage.postgres.models_business import AgentInput, AgentRun, AgentTurn, Message

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本测试使用自己创建的隔离 Schema。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本测试不创建知识库资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本测试不创建沙盒资源。"""
    yield


async def _queue_inputs(sessions, *, count: int) -> None:
    """接收多条持久输入，保持原始消息及 FIFO 序号。"""
    async with sessions() as db:
        conversation = await ConversationRepository(db).get_conversation_by_thread_id("input-thread")
        for number in range(count):
            input_id = f"input-{number}"
            await AgentInputRepository(db).create(
                input_id=input_id,
                thread_id="input-thread",
                uid="input-user",
                app_id=None,
                agent_slug="main",
                kind="follow_up",
                input_payload={"model_spec": "ci-replay:deterministic-chat", "tool_approval_mode": "default"},
            )
            receipt = await AgentInputReceiptRepository(db).create(
                receipt_id=f"receipt-{number}",
                idempotency_key=f"key-{number}",
                uid="input-user",
                app_id=None,
                thread_id="input-thread",
                event_type="agent.thread.input.message",
                intent_hash=f"intent-{number}",
                input_id=input_id,
            )
            message = Message(
                conversation_id=conversation.id,
                role="user",
                content=f"message-{number}",
                delivery_status="queued",
            )
            db.add(message)
            await db.flush()
            await AgentInputRepository(db).add_messages(
                input_id=input_id, receipt_id=receipt.id, message_ids=[message.id]
            )
        await db.commit()


def _binding() -> WorkdirBinding:
    """只提供领取所需的已授权 Project 目录快照。"""
    return WorkdirBinding(
        conversation_id=1,
        thread_id="input-thread",
        uid="input-user",
        project_id="input-project",
        workdir_path="projects/input-project",
        directory_mode="managed",
    )


async def test_concurrent_claims_consume_only_fifo_head() -> None:
    """两个领取者持同一 Thread 锁时，只能创建一轮和一个首段 Run。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _queue_inputs(sessions, count=2)
        start = asyncio.Event()

        async def claim() -> str | None:
            """模拟两个完成接收事务后的独立调度者。"""
            async with sessions() as db:
                await start.wait()
                conversation = await ConversationRepository(db).lock_conversation_by_thread_id("input-thread")
                dispatch = await scheduler.claim_follow_up(db=db, conversation=conversation, binding=_binding())
                await db.commit()
                return dispatch.run_id if dispatch else None

        contenders = [asyncio.create_task(claim()), asyncio.create_task(claim())]
        start.set()
        first, second = await asyncio.gather(*contenders)
        assert len([run_id for run_id in (first, second) if run_id]) == 1
        async with sessions() as db:
            inputs = list((await db.scalars(select(AgentInput).order_by(AgentInput.received_seq))).all())
            assert [(item.id, item.status) for item in inputs] == [
                ("input-0", "consumed"), ("input-1", "pending")
            ]
            assert inputs[0].consumed_run_id in {first, second}
            assert inputs[1].turn_id is None and inputs[1].consumed_run_id is None
            assert await db.scalar(select(func.count()).select_from(AgentTurn)) == 1
            assert await db.scalar(select(func.count()).select_from(AgentRun)) == 1
            messages = list((await db.scalars(select(Message).order_by(Message.id))).all())
            assert [(message.content, message.delivery_status) for message in messages] == [
                ("message-0", "dispatched"), ("message-1", "queued")
            ]
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_recovery_republishes_committed_pending_run(monkeypatch) -> None:
    """Run 已提交但没有 Redis 任务时，恢复扫描仍找到同一个 Run。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _queue_inputs(sessions, count=1)
        async with sessions() as db:
            conversation = await ConversationRepository(db).lock_conversation_by_thread_id("input-thread")
            dispatch = await scheduler.claim_follow_up(db=db, conversation=conversation, binding=_binding())
            assert dispatch is not None
            await db.commit()

        @asynccontextmanager
        async def test_session():
            """让恢复用例读取隔离 Schema 的真实已提交记录。"""
            async with sessions() as db:
                yield db

        sent = []

        async def record_delivery(candidate):
            """记录补偿将要投递的持久 Run ID。"""
            sent.append(candidate.run_id)

        monkeypatch.setattr(scheduler.pg_manager, "get_async_session_context", test_session)
        monkeypatch.setattr(scheduler, "deliver", record_delivery)
        await scheduler.recover_pending_dispatches()
        assert sent == [dispatch.run_id]
        async with sessions() as db:
            run = await db.get(AgentRun, dispatch.run_id)
            assert run.status == "pending" and run.input_id == "input-0"
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_cancelling_newer_input_keeps_older_queue_head_visible() -> None:
    """取消较新的 Input 后，较早的待领取输入仍出现在持久队列快照。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _queue_inputs(sessions, count=2)
        scope = ActorScope(uid="input-user", app_id=None)
        async with sessions() as db:
            await threads.cancel_input(
                db=db, scope=scope, thread_id="input-thread", input_id="input-1",
                idempotency_key="cancel-newer-input",
            )
            snapshot = await threads.get_queue_snapshot(
                db=db, scope=scope, thread_id="input-thread"
            )
            assert snapshot["queue_paused"] is False
            assert [(item["input_id"], item["content"]) for item in snapshot["inputs"]] == [
                ("input-0", "message-0")
            ]
        async with sessions() as db:
            persisted = list((await db.scalars(select(AgentInput).order_by(AgentInput.received_seq))).all())
            assert [(item.id, item.status) for item in persisted] == [
                ("input-0", "pending"), ("input-1", "cancelled")
            ]
    finally:
        await _drop_schema(schema, admin_engine, engine)


@pytest.mark.parametrize("control", ["steer", "cancel"])
async def test_completion_winning_thread_lock_rejects_stale_control(control: str) -> None:
    """完成与 steer/取消竞争时，后来者不能改变已提交的 Turn 结果。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await _queue_inputs(sessions, count=1)
        async with sessions() as db:
            conversation = await ConversationRepository(db).lock_conversation_by_thread_id("input-thread")
            dispatch = await scheduler.claim_follow_up(db=db, conversation=conversation, binding=_binding())
            assert dispatch is not None
            run = await db.get(AgentRun, dispatch.run_id)
            _, acquired = await AgentRunRepository(db).mark_running(
                run.id, worker_id="owner-a", lease_seconds=60
            )
            assert acquired is True
            output = Message(
                conversation_id=conversation.id, role="assistant", content="done",
                run_id=run.id, turn_id=run.turn_id, delivery_status="complete",
            )
            db.add(output)
            await db.flush()
            await AgentRunRepository(db).set_output_message(run.id, output.id, worker_id="owner-a")
            turn_id = run.turn_id
            await db.commit()

        scope = ActorScope(uid="input-user", app_id=None)
        lock_held = asyncio.Event()
        control_started = asyncio.Event()

        async def finish():
            """持有 Thread 锁直到控制方已开始竞争，再提交最终结果。"""
            async with sessions() as db:
                await ConversationRepository(db).lock_conversation_by_thread_id("input-thread")
                lock_held.set()
                await control_started.wait()
                await asyncio.sleep(0.05)
                current = await AgentRunRepository(db).get_run(dispatch.run_id)
                settled = await runs.settle_checkpoint(
                    db=db, run=current, worker_id="owner-a", status="completed", token_usage=None
                )
                await db.commit()
                return settled

        async def send_control():
            """通过真实领域用例竞争同一 Thread 锁。"""
            await lock_held.wait()
            control_started.set()
            async with sessions() as db:
                try:
                    if control == "steer":
                        return await inputs.accept_message(
                            db=db, scope=scope, thread_id="input-thread",
                            idempotency_key=str(uuid.uuid4()), mode="steer",
                            turn_id=turn_id, messages=[build_chat_input_message("change")],
                        )
                    return await turns.cancel_turn(
                        db=db, scope=scope, thread_id="input-thread", turn_id=turn_id,
                        idempotency_key=str(uuid.uuid4()),
                    )
                except HTTPException as exc:
                    return exc.status_code

        settled, rejected = await asyncio.gather(finish(), send_control())
        assert (settled.status, settled.changed, rejected) == ("completed", True, 409)
        async with sessions() as db:
            run = await db.get(AgentRun, dispatch.run_id)
            turn = await db.get(AgentTurn, turn_id)
            conversation = await ConversationRepository(db).get_conversation_by_thread_id("input-thread")
            assert run.status == "completed" and turn.status == "completed"
            assert turn.result_run_id == run.id and conversation.queue_paused is False
            assert await db.scalar(select(func.count()).select_from(AgentRun)) == 1
            assert await db.scalar(select(func.count()).select_from(AgentInput)) == 1
    finally:
        await _drop_schema(schema, admin_engine, engine)
