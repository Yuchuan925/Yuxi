"""业务 PostgreSQL 映射。"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from yuxi.infrastructure.postgres.base import JSON_VALUE
from yuxi.infrastructure.postgres.base import BusinessBase as Base
from yuxi.shared.datetime import utc_now


class AgentInput(Base):
    """持久输入只记录排队、消费和取消事实。"""

    __tablename__ = "agent_inputs"

    id = Column(String(64), primary_key=True)
    received_seq = Column(BigInteger, Identity(), nullable=False, unique=True)
    conversation_thread_id = Column(String(64), ForeignKey("conversations.thread_id"), nullable=False, index=True)
    uid = Column(String(64), nullable=False)
    app_id = Column(String(64), nullable=True)
    api_key_id = Column(Integer, nullable=True, comment="首次接收 Input 的 API Key ID 快照")
    agent_slug = Column(String(64), nullable=False)
    kind = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    turn_id = Column(String(64), nullable=True, index=True)
    consumed_run_id = Column(String(64), ForeignKey("agent_runs.id"), nullable=True, unique=True)
    cutoff_seq = Column(BigInteger, nullable=True)
    input_payload = Column(JSON_VALUE, nullable=False, default=dict)
    source = Column(String(32), nullable=False, default="chat")
    channel = Column(String(32), nullable=False, default="web")
    external_id = Column(String(128), nullable=True)
    origin_metadata = Column(JSON_VALUE, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "conversation_thread_id", name="uq_agent_inputs_id_thread"),
        ForeignKeyConstraint(
            ["turn_id", "conversation_thread_id"],
            ["agent_turns.id", "agent_turns.conversation_thread_id"],
            name="fk_agent_inputs_turn_thread",
        ),
        Index(
            "ix_agent_inputs_pending_head",
            "conversation_thread_id",
            (kind == "steer").self_group().desc(),
            "received_seq",
            postgresql_where=status == "pending",
        ).ddl_if(dialect="postgresql"),
        Index(
            "uq_agent_inputs_pending_steer",
            "conversation_thread_id",
            unique=True,
            postgresql_where=(kind == "steer") & (status == "pending"),
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("kind IN ('follow_up', 'steer')", name="ck_agent_inputs_kind"),
        CheckConstraint(
            "(status = 'pending' AND consumed_run_id IS NULL AND cutoff_seq IS NULL AND consumed_at IS NULL "
            "AND turn_id IS NULL) "
            "OR (status = 'consumed' AND turn_id IS NOT NULL AND consumed_run_id IS NOT NULL "
            "AND cutoff_seq IS NOT NULL AND consumed_at IS NOT NULL) "
            "OR (status = 'cancelled' AND consumed_run_id IS NULL AND cancelled_at IS NOT NULL)",
            name="ck_agent_inputs_delivery",
        ),
        ForeignKeyConstraint(
            ["turn_id", "consumed_run_id"],
            ["agent_runs.turn_id", "agent_runs.id"],
            name="fk_agent_inputs_consumed_run_turn",
            use_alter=True,
        ),
    )


class AgentInputReceipt(Base):
    """独立接收序号和作用域幂等事实。"""

    __tablename__ = "agent_input_receipts"

    id = Column(String(64), primary_key=True)
    receive_seq = Column(BigInteger, Identity(), nullable=False, unique=True)
    idempotency_key = Column(String(128), nullable=False)
    uid = Column(String(64), nullable=False)
    app_id = Column(String(64), nullable=True)
    conversation_thread_id = Column(String(64), ForeignKey("conversations.thread_id"), nullable=False)
    event_type = Column(String(48), nullable=False)
    intent_hash = Column(String(64), nullable=False)
    input_id = Column(String(64), nullable=True)
    turn_id = Column(String(64), nullable=True)
    run_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["input_id", "conversation_thread_id"],
            ["agent_inputs.id", "agent_inputs.conversation_thread_id"],
            name="fk_agent_input_receipts_input_thread",
        ),
        ForeignKeyConstraint(
            ["turn_id", "conversation_thread_id"],
            ["agent_turns.id", "agent_turns.conversation_thread_id"],
            name="fk_agent_input_receipts_turn_thread",
        ),
        ForeignKeyConstraint(
            ["turn_id", "run_id"],
            ["agent_runs.turn_id", "agent_runs.id"],
            name="fk_agent_input_receipts_run_turn",
        ),
        CheckConstraint("run_id IS NULL OR turn_id IS NOT NULL", name="ck_agent_input_receipts_run_turn"),
        UniqueConstraint("id", "input_id", name="uq_agent_input_receipts_id_input"),
        Index("ix_agent_input_receipts_input_seq", "input_id", receive_seq.desc()),
        Index(
            "uq_agent_input_receipts_scope_key",
            "uid",
            "app_id",
            "conversation_thread_id",
            "idempotency_key",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ).ddl_if(dialect="postgresql"),
    )


class AgentInputMessage(Base):
    """保存每次接收事件中的原始消息及顺序。"""

    __tablename__ = "agent_input_messages"

    id = Column(BigInteger, Identity(), primary_key=True)
    input_id = Column(String(64), nullable=False)
    receipt_id = Column(String(64), nullable=False)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False, unique=True)
    position = Column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["receipt_id", "input_id"],
            ["agent_input_receipts.id", "agent_input_receipts.input_id"],
            name="fk_agent_input_messages_receipt_input",
        ),
        UniqueConstraint("receipt_id", "position", name="uq_agent_input_messages_receipt_position"),
        Index("ix_agent_input_messages_input", "input_id", "receipt_id", "position"),
        CheckConstraint("position >= 0", name="ck_agent_input_messages_position"),
    )
