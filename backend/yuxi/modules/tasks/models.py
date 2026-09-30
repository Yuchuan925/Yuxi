"""业务 PostgreSQL 映射。"""

from typing import Any
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from yuxi.shared.datetime import format_utc_datetime, utc_now_naive

from yuxi.infrastructure.postgres.base import BusinessBase as Base


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("type", "dedupe_key", name="uq_tasks_active_dedupe"),
        Index("ix_tasks_status_lease_expires", "status", "lease_expires_at"),
    )

    id = Column(String(32), primary_key=True)
    name = Column(String(255), nullable=False)
    type = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    progress = Column(Float, nullable=False, default=0.0)
    message = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    cancel_requested = Column(Integer, nullable=False, default=0)
    handler_version = Column(Integer, nullable=False, default=1)
    dedupe_key = Column(String(64), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    worker_id = Column(String(128), nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    timeout_seconds = Column(Float, nullable=False, default=21600.0)
    created_at = Column(DateTime, default=utc_now_naive, index=True)
    updated_at = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": format_utc_datetime(self.created_at),
            "updated_at": format_utc_datetime(self.updated_at),
            "started_at": format_utc_datetime(self.started_at),
            "completed_at": format_utc_datetime(self.completed_at),
            "payload": self.payload or {},
            "result": self.result,
            "error": self.error,
            "cancel_requested": bool(self.cancel_requested),
            "handler_version": int(self.handler_version),
            "dedupe_key": self.dedupe_key,
            "attempt_count": int(self.attempt_count or 0),
            "worker_id": self.worker_id,
            "heartbeat_at": format_utc_datetime(self.heartbeat_at),
            "lease_expires_at": format_utc_datetime(self.lease_expires_at),
            "timeout_seconds": float(self.timeout_seconds or 0),
        }

    def to_summary_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("payload", None)
        data.pop("result", None)
        return data
