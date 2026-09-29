"""当前 Schema 初始化入口拒绝旧版本。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from yuxi.migrations import main as schema_bootstrap


@pytest.mark.asyncio
async def test_legacy_schema_version_is_rejected_before_ddl(monkeypatch):
    """旧版本标记必须阻止任何当前 Schema 建表。"""
    manager = SimpleNamespace(initialize=Mock(), close=AsyncMock())

    @asynccontextmanager
    async def locked(_manager):
        yield

    create_tables = AsyncMock()
    monkeypatch.setattr(schema_bootstrap, "pg_manager", manager)
    monkeypatch.setattr(schema_bootstrap, "schema_migration_lock", locked)
    monkeypatch.setattr(
        schema_bootstrap, "get_schema_versions", AsyncMock(return_value={"business": 2, "knowledge": 1})
    )
    monkeypatch.setattr(schema_bootstrap, "create_business_tables", create_tables)

    with pytest.raises(RuntimeError, match="Unsupported Yuxi schema versions"):
        await schema_bootstrap.main()

    create_tables.assert_not_awaited()
    manager.close.assert_awaited_once()
