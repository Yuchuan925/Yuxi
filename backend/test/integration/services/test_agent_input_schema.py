"""新 Agent 输入模型在真实 PostgreSQL 上的投递与约束证据。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.infrastructure.postgres.manager import PostgresManager
from yuxi.migrations.schema import create_business_tables
from yuxi.modules.agents.models.inputs import AgentInput
from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.sessions import Session
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.repositories.input import AgentInputRepository
from yuxi.modules.agents.repositories.input_receipt import AgentInputReceiptRepository
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository

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
    await create_business_tables(manager)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (username, uid, password_hash, role, login_failed_count, is_deleted) VALUES ('input-user', 'input-user', 'hash', 'user', 0, 0)"
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
                "INSERT INTO sessions "
                "(thread_id, tree_root_thread_id, uid, agent_id, project_id, is_pinned, status, config_snapshot) "
                "VALUES ('input-thread', 'input-thread', 'input-user', 'main', 'input-project', false, 'active', "
                '\'{"model":"test:chat","tool_approval_mode":"default"}\')'
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
            session_record_id = await db.scalar(select(text("id")).select_from(text("sessions")))
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
                    Message(session_record_id=session_record_id, role="user", content=content, delivery_status="queued")
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
            second_message = Message(session_record_id=session_record_id, role="user", content="next input", delivery_status="queued")
            db.add(second_message)
            await db.flush()
            await input_repo.add_messages(input_id=second_input.id, receipt_id=second_receipt.id, message_ids=[second_message.id])
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
            turn = await AgentTurnRepository(db).create(turn_id="turn-one", thread_id="input-thread", uid="input-user", app_id=None)
            run = await AgentRunRepository(db).create_run(
                run_id="run-one",
                thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                input_id=head.id,
                input_payload={},
                session_record_id=session_record_id,
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
                await AgentInputRepository(db).consume(input_id="input-one", turn_id="turn-one", run_id="run-one", cutoff_seq=cutoff)
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
            await AgentTurnRepository(db).create(turn_id="turn-active", thread_id="input-thread", uid="input-user", app_id=None)
            await AgentInputRepository(db).create(
                input_id="steer-one",
                thread_id="input-thread",
                uid="input-user",
                app_id=None,
                agent_slug="main",
                kind="steer",
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
                await AgentTurnRepository(db).create(turn_id="turn-overlap", thread_id="input-thread", uid="input-user", app_id=None)
            await db.rollback()

            with pytest.raises(IntegrityError):
                await AgentInputRepository(db).create(
                    input_id="steer-overlap",
                    thread_id="input-thread",
                    uid="input-user",
                    app_id=None,
                    agent_slug="main",
                    kind="steer",
                )
            await db.rollback()

            with pytest.raises(IntegrityError, match="ck_agent_inputs_delivery"):
                await db.execute(text("UPDATE agent_inputs SET turn_id = 'turn-active' WHERE id = 'steer-one'"))
            await db.rollback()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sessions "
                    "(thread_id, tree_root_thread_id, uid, agent_id, project_id, is_pinned, status, config_snapshot) "
                    "VALUES ('other-thread', 'other-thread', 'input-user', 'main', 'input-project', false, 'active', "
                    '\'{"model":"test:chat","tool_approval_mode":"default"}\')'
                )
            )
        async with sessions() as db:
            turn = await AgentTurnRepository(db).create(turn_id="other-turn", thread_id="other-thread", uid="input-user", app_id=None)
            await AgentRunRepository(db).create_run(
                run_id="other-run",
                thread_id="other-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                input_payload={},
            )
            await db.commit()

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE agent_turns SET current_run_id = 'other-run' WHERE id = 'turn-active'"))
                await connection.execute(text("SET CONSTRAINTS fk_agent_turns_current_run IMMEDIATE"))
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_run_execution_sequence_orders_segments_across_a_turn() -> None:
    """持久执行序号可在断线后按 Thread 找回后续 Run。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            session_record_id = await db.scalar(select(text("id")).select_from(text("sessions")))
            turn = await AgentTurnRepository(db).create(turn_id="cursor-turn", thread_id="input-thread", uid="input-user", app_id=None)
            runs = AgentRunRepository(db)
            first = await runs.create_run(
                run_id="cursor-first",
                thread_id="input-thread",
                session_record_id=session_record_id,
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
                thread_id="input-thread",
                session_record_id=session_record_id,
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
                        session_record_id=session_record_id,
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
                        session_record_id=session_record_id,
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


async def test_parent_and_child_usage_stays_in_its_own_turn() -> None:
    """父子 Turn 分别计数，错 Run 或错 Turn 的文本不得混入。"""
    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            parent_session = await db.scalar(select(Session).where(Session.thread_id == "input-thread"))
            turn = await AgentTurnRepository(db).create(turn_id="usage-turn", thread_id="input-thread", uid="input-user", app_id=None)
            runs = AgentRunRepository(db)
            parent_run = await runs.create_run(
                run_id="usage-parent",
                thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=turn.id,
                session_record_id=parent_session.id,
                input_payload={},
            )
            child_session = Session(
                thread_id="usage-child-thread",
                project_id="input-project",
                uid="input-user",
                agent_id="helper",
                tree_root_thread_id=parent_session.thread_id,
                parent_thread_id=parent_session.thread_id,
                cooperation_name="helper",
                cooperation_path="/root/helper",
                status="active",
            )
            db.add(child_session)
            await db.flush()
            child_turn = await AgentTurnRepository(db).create(
                turn_id="child-usage-turn", thread_id=child_session.thread_id, uid="input-user", app_id=None
            )
            child_run = await runs.create_run(
                run_id="usage-child",
                thread_id=child_session.thread_id,
                runtime_scope_id=parent_session.thread_id,
                agent_slug="helper",
                uid="input-user",
                turn_id=child_turn.id,
                session_record_id=child_session.id,
                run_type="chat",
                created_by_run_id=parent_run.id,
                input_payload={},
            )
            parent_run.status = "completed"
            child_run.status = "completed"
            await db.flush()
            other_turn = AgentTurn(id="other-turn", thread_id="input-thread", uid="input-user", status="completed")
            db.add(other_turn)
            await db.flush()
            other_run = await runs.create_run(
                run_id="other-run",
                thread_id="input-thread",
                agent_slug="main",
                uid="input-user",
                turn_id=other_turn.id,
                session_record_id=parent_session.id,
                input_payload={},
            )
            parent_audit = Message(
                session_record_id=parent_session.id,
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
                session_record_id=child_session.id,
                run_id=child_run.id,
                turn_id=child_turn.id,
                role="assistant",
                content="tool call",
                message_type="model_audit",
                operation_id="child-tool-model",
                execution_status="completed",
                usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            )
            wrong_run_text = Message(
                session_record_id=child_session.id,
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
                session_record_id=parent_session.id,
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
                session_record_id=child_session.id,
                run_id=child_run.id,
                turn_id=child_turn.id,
                role="assistant",
                content="child result",
                message_type="text",
                operation_id="child-final-model",
                execution_status="completed",
                usage={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
            )
            other_final = Message(
                session_record_id=parent_session.id,
                run_id=other_run.id,
                turn_id=other_turn.id,
                role="assistant",
                content="other turn result",
                message_type="text",
                operation_id="other-final-model",
                execution_status="completed",
                usage={"input_tokens": 900, "output_tokens": 900, "total_tokens": 1800},
            )
            db.add_all([parent_audit, child_audit, child_final, other_final])
            await db.flush()
            for invalid_message in (wrong_run_text, wrong_turn_text):
                with pytest.raises(IntegrityError, match="fk_messages_run_turn_session"):
                    async with db.begin_nested():
                        db.add(invalid_message)
                        await db.flush()
                        await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            for run, foreign_message in ((parent_run, child_final), (other_run, parent_audit)):
                with pytest.raises(IntegrityError, match="fk_agent_runs_output_message_scope"):
                    async with db.begin_nested():
                        run.output_message_id = foreign_message.id
                        await db.flush()
                        await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                await db.refresh(run)
            other_run.output_message_id = other_final.id
            child_run.output_message_id = child_final.id
            await db.commit()

        async with sessions() as db:
            audits = await AgentTurnRepository(db).list_model_usage_audits(turn.id)
            assert [message.operation_id for message in audits] == ["parent-model"]
            assert {key: sum(message.usage[key] for message in audits) for key in ("input_tokens", "output_tokens", "total_tokens")} == {
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
            }
            child_audits = await AgentTurnRepository(db).list_model_usage_audits(child_turn.id)
            assert [message.operation_id for message in child_audits] == ["child-tool-model", "child-final-model"]
            assert sum(message.usage["total_tokens"] for message in child_audits) == 10
            other_audits = await AgentTurnRepository(db).list_model_usage_audits(other_turn.id)
            assert [(message.operation_id, message.usage["total_tokens"]) for message in other_audits] == [("other-final-model", 1800)]
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_reused_tool_call_id_never_reuses_another_message_declaration():
    """供应商重复 call_id 时，每条声明仍拥有独立 ToolCall 投影。"""
    from yuxi.modules.agents.repositories.sessions import SessionRepository

    schema, admin_engine, engine = await _create_schema()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            agent_session = await db.scalar(select(Session).where(Session.thread_id == "input-thread"))
            messages = [Message(session_record_id=agent_session.id, role="assistant", content="", extra_metadata={}) for _ in range(2)]
            db.add_all(messages)
            await db.flush()
            repository = SessionRepository(db)
            first, second = [
                await repository.add_tool_call(
                    message_id=message.id,
                    tool_name="execute",
                    tool_input={"command": str(message.id)},
                    langgraph_tool_call_id="provider-reused-id",
                    commit=False,
                )
                for message in messages
            ]
            assert first.id != second.id and second.message_id == messages[1].id
            repeated = await repository.add_tool_call(
                message_id=messages[0].id,
                tool_name="execute",
                tool_input={"command": str(messages[0].id)},
                langgraph_tool_call_id="provider-reused-id",
                commit=False,
            )
            assert repeated.id == first.id
            await db.commit()
    finally:
        await _drop_schema(schema, admin_engine, engine)


async def test_attachment_schema_upgrade_is_idempotent_and_preserves_inputs():
    """显式 v5 升级只补附件表，重跑保留输入并拒绝无效准备状态。"""
    from datetime import timedelta
    from yuxi.modules.agents.models.attachments import AgentAttachment
    from yuxi.migrations.schema import add_attachment_table, create_schema_version_table, record_schema_version
    from yuxi.shared.datetime import utc_now

    schema, admin, engine = await _create_schema()
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(
                AgentInput(
                    id="upgrade-input",
                    thread_id="input-thread",
                    uid="input-user",
                    agent_slug="main",
                    kind="follow_up",
                    status="pending",
                    input_payload={"context_snapshot": {"model": "test:chat", "tool_approval_mode": "default"}},
                )
            )
            await db.commit()
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: AgentAttachment.__table__.drop(sync))
        await create_schema_version_table(manager)
        await record_schema_version(manager, "business", 5)
        await add_attachment_table(manager)
        await add_attachment_table(manager)
        async with factory() as db:
            assert (await db.get(AgentInput, "upgrade-input")).status == "pending"
            assert await db.scalar(text("SELECT version FROM yuxi_schema_migrations WHERE domain='business'")) == 6
            with pytest.raises(IntegrityError, match="ck_agent_attachments_preparation"):
                async with db.begin_nested():
                    db.add(
                        AgentAttachment(
                            id="invalid-ready",
                            uid="input-user",
                            filename="file.txt",
                            mime_type="text/plain",
                            size_bytes=1,
                            created_at=utc_now(),
                            expires_at=utc_now() + timedelta(days=1),
                            status="ready",
                            object_name="temporary",
                        )
                    )
                    await db.flush()
            assert await db.get(AgentAttachment, "invalid-ready") is None
    finally:
        await _drop_schema(schema, admin, engine)
