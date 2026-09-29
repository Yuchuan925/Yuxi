"""新 Agent 输入模型在真实 PostgreSQL 上的投递与约束证据。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.agents.input import AgentInputRepository
from yuxi.repositories.agents.input_receipt import AgentInputReceiptRepository
from yuxi.repositories.agents.turn import AgentTurnRepository
from yuxi.services.agents.scheduler import claim_follow_up
from yuxi.services.workdir_service import WorkdirBinding
from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import AgentInput, AgentRun, AgentTurn, Conversation, Message, SubagentThread

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件只在隔离 PostgreSQL Schema 中运行。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """隔离 Schema 测试不创建知识库资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """隔离 Schema 测试不创建沙盒资源。"""
    yield


async def _create_schema():
    """建立新模型专用 Schema，并返回清理句柄。"""
    schema = f"pytest_agent_input_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    await manager.create_business_tables()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (username, uid, password_hash, role, login_failed_count, is_deleted) "
                "VALUES ('input-user', 'input-user', 'hash', 'user', 0, 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO projects (id, uid, selection_status, workdir_path, directory_mode) "
                "VALUES ('input-project', 'input-user', 'implicit', 'projects/input-project', 'managed')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO conversations (thread_id, uid, agent_id, project_id, is_pinned, status) "
                "VALUES ('input-thread', 'input-user', 'main', 'input-project', false, 'active')"
            )
        )
    return schema, admin_engine, engine


async def _drop_schema(schema: str, admin_engine, engine) -> None:
    """删除本测试创建的独立 Schema。"""
    await engine.dispose()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin_engine.dispose()


