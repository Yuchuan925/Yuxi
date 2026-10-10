"""Thread 优先队列在真实 PostgreSQL 上的接收、合批和消费证据。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.integration.services.test_agent_input_concurrency import _binding, _queue_inputs
from test.integration.services.test_agent_input_schema import _create_schema, _drop_schema
from yuxi.api.dependencies.auth import get_db
from yuxi.api.routers.public_v1.agents.auth import require_public_context
from yuxi.api.routers.public_v1.agents.events import router
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.agents.models.inputs import AgentInput, AgentInputReceipt
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.input_receipt import AgentInputReceiptRepository
from yuxi.modules.agents.repositories.definitions import DEFAULT_SHARE_CONFIG
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.sessions import SessionRepository
from yuxi.modules.agents.services import input_config, inputs, messages, runs, scheduler, threads, turns
from yuxi.modules.agents.services.input_messages import build_chat_input_message
from yuxi.modules.agents.services.scope import ActorScope

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
SCOPE = ActorScope(uid="input-user", app_id=None)


@pytest.mark.parametrize("outcome", ["generated", "timeout", "manual"])
async def test_auto_title_persists_once_and_preserves_manual_rename(sessions, monkeypatch, outcome):
    """回读真实数据库验证生成、失败回退及同名手动修改优先。"""
    from unittest.mock import AsyncMock

    from yuxi.modules.agents.services import titles

    text = "请帮我分析 PostgreSQL 的慢查询并给出索引优化建议"
    fallback = text[:30]
    async with sessions() as db:
        await SessionRepository(db).add_session(
            uid=SCOPE.uid,
            agent_id="main",
            project_id="input-project",
            thread_id="title-thread",
            config_snapshot={"model": "test:chat", "tool_approval_mode": "default"},
        )
        await db.commit()

    async def invoke(_messages):
        """模型边界返回固定结果，等待期间通过真实改名用例制造竞争。"""
        async with sessions() as db:
            persisted = await SessionRepository(db).get_session_by_thread_id("title-thread")
            assert persisted.title == fallback
            if outcome == "manual":
                await threads.update_thread(db=db, scope=SCOPE, thread_id="title-thread", title=fallback)
        if outcome == "timeout":
            raise TimeoutError
        return SimpleNamespace(text="PostgreSQL 慢查询优化")

    monkeypatch.setattr(titles, "system_options", SimpleNamespace(get=AsyncMock(return_value={"fast_model": "test:fast"})))
    monkeypatch.setattr(titles, "load_chat_model", lambda *args, **kwargs: SimpleNamespace(ainvoke=invoke))
    await titles.generate_session_title(thread_id="title-thread", uid=SCOPE.uid, app_id=None, content=text)

    async def unexpected(_messages):
        """第二次调用不得再生成标题。"""
        pytest.fail("同一会话重复生成标题")

    monkeypatch.setattr(titles, "load_chat_model", lambda *args, **kwargs: SimpleNamespace(ainvoke=unexpected))
    await titles.generate_session_title(thread_id="title-thread", uid=SCOPE.uid, app_id=None, content="第二条消息")
    async with sessions() as db:
        persisted = await SessionRepository(db).get_session_by_thread_id("title-thread")
        assert persisted.title == ("PostgreSQL 慢查询优化" if outcome == "generated" else fallback)


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """只使用测试拥有的隔离 Schema。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本文件不创建知识库资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本文件不创建沙盒资源。"""
    yield


@pytest.fixture
async def sessions(monkeypatch):
    """创建真实 Agent 与隔离数据库，仅隔离外部模型发现和投递。"""
    schema, admin_engine, engine = await _create_schema()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Agent(
                slug="main",
                backend_id="ChatbotAgent",
                name="test",
                created_by=SCOPE.uid,
                share_config=DEFAULT_SHARE_CONFIG.copy(),
                config_json={"context": {"model": "test:chat"}},
            )
        )
        agent_session = await SessionRepository(db).get_session_by_thread_id("input-thread")
        agent_session.queue_paused = True
        agent_session.config_snapshot = {"model": "test:chat", "tool_approval_mode": "default"}
        await db.commit()

    async def resolve_binding(**kwargs):
        """提供已授权的固定 Workdir，避免测试物化宿主目录。"""
        return _binding()

    async def deliver(dispatch):
        """替代外部 ARQ，执行状态通过数据库回读核对。"""
        async with factory() as db:
            assert (await db.get(AgentRun, dispatch.run_id)).status == "pending"

    @asynccontextmanager
    async def context():
        """安全钩子与恢复扫描读取同一个隔离数据库。"""
        async with factory() as db:
            yield db
            await db.commit()

    async def notify(*args, **kwargs):
        """隔离远端 tracing 与 Redis 取消信号。"""

    monkeypatch.setattr(input_config.model_cache, "get_model_info", lambda spec: SimpleNamespace(model_type="chat"))
    monkeypatch.setattr(turns, "finish_turn_observation_if_terminal", notify)
    monkeypatch.setattr(turns, "publish_cancel_signals", notify)
    monkeypatch.setattr(scheduler, "resolve_session_workdir_binding", resolve_binding)
    monkeypatch.setattr(threads, "deliver", deliver)
    monkeypatch.setattr(scheduler, "deliver", deliver)
    monkeypatch.setattr(scheduler.pg_manager, "get_async_session_context", context)
    try:
        yield factory
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def _send(sessions, content: str, mode: str = "steer") -> dict:
    """通过接收用例提交独立幂等消息。"""
    async with sessions() as db:
        return await inputs.accept_message(
            db=db,
            scope=SCOPE,
            thread_id="input-thread",
            idempotency_key=content,
            mode=mode,
            messages=[build_chat_input_message(content)],
        )


async def _start(sessions, *, running: bool = True):
    """领取首条消息并取得执行所有权。"""
    await _queue_inputs(sessions, count=1)
    async with sessions() as db:
        agent_session = await SessionRepository(db).lock_session_by_thread_id("input-thread")
        agent_session.queue_paused = False
        dispatch = await scheduler.claim_next_input(db=db, agent_session=agent_session, binding=_binding())
        run = await db.get(AgentRun, dispatch.run_id)
        if running:
            run, acquired = await AgentRunRepository(db).mark_running(dispatch.run_id, worker_id="owner", lease_seconds=60)
            assert acquired
        await db.commit()
        return run


async def test_empty_session_freezes_system_default_before_first_input(sessions, monkeypatch):
    """创建后的默认模型与 Agent 修改不影响首个输入，投递前事务已提交。"""
    from unittest.mock import AsyncMock
    from dataclasses import replace

    defaults = {"default_model": "test:original-default"}
    monkeypatch.setattr(input_config, "system_options", SimpleNamespace(get=AsyncMock(return_value=defaults)))
    monkeypatch.setattr(inputs, "resolve_session_workdir_binding", AsyncMock(return_value=replace(_binding(), directory_mode="linked")))
    async with sessions() as db:
        agent = await db.scalar(select(Agent).where(Agent.slug == "main"))
        agent.config_json = {"context": {"model": "", "system_prompt": "ORIGINAL"}}
        await db.commit()
        created = await inputs.create_thread(
            db=db,
            scope=SCOPE,
            agent_slug="main",
            thread_id="default-snapshot-thread",
            idempotency_key="default-snapshot-create",
        )
        assert created["input_id"] is None
    async with sessions() as db:
        agent_session = await SessionRepository(db).get_session_by_thread_id("default-snapshot-thread")
        snapshot = agent_session.config_snapshot
        assert snapshot["model"] == "test:original-default"
        assert snapshot["system_prompt"] == "ORIGINAL"
        agent = await db.scalar(select(Agent).where(Agent.slug == "main"))
        agent.config_json = {"context": {"model": "", "system_prompt": "CHANGED"}}
        await db.commit()
    defaults["default_model"] = "test:changed-default"
    async with sessions() as db:
        accepted = await inputs.accept_message(
            db=db,
            scope=SCOPE,
            thread_id="default-snapshot-thread",
            idempotency_key="default-snapshot-input",
            mode=None,
            messages=[build_chat_input_message("first input")],
        )
    async with sessions() as db:
        run = await db.get(AgentRun, accepted["run_id"])
        assert run.input_payload["context_snapshot"]["model"] == "test:original-default"
        assert run.input_payload["context_snapshot"] == snapshot
        assert (await db.get(Session, run.session_record_id)).config_snapshot == snapshot
        assert (await db.get(AgentInput, accepted["input_id"])).consumed_run_id == run.id
        receipt = await AgentInputReceiptRepository(db).get_for_scope(
            uid=SCOPE.uid,
            app_id=SCOPE.app_id,
            thread_id="default-snapshot-thread",
            idempotency_key="default-snapshot-input",
        )
        assert receipt.input_id == accepted["input_id"] and receipt.run_id == run.id


async def test_idle_steer_batch_precedes_fifo_and_is_sealed_on_claim(sessions):
    """空闲暂停队列合并 S1/S2，恢复后优先消费；S3 形成新的批次。"""
    await _queue_inputs(sessions, count=2)
    first = await _send(sessions, "S1")
    second = await _send(sessions, "S2")
    assert first["input_id"] == second["input_id"]
    assert first["turn_id"] is None and first["run_id"] is None
    assert "mode" not in first
    async with sessions() as db:
        snapshot = await threads.get_queue_snapshot(db=db, scope=SCOPE, thread_id="input-thread")
        assert [(item["kind"], item["content"]) for item in snapshot["inputs"]] == [
            ("steer", "S1\nS2"),
            ("follow_up", "message-0"),
            ("follow_up", "message-1"),
        ]
        assert all(item["turn_id"] is None for item in snapshot["inputs"])
        accepted = await threads.continue_queue(db=db, scope=SCOPE, thread_id="input-thread", idempotency_key="continue")
    third = await _send(sessions, "S3")
    assert third["input_id"] != first["input_id"]
    async with sessions() as db:
        batch = await db.get(AgentInput, first["input_id"])
        assert batch.status == "consumed" and batch.consumed_run_id == accepted["run_id"]
        claimed_run = await db.get(AgentRun, accepted["run_id"])
        assert batch.turn_id == claimed_run.turn_id
        receipt = await db.get(AgentInputReceipt, accepted["event_id"])
        assert receipt.turn_id == claimed_run.turn_id
        replay = await threads.continue_queue(db=db, scope=SCOPE, thread_id="input-thread", idempotency_key="continue")
        assert replay == accepted
        assert await db.scalar(select(func.count()).select_from(AgentRun)) == 1
        messages = await AgentInputRepository(db).list_messages(batch.id)
        assert [(item.content, item.run_id, item.delivery_status) for item in messages] == [
            ("S1", accepted["run_id"], "dispatched"),
            ("S2", accepted["run_id"], "dispatched"),
        ]
        pending = await AgentInputRepository(db).list_pending_inputs(thread_id="input-thread", uid=SCOPE.uid, app_id=None)
        assert [item.id for item in pending] == [third["input_id"], "input-0", "input-1"]
        assert (await db.get(AgentRun, accepted["run_id"])).resume_from_run_id is None
        retry = await inputs.accept_message(
            db=db,
            scope=SCOPE,
            thread_id="input-thread",
            idempotency_key="S1",
            mode="steer",
            messages=[build_chat_input_message("S1")],
        )
        assert retry["input_id"] == batch.id and retry["run_id"] == batch.consumed_run_id
        assert await db.scalar(select(func.count()).select_from(AgentInputReceipt).where(AgentInputReceipt.idempotency_key == "S1")) == 1


async def test_safe_handoff_consumes_batch_in_same_turn_with_current_config(sessions):
    """运行中领取全部 steer，旧 Run yielded，follow-up 保持 pending。"""
    current = await _start(sessions)
    follow = await _send(sessions, "F1", "follow_up")
    first = await _send(sessions, "S1")
    second = await _send(sessions, "S2")
    assert first["input_id"] == second["input_id"]
    assert await runs.should_yield_for_steer(current.id)
    async with sessions() as db:
        await SessionRepository(db).lock_session_by_thread_id("input-thread")
        run = await db.get(AgentRun, current.id)
        settled = await runs.settle_checkpoint(db=db, run=run, worker_id="owner", status="completed", token_usage=None)
        assert settled.status == "yielded" and settled.next_run_id
        await db.commit()
    async with sessions() as db:
        old = await db.get(AgentRun, current.id)
        next_run = await db.get(AgentRun, settled.next_run_id)
        assert old.status == "yielded"
        assert next_run.turn_id == current.turn_id and next_run.resume_from_run_id == current.id
        assert next_run.input_payload == current.input_payload
        assert next_run.input_payload["context_snapshot"]["model"] != "test:chat"
        assert (await db.get(AgentInput, follow["input_id"])).status == "pending"
        messages = await AgentInputRepository(db).list_messages(next_run.input_id)
        assert [message.content for message in messages] == ["S1", "S2"]
        turn = await db.get(AgentTurn, current.turn_id)
        assert turn.current_run_id == next_run.id and turn.result_run_id is None


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_terminal_failure_preserves_steer_for_explicit_continue(sessions, status):
    """失败或取消保留 Thread 的优先批次，解除暂停后创建新 Turn。"""
    current = await _start(sessions, running=status != "cancelled")
    follow = await _send(sessions, "F1", "follow_up")
    steer = await _send(sessions, "S1")
    async with sessions() as db:
        await SessionRepository(db).lock_session_by_thread_id("input-thread")
        run = await db.get(AgentRun, current.id)
        if status == "failed":
            await runs.settle_checkpoint(db=db, run=run, worker_id="owner", status=status, token_usage=None)
        else:
            await turns.cancel_turn(
                db=db,
                scope=SCOPE,
                thread_id="input-thread",
                turn_id=current.turn_id,
                idempotency_key="cancel",
            )
        await db.commit()
    async with sessions() as db:
        agent_session = await SessionRepository(db).get_session_by_thread_id("input-thread")
        assert agent_session.queue_paused
        batch = await db.get(AgentInput, steer["input_id"])
        assert batch.status == "pending" and batch.turn_id is None
        assert (await AgentInputRepository(db).list_messages(batch.id))[0].delivery_status == "queued"
        result = await threads.continue_queue(db=db, scope=SCOPE, thread_id="input-thread", idempotency_key="continue")
    async with sessions() as db:
        consumed = await db.get(AgentInput, steer["input_id"])
        assert consumed.consumed_run_id == result["run_id"] and consumed.turn_id != current.turn_id
        assert (await db.get(AgentInput, follow["input_id"])).status == "pending"


async def test_recovery_claims_idle_steer_without_any_follow_up(sessions):
    """崩溃后仅剩 steer 的 ready Thread 也能由恢复扫描领取。"""
    steer = await _send(sessions, "S1")
    async with sessions() as db:
        agent_session = await SessionRepository(db).get_session_by_thread_id("input-thread")
        agent_session.queue_paused = False
        await db.commit()
    await scheduler.recover_pending_dispatches()
    async with sessions() as db:
        batch = await db.get(AgentInput, steer["input_id"])
        assert batch.status == "consumed" and batch.consumed_run_id
        run = await db.get(AgentRun, batch.consumed_run_id)
        assert run.input_payload["context_snapshot"]["model"] == "test:chat"
        assert run.resume_from_run_id is None


@pytest.mark.parametrize("cancel_batch", [False, True])
async def test_steer_before_first_model_is_saved_as_yielded_not_failed(sessions, monkeypatch, cancel_batch):
    """模型前让位允许无输出；提交前取消批次则继续原 Run 而非失败。"""
    from langchain.messages import AIMessage
    from yuxi.modules.agents.runtime.middlewares.steer import SteerMiddleware

    current = await _start(sessions)
    steer = await _send(sessions, "S3")
    context = SimpleNamespace(run_id=current.id)
    jumped = await SteerMiddleware().abefore_model({}, SimpleNamespace(context=context))
    assert jumped == {"jump_to": "end"} and context.steer_before_model
    if cancel_batch:
        async with sessions() as db:
            await threads.cancel_input(
                db=db,
                scope=SCOPE,
                thread_id="input-thread",
                input_id=steer["input_id"],
                idempotency_key="cancel-S3",
            )

    async def dispatch(run_id):
        """投递时验证接续 Run 已提交，避免用通知冒充消费事实。"""
        async with sessions() as db:
            run = await db.get(AgentRun, run_id)
            assert run.status == "pending" and run.input_id == steer["input_id"]

    monkeypatch.setattr(messages, "enqueue_agent_run", dispatch)
    async with sessions() as db:
        status = await messages.save_messages_from_langgraph_state(
            state=SimpleNamespace(values={"messages": []}),
            thread_id="input-thread",
            session_repo=SessionRepository(db),
            run_id=current.id,
            turn_id=current.turn_id,
            worker_id="owner",
            complete_run=True,
            steer_before_model=context.steer_before_model,
        )
    assert status == ("running" if cancel_batch else "yielded")
    async with sessions() as db:
        old = await db.get(AgentRun, current.id)
        turn = await db.get(AgentTurn, current.turn_id)
        batch = await db.get(AgentInput, steer["input_id"])
        assert old.status == status and old.output_message_id is None
        assert turn.status == "running" and turn.result_run_id is None
        if cancel_batch:
            assert turn.current_run_id == old.id and old.worker_id == "owner"
            assert batch.status == "cancelled" and batch.consumed_run_id is None
            assert await db.scalar(select(func.count()).select_from(AgentRun)) == 1
            status = await messages.save_messages_from_langgraph_state(
                state=SimpleNamespace(values={"messages": [AIMessage(content="original task done", id="after-cancel")]}),
                thread_id="input-thread",
                session_repo=SessionRepository(db),
                run_id=current.id,
                turn_id=current.turn_id,
                worker_id="owner",
                complete_run=True,
            )
            assert status == "completed"
        else:
            assert turn.current_run_id == batch.consumed_run_id and batch.status == "consumed"
        assert not (await SessionRepository(db).get_session_by_thread_id("input-thread")).queue_paused
    if cancel_batch:
        async with sessions() as db:
            turn = await db.get(AgentTurn, current.turn_id)
            old = await db.get(AgentRun, current.id)
            assert turn.result_run_id == old.id and old.status == "completed"
            assert (await db.get(Message, old.output_message_id)).content == "original task done"


async def test_cancelled_steer_continuation_preserves_wire_ids_and_audit_order(sessions, monkeypatch):
    """图段重置 seq 后，真实投影 ID 唯一且 PG 审计仍按原执行顺序返回。"""
    from langchain.messages import AIMessage
    from unittest.mock import AsyncMock
    from test.unit.services.test_openai_event_adapter import native
    from yuxi.modules.agents.runtime.base import GraphExecutionResult
    from yuxi.modules.agents.runtime.context import BaseContext
    from yuxi.modules.agents.runtime.middlewares.steer import SteerMiddleware
    from yuxi.modules.agents.repositories.model_audit import ModelMessageAuditRepository
    from yuxi.modules.agents.services import execution
    from yuxi.modules.agents.services.tracing import LangfuseRunContext

    current = await _start(sessions)
    steer = await _send(sessions, "wire-steer")
    context = BaseContext(thread_id="input-thread", uid=SCOPE.uid, run_id=current.id, worker_id="owner")
    raw_events, graph_inputs = [], []

    class AgentSource:
        """提供原生模型协议图段；只隔离图源，不替换 recorder 或 adapter。"""

        calls = 0
        history = []

        async def stream_messages_with_state(self, value, **kwargs):
            """第一段让位并取消批次，第二段从零开始原生序号。"""
            self.calls += 1
            graph_inputs.append([message.content for message in value])
            if self.calls == 1:
                self.history = list(value)
            operations = [("model-one", "first", 0), ("model-two", "second", 10)] if self.calls == 1 else [("model-three", "final", 0)]
            for operation, text, seq in operations:
                packets = [
                    {"event": "message-start", "id": operation},
                    {"event": "content-block-delta", "index": 0, "delta": {"type": "text-delta", "text": text}},
                    {"event": "content-block-finish", "index": 0, "content": {"type": "text", "text": text}},
                    {"event": "message-finish"},
                ]
                for index, packet in enumerate(packets):
                    event = native(seq + index, packet)
                    event["params"]["data"] = (packet, {"run_id": operation})
                    raw_events.append(event)
                    yield event
                self.history.append(AIMessage(content=text, id=operation))
            before_model = self.calls == 1
            if before_model:
                jump = await SteerMiddleware().abefore_model({}, SimpleNamespace(context=context))
                assert jump == {"jump_to": "end"} and context.steer_before_model
                async with sessions() as db:
                    await threads.cancel_input(
                        db=db,
                        scope=SCOPE,
                        thread_id="input-thread",
                        input_id=steer["input_id"],
                        idempotency_key="cancel-wire-steer",
                    )
            yield GraphExecutionResult(
                checkpoint=SimpleNamespace(values={"messages": list(self.history)}),
                steer_before_model=before_model,
            )

    source = AgentSource()

    async def resolve(**kwargs):
        """隔离模型发现，Thread、审计与输出持久化仍读取真实 PG。"""
        agent_session = await SessionRepository(kwargs["db"]).get_session_by_thread_id("input-thread")
        return SimpleNamespace(slug="main", name="test", backend_id="ChatbotAgent"), source, context, agent_session

    monkeypatch.setattr(execution, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(execution, "_build_langfuse_run_context", lambda **kwargs: LangfuseRunContext())
    monkeypatch.setattr(execution, "_flush_langfuse_best_effort", AsyncMock())
    async with sessions() as db:
        events = [
            event
            async for event in execution.stream_agent_chat(
                agent_slug="main",
                input_messages=[build_chat_input_message("message-0")],
                thread_id="input-thread",
                meta={
                    "run_id": current.id,
                    "turn_id": current.turn_id,
                    "worker_id": "owner",
                    "input_id": current.input_id,
                },
                current_user=SimpleNamespace(uid=SCOPE.uid),
                db=db,
                prepared_execution=SimpleNamespace(context=context),
            )
        ]
    assert events[-1].status == "completed"
    deltas = [event for event in events if isinstance(event, dict) and event["type"] == "agent.session.turn.output_text.delta"]
    assert [event["delta"] for event in deltas] == ["first", "second", "final"]
    assert len({event["event_id"] for event in deltas}) == 3
    assert graph_inputs == [["message-0"], []]
    assert [event["seq"] for event in raw_events] == [0, 1, 2, 3, 10, 11, 12, 13, 0, 1, 2, 3]
    async with sessions() as db:
        audits = await ModelMessageAuditRepository(db).list_for_run(current.id)
        assert [audit.operation_id for audit in audits] == ["model-one", "model-two", "model-three"]
        assert [audit.sequence for audit in audits] == [0, 10, 14]
        turn = await db.get(AgentTurn, current.turn_id)
        run = await db.get(AgentRun, current.id)
        assert turn.result_run_id == run.id and run.status == "completed"
        assert (await db.get(Message, run.output_message_id)).content == "final"
        assert (await db.get(AgentInput, steer["input_id"])).status == "cancelled"


async def test_completion_winning_thread_lock_still_accepts_steer(sessions):
    """完成先提交时，在途 steer 创建新 Turn，旧结果保持不变。"""
    current = await _start(sessions)
    locked, sending = asyncio.Event(), asyncio.Event()

    async def finish():
        """在 steer 竞争锁之前固定旧 Turn 的最终输出。"""
        async with sessions() as db:
            agent_session = await SessionRepository(db).lock_session_by_thread_id("input-thread")
            locked.set()
            await sending.wait()
            run = await db.get(AgentRun, current.id)
            output = Message(
                session_record_id=agent_session.id,
                role="assistant",
                content="done",
                run_id=run.id,
                turn_id=run.turn_id,
                delivery_status="complete",
            )
            db.add(output)
            await db.flush()
            await AgentRunRepository(db).set_output_message(run.id, output.id, worker_id="owner")
            settled = await runs.settle_checkpoint(db=db, run=run, worker_id="owner", status="completed", token_usage=None)
            assert settled.status == "completed"
            await db.commit()
            return output.id

    async def send():
        """发送方不选择目标 Turn，接收事务与完成事务竞争 Thread 锁。"""
        await locked.wait()
        sending.set()
        return await _send(sessions, "late-steer")

    output_id, accepted = await asyncio.wait_for(asyncio.gather(finish(), send()), timeout=15)
    assert accepted["turn_id"] != current.turn_id and accepted["run_id"]
    async with sessions() as db:
        old_turn = await db.get(AgentTurn, current.turn_id)
        old_run = await db.get(AgentRun, current.id)
        assert old_turn.status == "completed" and old_turn.result_run_id == current.id
        assert old_run.output_message_id == output_id
        new_run = await db.get(AgentRun, accepted["run_id"])
        assert new_run.resume_from_run_id is None and new_run.input_payload["context_snapshot"]["model"] == "test:chat"


async def test_concurrent_steer_receipts_share_one_thread_batch(sessions):
    """并发接收由 Thread 锁串行化，消息顺序以持久接收序号为准。"""
    accepted = await asyncio.gather(*(_send(sessions, f"S{index}") for index in range(4)))
    assert len({item["input_id"] for item in accepted}) == 1
    async with sessions() as db:
        receipts = list(await db.scalars(select(AgentInputReceipt).order_by(AgentInputReceipt.receive_seq)))
        messages = await AgentInputRepository(db).list_messages(accepted[0]["input_id"])
        assert [message.content for message in messages] == [receipt.idempotency_key for receipt in receipts]
        assert len(messages) == 4
        assert await db.scalar(select(func.count()).select_from(AgentInput)) == 1
        assert await db.scalar(select(func.count()).select_from(AgentTurn)) == 0


async def test_http_steer_has_no_target_or_effective_mode_and_keeps_scope(sessions):
    """实际 HTTP 适配器持久化优先输入，旧目标字段和越权作用域被拒绝。"""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/agents")

    async def database():
        """每个 HTTP 请求使用独立 PostgreSQL session。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_public_context] = lambda: SimpleNamespace(scope=SCOPE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "S1"}]}],
                    "yuxi": {"mode": "steer"},
                }
            ]
        }
        url = "/api/v1/agents/sessions/input-thread/events"
        response = await client.post(url, headers={"Idempotency-Key": "http-S1"}, json=body)
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert "mode" not in accepted and accepted["turn_id"] is None
        body["events"][0]["yuxi"]["turn_id"] = "stale-turn"
        assert (await client.post(url, headers={"Idempotency-Key": "http-stale"}, json=body)).status_code == 422
        del body["events"][0]["yuxi"]["turn_id"]
        app.dependency_overrides[require_public_context] = lambda: SimpleNamespace(scope=ActorScope(uid="another-user", app_id=None))
        assert (await client.post(url, headers={"Idempotency-Key": "http-spoof"}, json=body)).status_code == 404
    async with sessions() as db:
        persisted = await db.get(AgentInput, accepted["input_id"])
        assert persisted.kind == "steer" and persisted.status == "pending" and persisted.turn_id is None
        assert [message.content for message in await AgentInputRepository(db).list_messages(persisted.id)] == ["S1"]
        with pytest.raises(HTTPException) as exc:
            await inputs.accept_message(
                db=db,
                scope=SCOPE,
                thread_id="input-thread",
                idempotency_key="http-S1",
                mode="follow_up",
                messages=[build_chat_input_message("S1")],
            )
        assert exc.value.status_code == 409
