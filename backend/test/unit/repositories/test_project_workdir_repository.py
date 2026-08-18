from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.project_workdir_repository import ProjectWorkdirRepository
from yuxi.storage.postgres.models_business import Base, ProjectWorkdir

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


async def test_top_level_conversation_creates_opaque_default_workdir(session):
    conversation = await ConversationRepository(session).add_conversation(
        uid="oidc:user@example.com",
        agent_id="main",
        thread_id="thread-1",
    )

    workdir = await session.get(ProjectWorkdir, conversation.workdir_id)

    assert workdir is not None
    assert workdir.uid == "oidc:user@example.com"
    assert workdir.storage_key == f"projects/{workdir.id}"
    assert "oidc" not in workdir.storage_key
    assert workdir.materialization_status == "pending"


async def test_same_user_can_explicitly_share_workdir_between_top_level_conversations(session):
    first = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-1",
    )
    second = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-2",
        workdir_id=first.workdir_id,
    )

    assert second.workdir_id == first.workdir_id


async def test_conversation_rejects_cross_user_workdir_binding(session):
    first = await ConversationRepository(session).add_conversation(
        uid="user-1",
        agent_id="main",
        thread_id="thread-1",
    )

    with pytest.raises(ValueError, match="不属于当前用户"):
        await ConversationRepository(session).add_conversation(
            uid="user-2",
            agent_id="main",
            thread_id="thread-2",
            workdir_id=first.workdir_id,
        )


async def test_materialization_status_rejects_unknown_value(session):
    workdir = await ProjectWorkdirRepository(session).create_default(uid="user-1")

    with pytest.raises(ValueError, match="无效"):
        await ProjectWorkdirRepository(session).set_materialization_status(workdir, status="done")

    await ProjectWorkdirRepository(session).set_materialization_status(
        workdir,
        status="error",
        error_message="missing legacy object",
    )
    assert workdir.materialization_status == "error"
    assert workdir.materialization_error == "missing legacy object"