async def test_follow_up_claim_fixes_order_and_turn_only_once() -> None:
    """排队消息无 Turn，领取后多消息及回执按接收顺序固定到同一 Run。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            conversation_id = await db.scalar(select(text("id")).select_from(text("conversations")))
            input_repo = AgentInputRepository(db)
            receipt_repo = AgentInputReceiptRepository(db)
            input_item = await input_repo.create(
                input_id="input-one",
                thread_id="input-thread",
                uid="input-user",
                app_id=None,
                agent_slug="main",
                kind="follow_up",
                input_payload={"model_spec": "test"},
            )
            for number, contents in enumerate((("first", "second"), ("third",)), start=1):
                receipt = await receipt_repo.create(
                    receipt_id=f"receipt-{number}",
                    idempotency_key=f"key-{number}",
                    uid="input-user",
                    app_id=None,
                    thread_id="input-thread",
                    event_type="message",
                    intent_hash=f"hash-{number}",
                    input_id=input_item.id,
                )
                messages = [
                    Message(conversation_id=conversation_id, role="user", content=content, delivery_status="queued")
                    for content in contents
                ]
                db.add_all(messages)
                await db.flush()
                await input_repo.add_messages(
                    input_id=input_item.id, receipt_id=receipt.id, message_ids=[message.id for message in messages]
                )
            second_input = await input_repo.create(
                input_id="input-two",
                thread_id="input-thread",
                uid="input-user",
                app_id=None,
                agent_slug="main",
                kind="follow_up",
                input_payload={},
            )
            second_receipt = await receipt_repo.create(
                receipt_id="receipt-three",
                idempotency_key="key-three",
                uid="input-user",
                app_id=None,
                thread_id="input-thread",
                event_type="message",
                intent_hash="hash-three",
                input_id=second_input.id,
            )
            second_message = Message(
                conversation_id=conversation_id, role="user", content="next input", delivery_status="queued"
            )
            db.add(second_message)
            await db.flush()
            await input_repo.add_messages(
                input_id=second_input.id, receipt_id=second_receipt.id, message_ids=[second_message.id]
            )
            await db.commit()

        async with sessions() as db:
            input_repo = AgentInputRepository(db)
            assert await db.scalar(select(AgentTurn.id)) is None
            head = await input_repo.get_queue_head(thread_id="input-thread", uid="input-user", app_id=None)
            assert head is not None and head.id == "input-one" and head.turn_id is None
            cutoff = await input_repo.get_latest_receive_seq(head.id)
            assert [message.content for message in await input_repo.list_messages(head.id)] == [
                "first",
                "second",
                "third",
            ]
            turn = await AgentTurnRepository(db).create(
                turn_id="turn-one", thread_id="input-thread", uid="input-user", app_id=None
            )
            run = await AgentRunRepository(db).create_run(
                run_id="run-one",
                conversation_thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                input_id=head.id,
                input_payload={},
                conversation_id=conversation_id,
            )
            await AgentTurnRepository(db).set_current(turn, run_id=run.id)
            await input_repo.consume(input_id=head.id, turn_id=turn.id, run_id=run.id, cutoff_seq=cutoff)
            await db.commit()

        async with sessions() as db:
            input_item = await db.get(AgentInput, "input-one")
            assert (input_item.status, input_item.turn_id, input_item.consumed_run_id, input_item.cutoff_seq) == (
                "consumed",
                "turn-one",
                "run-one",
                cutoff,
            )
            messages = await AgentInputRepository(db).list_messages("input-one", through_seq=cutoff)
            assert [(message.content, message.turn_id, message.delivery_status) for message in messages] == [
                ("first", "turn-one", "dispatched"),
                ("second", "turn-one", "dispatched"),
                ("third", "turn-one", "dispatched"),
            ]
            grouped = await AgentInputRepository(db).list_messages_for_inputs(["input-two", "input-one", "missing"])
            assert {input_id: [message.content for message in items] for input_id, items in grouped.items()} == {
                "input-two": ["next input"],
                "input-one": ["first", "second", "third"],
                "missing": [],
            }
            with pytest.raises(ValueError, match="已领取"):
                await AgentInputRepository(db).consume(
                    input_id="input-one", turn_id="turn-one", run_id="run-one", cutoff_seq=cutoff
                )
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_product_key_active_turn_and_steer_uniqueness_are_enforced() -> None:
    """NULL APP 幂等、活跃 Turn 与待消费 steer 的重复行均由 PostgreSQL 拒绝。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            await AgentInputReceiptRepository(db).create(
                receipt_id="receipt-product",
                idempotency_key="same-key",
                uid="input-user",
                app_id=None,
                thread_id="input-thread",
                event_type="message",
                intent_hash="one",
            )
            await AgentTurnRepository(db).create(
                turn_id="turn-active", thread_id="input-thread", uid="input-user", app_id=None
            )
            await AgentInputRepository(db).create(
                input_id="steer-one",
                thread_id="input-thread",
                uid="input-user",
                app_id=None,
                agent_slug="main",
                kind="steer",
                turn_id="turn-active",
            )
            await db.commit()

        async with sessions() as db:
            with pytest.raises(IntegrityError):
                await AgentInputReceiptRepository(db).create(
                    receipt_id="receipt-product-reuse",
                    idempotency_key="same-key",
                    uid="input-user",
                    app_id=None,
                    thread_id="input-thread",
                    event_type="cancel",
                    intent_hash="different",
                )
            await db.rollback()

            with pytest.raises(IntegrityError):
                await AgentTurnRepository(db).create(
                    turn_id="turn-overlap", thread_id="input-thread", uid="input-user", app_id=None
                )
            await db.rollback()

            with pytest.raises(IntegrityError):
                await AgentInputRepository(db).create(
                    input_id="steer-overlap",
                    thread_id="input-thread",
                    uid="input-user",
                    app_id=None,
                    agent_slug="main",
                    kind="steer",
                    turn_id="turn-active",
                )
            await db.rollback()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO conversations (thread_id, uid, agent_id, project_id, is_pinned, status) "
                    "VALUES ('other-thread', 'input-user', 'main', 'input-project', false, 'active')"
                )
            )
        async with sessions() as db:
            turn = await AgentTurnRepository(db).create(
                turn_id="other-turn", thread_id="other-thread", uid="input-user", app_id=None
            )
            await AgentRunRepository(db).create_run(
                run_id="other-run",
                conversation_thread_id="other-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                input_payload={},
            )
            await db.commit()

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE agent_turns SET current_run_id = 'other-run' WHERE id = 'turn-active'")
                )
                await connection.execute(text("SET CONSTRAINTS fk_agent_turns_current_run IMMEDIATE"))
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_run_execution_sequence_orders_segments_across_a_turn() -> None:
    """持久执行序号可在断线后按 Thread 找回后续 Run。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            conversation_id = await db.scalar(select(text("id")).select_from(text("conversations")))
            turn = await AgentTurnRepository(db).create(
                turn_id="cursor-turn", thread_id="input-thread", uid="input-user", app_id=None
            )
            runs = AgentRunRepository(db)
            first = await runs.create_run(
                run_id="cursor-first",
                conversation_thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                input_payload={},
            )
            first_seq = first.execution_seq
            assert first_seq is not None
            first.status = "yielded"
            await db.flush()
            second = await runs.create_run(
                run_id="cursor-second",
                conversation_thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                resume_from_run_id=first.id,
                input_payload={},
            )
            assert second.execution_seq is not None and second.execution_seq > first_seq
            db.add_all(
                [
                    Message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content="first",
                        message_type="model_audit",
                        turn_id=turn.id,
                        run_id=first.id,
                        operation_id="same-model-call",
                        execution_status="completed",
                        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    ),
                    Message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content="",
                        message_type="model_audit",
                        turn_id=turn.id,
                        run_id=second.id,
                        operation_id="same-model-call",
                        execution_status="abandoned",
                    ),
                ]
            )
            await db.flush()
            audits = await AgentTurnRepository(db).list_model_usage_audits(turn.id)
            assert [(audit.run_id, audit.execution_status) for audit in audits] == [
                (first.id, "completed"),
                (second.id, "abandoned"),
            ]
            await db.commit()

        async with sessions() as db:
            subsequent = await AgentRunRepository(db).list_top_level_runs_after_sequence(
                thread_id="input-thread", uid="input-user", app_id=None, after_sequence=first_seq
            )
            assert [run.id for run in subsequent] == ["cursor-second"]
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_turn_usage_counts_parent_and_child_published_model_output_with_exact_owner() -> None:
    """父子 Run 的模型调用同轮计数，错 Run 或错 Turn 的文本不得混入。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            parent_conversation = await db.scalar(select(Conversation).where(Conversation.thread_id == "input-thread"))
            turn = await AgentTurnRepository(db).create(
                turn_id="usage-turn", thread_id="input-thread", uid="input-user", app_id=None
            )
            runs = AgentRunRepository(db)
            parent_run = await runs.create_run(
                run_id="usage-parent",
                conversation_thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                conversation_id=parent_conversation.id,
                input_payload={},
            )
            child_conversation = Conversation(
                thread_id="usage-child-thread",
                project_id="input-project",
                uid="input-user",
                agent_id="helper",
                status="subagent",
            )
            db.add(child_conversation)
            await db.flush()
            relation = SubagentThread(
                uid="input-user",
                parent_conversation_id=parent_conversation.id,
                child_conversation_id=child_conversation.id,
                child_thread_id=child_conversation.thread_id,
                subagent_slug="helper",
                created_by_run_id=parent_run.id,
            )
            db.add(relation)
            await db.flush()
            child_run = await runs.create_run(
                run_id="usage-child",
                conversation_thread_id=child_conversation.thread_id,
                runtime_scope_id=parent_conversation.thread_id,
                agent_slug="helper",
                uid="input-user",
                turn_id=turn.id,
                conversation_id=child_conversation.id,
                run_type="subagent",
                created_by_run_id=parent_run.id,
                subagent_thread_relation_id=relation.id,
                input_payload={},
            )
            parent_run.status = "completed"
            child_run.status = "completed"
            await db.flush()
            other_turn = AgentTurn(
                id="other-turn", conversation_thread_id="input-thread", uid="input-user", status="completed"
            )
            db.add(other_turn)
            await db.flush()
            other_run = await runs.create_run(
                run_id="other-run",
                conversation_thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=other_turn.id,
                conversation_id=parent_conversation.id,
                input_payload={},
            )
            parent_audit = Message(
                conversation_id=parent_conversation.id,
                run_id=parent_run.id,
                turn_id=turn.id,
                role="assistant",
                content="delegate",
                message_type="model_audit",
                operation_id="parent-model",
                execution_status="completed",
                usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            )
            child_audit = Message(
                conversation_id=child_conversation.id,
                run_id=child_run.id,
                turn_id=turn.id,
                role="assistant",
                content="tool call",
                message_type="model_audit",
                operation_id="child-tool-model",
                execution_status="completed",
                usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            )
            wrong_run_text = Message(
                conversation_id=child_conversation.id,
                run_id=child_run.id,
                turn_id=turn.id,
                role="assistant",
                content="wrong run pointer",
                message_type="text",
                operation_id="wrong-run-model",
                execution_status="completed",
                usage={"input_tokens": 900, "output_tokens": 900, "total_tokens": 1800},
            )
            wrong_turn_text = Message(
                conversation_id=parent_conversation.id,
                run_id=other_run.id,
                turn_id=turn.id,
                role="assistant",
                content="wrong turn pointer",
                message_type="text",
                operation_id="wrong-turn-model",
                execution_status="completed",
                usage={"input_tokens": 900, "output_tokens": 900, "total_tokens": 1800},
            )
            child_final = Message(
                conversation_id=child_conversation.id,
                run_id=child_run.id,
                turn_id=turn.id,
                role="assistant",
                content="child result",
                message_type="text",
                operation_id="child-final-model",
                execution_status="completed",
                usage={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
            )
            db.add_all([parent_audit, child_audit, wrong_run_text, wrong_turn_text, child_final])
            await db.flush()
            parent_run.output_message_id = wrong_run_text.id
            other_run.output_message_id = wrong_turn_text.id
            child_run.output_message_id = child_final.id
            await db.commit()

        async with sessions() as db:
            audits = await AgentTurnRepository(db).list_model_usage_audits(turn.id)
            assert [message.operation_id for message in audits] == [
                "parent-model",
                "child-tool-model",
                "child-final-model",
            ]
            assert {
                key: sum(message.usage[key] for message in audits)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            } == {"input_tokens": 9, "output_tokens": 6, "total_tokens": 15}
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_input_api_key_origin_survives_same_version_migration_and_fifo_claim() -> None:
    """同版本补列后，Key 与 JWT Input 的首次来源按 FIFO 固定到各自 Run。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE agent_inputs DROP COLUMN api_key_id"))
        manager = object.__new__(PostgresManager)
        PostgresManager.__init__(manager)
        manager.async_engine = engine
        manager._initialized = True
        await manager.ensure_agent_input_api_key_id()
        await manager.ensure_agent_input_api_key_id()

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            conversation = await db.scalar(select(Conversation).where(Conversation.thread_id == "input-thread"))
            input_repo = AgentInputRepository(db)
            receipt_repo = AgentInputReceiptRepository(db)
            for input_id, api_key_id in (("key-input", 42), ("jwt-input", None)):
                item = await input_repo.create(
                    input_id=input_id,
                    thread_id=conversation.thread_id,
                    uid=conversation.uid,
                    app_id=None,
                    api_key_id=api_key_id,
                    agent_slug="main",
                    kind="follow_up",
                )
                receipt = await receipt_repo.create(
                    receipt_id=f"{input_id}-receipt",
                    idempotency_key=f"{input_id}-key",
                    uid=conversation.uid,
                    app_id=None,
                    thread_id=conversation.thread_id,
                    event_type="message",
                    intent_hash=f"{input_id}-hash",
                    input_id=item.id,
                )
                message = Message(conversation_id=conversation.id, role="user", content=input_id)
                db.add(message)
                await db.flush()
                await input_repo.add_messages(input_id=item.id, receipt_id=receipt.id, message_ids=[message.id])
            await db.commit()

        binding = WorkdirBinding(
            conversation_id=conversation.id,
            thread_id=conversation.thread_id,
            uid=conversation.uid,
            project_id="input-project",
            workdir_path="projects/input-project",
            directory_mode="managed",
        )
        async with sessions() as db:
            conversation = await db.scalar(select(Conversation).where(Conversation.thread_id == "input-thread"))
            first_dispatch = await claim_follow_up(db=db, conversation=conversation, binding=binding)
            assert first_dispatch is not None
            first_run = await db.get(AgentRun, first_dispatch.run_id)
            first_run.status = "completed"
            first_turn = await db.get(AgentTurn, first_run.turn_id)
            await AgentTurnRepository(db).set_terminal(first_turn, status="completed", result_run_id=first_run.id)
            second_dispatch = await claim_follow_up(db=db, conversation=conversation, binding=binding)
            assert second_dispatch is not None
            await db.commit()

        async with sessions() as db:
            key_input = await db.get(AgentInput, "key-input")
            jwt_input = await db.get(AgentInput, "jwt-input")
            first_run = await db.get(AgentRun, first_dispatch.run_id)
            second_run = await db.get(AgentRun, second_dispatch.run_id)
            assert (key_input.api_key_id, first_run.input_id, first_run.api_key_id) == (42, key_input.id, 42)
            assert (jwt_input.api_key_id, second_run.input_id, second_run.api_key_id) == (None, jwt_input.id, None)
            assert key_input.status == jwt_input.status == "consumed"
    finally:
        await _drop_schema(schema, admin_engine, engine)
