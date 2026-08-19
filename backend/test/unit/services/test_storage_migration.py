from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yuxi import storage_migration


class _ScalarSession:
    def __init__(self, values):
        self._values = iter(values)

    async def scalar(self, _statement):
        return next(self._values)


@pytest.mark.asyncio
async def test_storage_migration_owns_legacy_cutover_before_shipping_start(monkeypatch):
    calls: list[object] = []
    session = object()

    @asynccontextmanager
    async def session_context():
        yield session

    manager = SimpleNamespace(
        initialize=lambda: calls.append("initialize"),
        create_business_tables=lambda: _record(calls, "create_business_tables"),
        ensure_business_schema=lambda: _record(calls, "ensure_business_schema"),
        get_async_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )

    async def materialize():
        calls.append("materialize")

    async def migrate_skills(db, *, remove_personal_legacy=False):
        calls.append(("migrate_skills", db, remove_personal_legacy))

    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(storage_migration, "ensure_project_workdir_materialized", materialize)
    monkeypatch.setattr(storage_migration, "migrate_legacy_skill_storage", migrate_skills)
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge_db:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(storage_migration, "mark_legacy_skill_storage_migrated", lambda: calls.append("mark_skills"))
    monkeypatch.setattr(
        storage_migration,
        "_legacy_cutover_pending_before_schema_init",
        lambda: _async_value(False),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)

    await storage_migration.main()

    assert calls == [
        "initialize",
        "create_business_tables",
        "ensure_business_schema",
        "converge_db:False",
        "materialize",
        ("migrate_skills", session, True),
        "mark_skills",
        "close",
    ]


@pytest.mark.asyncio
async def test_storage_migration_closes_database_when_cutover_fails(monkeypatch):
    calls: list[str] = []

    async def fail_materialize():
        raise RuntimeError("cutover failed")

    manager = SimpleNamespace(
        initialize=lambda: None,
        create_business_tables=lambda: _record(calls, "create_business_tables"),
        ensure_business_schema=lambda: _record(calls, "ensure_business_schema"),
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(storage_migration, "ensure_project_workdir_materialized", fail_materialize)
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge_db:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(
        storage_migration,
        "_legacy_cutover_pending_before_schema_init",
        lambda: _async_value(False),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)

    with pytest.raises(RuntimeError, match="cutover failed"):
        await storage_migration.main()

    assert calls == ["create_business_tables", "ensure_business_schema", "converge_db:False", "close"]


@pytest.mark.asyncio
async def test_storage_migration_rejects_legacy_delete_without_quiescence_proof(
    monkeypatch,
    tmp_path,
):
    """存在旧文件事实时，普通 compose up 不得进入破坏性迁移。"""
    calls: list[str] = []
    manager = SimpleNamespace(
        initialize=lambda: None,
        create_business_tables=lambda: _record(calls, "create_business_tables"),
        ensure_business_schema=lambda: _record(calls, "ensure_business_schema"),
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "_legacy_cutover_pending_before_schema_init",
        lambda: _async_value(True),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE", str(tmp_path / "missing-proof"))
    monkeypatch.delenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="migrate-storage.sh"):
        await storage_migration.main()

    assert calls == ["create_business_tables", "ensure_business_schema", "close"]


@pytest.mark.asyncio
async def test_storage_migration_accepts_matching_one_time_quiescence_proof(
    monkeypatch,
    tmp_path,
):
    calls: list[str] = []
    proof = tmp_path / "proof"
    proof.write_text("proof-token\n", encoding="utf-8")

    @asynccontextmanager
    async def session_context():
        yield object()

    manager = SimpleNamespace(
        initialize=lambda: None,
        create_business_tables=lambda: _record(calls, "create_business_tables"),
        ensure_business_schema=lambda: _record(calls, "ensure_business_schema"),
        get_async_session_context=session_context,
        close=lambda: _record(calls, "close"),
    )
    monkeypatch.setattr(storage_migration, "pg_manager", manager)
    monkeypatch.setattr(
        storage_migration,
        "_legacy_cutover_pending_before_schema_init",
        lambda: _async_value(True),
    )
    monkeypatch.setattr(storage_migration, "_legacy_skill_roots_exist", lambda: False)
    monkeypatch.setattr(
        storage_migration,
        "ensure_project_workdir_materialized",
        lambda: _record(calls, "materialize"),
    )
    monkeypatch.setattr(
        storage_migration,
        "_converge_database_state",
        lambda *, fail_nonterminal_runs: _record(calls, f"converge_db:{fail_nonterminal_runs}"),
    )
    monkeypatch.setattr(
        storage_migration,
        "migrate_legacy_skill_storage",
        lambda _db, *, remove_personal_legacy: _record(calls, f"skills:{remove_personal_legacy}"),
    )
    monkeypatch.setattr(storage_migration, "mark_legacy_skill_storage_migrated", lambda: calls.append("mark_skills"))
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE", str(proof))
    monkeypatch.setenv("YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN", "proof-token")

    await storage_migration.main()

    assert calls == [
        "create_business_tables",
        "ensure_business_schema",
        "converge_db:True",
        "materialize",
        "skills:True",
        "mark_skills",
        "close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scalar_values", "expected"),
    [
        ([False], False),
        ([True, False], True),
        ([True, True, "pending"], True),
        ([True, True, "active"], False),
    ],
)
async def test_legacy_cutover_state_is_read_before_new_schema_is_created(
    monkeypatch,
    scalar_values,
    expected,
):
    @asynccontextmanager
    async def session_context():
        yield _ScalarSession(scalar_values)

    monkeypatch.setattr(
        storage_migration,
        "pg_manager",
        SimpleNamespace(get_async_session_context=session_context),
    )
    assert await storage_migration._legacy_cutover_pending_before_schema_init() is expected


def test_completed_skill_cutover_ignores_later_workspace_directory(monkeypatch, tmp_path):
    """完成标记之后，同名普通 workspace 目录不得再次触发破坏性迁移。"""
    skill_data = tmp_path / "skill-sources"
    skill_data.mkdir()
    (skill_data / ".legacy-migration-complete").write_text("1\n", encoding="utf-8")
    later_dir = tmp_path / "user-data/shared/user-1/workspace/agents/skills/notes"
    later_dir.mkdir(parents=True)
    (later_dir / "README.md").write_text("ordinary files", encoding="utf-8")

    monkeypatch.setattr(storage_migration, "get_user_data_dir", lambda: tmp_path / "user-data")
    monkeypatch.setattr(storage_migration, "legacy_skill_storage_migration_completed", lambda: True)

    assert storage_migration._legacy_skill_roots_exist() is False


async def _record(calls: list[object], value: str) -> None:
    calls.append(value)


async def _async_value(value):
    return value
