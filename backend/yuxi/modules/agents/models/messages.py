"""业务 PostgreSQL 映射。"""

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from yuxi.infrastructure.postgres.base import JSON_VALUE
from yuxi.infrastructure.postgres.base import BusinessBase as Base
from yuxi.shared.datetime import format_utc_datetime, utc_now

MODEL_AUDIT_MESSAGE_TYPE = "model_audit"


TOOL_AUDIT_MESSAGE_TYPE = "tool_audit"


AUDIT_MESSAGE_TYPES = (MODEL_AUDIT_MESSAGE_TYPE, TOOL_AUDIT_MESSAGE_TYPE)


class Message(Base):
    """Message table - 消息表"""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "execution_status IS NULL OR execution_status IN ('running', 'completed', 'failed', 'interrupted', 'abandoned')",
            name="ck_messages_execution_status",
        ),
        Index(
            "uq_messages_run_role_operation_id",
            "run_id",
            "role",
            "operation_id",
            unique=True,
            postgresql_where=text("operation_id IS NOT NULL"),
            sqlite_where=text("operation_id IS NOT NULL"),
        ),
        UniqueConstraint("id", "run_id", "turn_id", "session_record_id", name="uq_messages_id_run_turn_thread"),
        UniqueConstraint("source_input_id", "input_position", name="uq_messages_input_position"),
        UniqueConstraint("id", "source_input_id", name="uq_messages_input_origin"),
        CheckConstraint(
            "(source_input_id IS NULL AND input_position IS NULL AND received_at IS NULL) OR "
            "(source_input_id IS NOT NULL AND input_position IS NOT NULL AND input_position >= 0 AND received_at IS NOT NULL "
            "AND run_id IS NOT NULL AND turn_id IS NOT NULL AND role = 'user')",
            name="ck_messages_input_origin",
        ),
        ForeignKeyConstraint(
            ["source_input_id", "run_id", "turn_id"],
            ["agent_inputs.id", "agent_inputs.consumed_run_id", "agent_inputs.turn_id"],
            name="fk_messages_consumed_input",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("(run_id IS NULL) = (turn_id IS NULL)", name="ck_messages_execution_scope"),
        ForeignKeyConstraint(
            ["run_id", "turn_id", "session_record_id"],
            ["agent_runs.id", "agent_runs.turn_id", "agent_runs.session_record_id"],
            name="fk_messages_run_turn_session",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_messages_run_sequence",
            "run_id",
            "sequence",
            postgresql_where=text("sequence IS NOT NULL"),
            sqlite_where=text("sequence IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    session_record_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True, comment="Session ID")
    role = Column(String(20), nullable=False, comment="Message role: user/assistant/system/tool")
    content = Column(Text, nullable=False, comment="Message content")
    message_type = Column(String(30), default="text", comment="Message type: text/tool_call/tool_result")
    created_at = Column(DateTime(timezone=True), default=utc_now, comment="Creation time")
    source_input_id = Column(String(64), nullable=True, index=True)
    input_position = Column(Integer, nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=True)
    extra_metadata = Column(JSON, nullable=True, comment="Additional metadata (complete message dump)")
    image_content = Column(Text, nullable=True, comment="Base64 encoded image content for multimodal messages")
    run_id = Column(String(64), nullable=True, index=True, comment="Agent run ID")
    turn_id = Column(String(64), nullable=True, index=True)
    delivery_status = Column(String(32), nullable=False, default="complete", comment="Message status")
    operation_id = Column(String(128), nullable=True, comment="同一 Run 内的 Model/Tool 稳定来源键")
    started_at = Column(DateTime(timezone=True), nullable=True, comment="Yuxi 观察到操作开始的 wall-clock 时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="Yuxi 观察到操作结束的 wall-clock 时间")
    duration_ms = Column(BigInteger, nullable=True, comment="本进程 monotonic clock 计算的操作耗时")
    sequence = Column(BigInteger, nullable=True, comment="LangGraph 根 StreamMux 事件顺序")
    execution_status = Column(String(32), nullable=True, comment="Model/Tool 执行状态")
    usage = Column(JSON_VALUE, nullable=True, comment="Provider 返回的单次可靠 usage")

    # Relationships
    agent_session = relationship("Session", back_populates="messages")
    tool_calls = relationship("ToolCall", back_populates="message", cascade="all, delete-orphan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_record_id": self.session_record_id,
            "role": self.role,
            "content": self.content,
            "message_type": self.message_type,
            "created_at": format_utc_datetime(self.created_at),
            "source_input_id": self.source_input_id,
            "input_position": self.input_position,
            "received_at": format_utc_datetime(self.received_at),
            "metadata": self.extra_metadata or {},
            "image_content": self.image_content,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "status": self.delivery_status,
            "operation_id": self.operation_id,
            "started_at": format_utc_datetime(self.started_at),
            "finished_at": format_utc_datetime(self.finished_at),
            "duration_ms": self.duration_ms,
            "sequence": self.sequence,
            "execution_status": self.execution_status,
            "usage": self.usage,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls] if self.tool_calls else [],
        }


class ToolCall(Base):
    """ToolCall table - 工具调用表"""

    __tablename__ = "tool_calls"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Primary key")
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False, index=True, comment="Message ID")
    langgraph_tool_call_id = Column(String(100), nullable=True, index=True, comment="LangGraph tool_call_id")
    tool_name = Column(String(100), nullable=False, comment="Tool name")
    tool_input = Column(JSON, nullable=True, comment="Tool input parameters")
    tool_output = Column(Text, nullable=True, comment="Tool execution result")
    status = Column(String(20), default="pending", comment="Status: pending/success/error")
    error_message = Column(Text, nullable=True, comment="Error message if failed")
    created_at = Column(DateTime(timezone=True), default=utc_now, comment="Creation time")

    # Relationships
    message = relationship("Message", back_populates="tool_calls")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message_id": self.message_id,
            "langgraph_tool_call_id": self.langgraph_tool_call_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input or {},
            "tool_output": self.tool_output,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": format_utc_datetime(self.created_at),
        }
