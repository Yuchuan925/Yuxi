"""为 SQLite 单测提供 PostgreSQL Run 序号的最小替身。"""

from itertools import count

import pytest
from sqlalchemy import event

from yuxi.storage.postgres.models_business import AgentRun


@pytest.fixture(autouse=True)
def sqlite_run_execution_sequence():
    """SQLite 无 sequence，插入测试 Run 时按提交顺序补序号。"""
    sequence = count(1)

    def assign_sequence(_mapper, connection, run: AgentRun) -> None:
        if connection.dialect.name == "sqlite" and run.execution_seq is None:
            run.execution_seq = next(sequence)

    event.listen(AgentRun, "before_insert", assign_sequence)
    try:
        yield
    finally:
        event.remove(AgentRun, "before_insert", assign_sequence)
