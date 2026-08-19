from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.storage.postgres.models_business import Base

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def test_top_level_conversation_creates_default_workdir(session, monkeypatch, tmp_path: Path):
    created = tmp_path / "projects" / "opaque-id"
    created.mkdir(parents=True)
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.paths.allocate_default_user_workdir_path",
        lambda: "projects/opaque-id",
    )

    conversation = await ConversationRepository(session).add_conversation(
        uid="oidc:user@example.com",
        agent_id="main",
        thread_id="thread-1",
    )

    assert conversation.workdir_path == "projects/opaque-id"


async def test_same_user_can_share_existing_explicit_workdir(session, monkeypatch, tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    checked = []
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.paths.user_workdir_host_dir",
        lambda uid, path: checked.append((uid, path)) or shared,
    )

    first = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-1",
        workdir_path="shared",
    )
    second = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-2",
        workdir_path="shared",
    )

    assert first.workdir_path == second.workdir_path == "shared"
    assert checked == [("user-1", "shared"), ("user-1", "shared")]


@pytest.mark.parametrize("path", ["../outside", "/tmp/host", "agents/skills", "https://example.com/a"])
async def test_explicit_workdir_rejects_invalid_path(session, path):
    with pytest.raises((ValueError, FileNotFoundError, NotADirectoryError, OSError)):
        await ConversationRepository(session).add_conversation(
            uid="user-1",
            agent_id="main",
            thread_id=f"thread-{abs(hash(path))}",
            workdir_path=path,
        )


async def test_failed_conversation_flush_does_not_create_unbound_workdir(session, monkeypatch, tmp_path: Path):
    created = tmp_path / "projects/opaque-id"
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.paths.allocate_default_user_workdir_path",
        lambda: "projects/opaque-id",
    )
    monkeypatch.setattr(session, "flush", AsyncMock(side_effect=RuntimeError("db failure")))

    with pytest.raises(RuntimeError, match="db failure"):
        await ConversationRepository(session).add_conversation(
            uid="user-1",
            agent_id="main",
            thread_id="thread-failed",
        )

    assert not created.exists()


async def test_failed_commit_does_not_materialize_default_workdir(session, monkeypatch, tmp_path: Path):
    created = tmp_path / "projects/opaque-id"
    materialized: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.paths.allocate_default_user_workdir_path",
        lambda: "projects/opaque-id",
    )
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.paths.ensure_bound_user_workdir",
        lambda uid, path: materialized.append((uid, path)),
    )
    monkeypatch.setattr(session, "commit", AsyncMock(side_effect=RuntimeError("commit failure")))

    with pytest.raises(RuntimeError, match="commit failure"):
        await ConversationRepository(session).create_conversation(
            uid="user-1",
            agent_id="main",
            thread_id="thread-commit-failed",
        )

    assert materialized == []
    assert not created.exists()


async def test_outer_transaction_rollback_leaves_no_default_directory(session, monkeypatch, tmp_path: Path):
    created = tmp_path / "projects/opaque-id"
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.paths.allocate_default_user_workdir_path",
        lambda: "projects/opaque-id",
    )

    await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-outer-rollback",
    )
    await session.rollback()

    assert not created.exists()
