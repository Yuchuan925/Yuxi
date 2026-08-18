from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.thread_output_repository import OutputRevisionConflictError, ThreadOutputRepository
from yuxi.services.thread_output_service import get_current_output_snapshot
from yuxi.storage.postgres.models_business import Base, Conversation, ThreadOutputRevision

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


async def _conversation(session) -> Conversation:
    conversation = Conversation(thread_id="thread-1", uid="user-1", agent_id="main", status="active")
    session.add(conversation)
    await session.flush()
    return conversation


def _file(path: str, digest: str) -> dict:
    return {"path": path, "size": 1, "sha256": digest * 64, "content_type": "text/plain"}


async def test_publish_rejects_different_changes_to_same_path(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    first = await repository.create_staging(
        revision_id="revision-1", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.set_files(first.id, [_file("/home/gem/user-data/outputs/first.txt", "a")])
    await repository.publish(first.id)
    await session.commit()

    second = await repository.create_staging(
        revision_id="revision-2", conversation=conversation, run_id=None, base_revision_id=first.id
    )
    await repository.create_staging(
        revision_id="revision-stale", conversation=conversation, run_id=None, base_revision_id=first.id
    )
    await repository.set_files(second.id, [_file("/home/gem/user-data/outputs/first.txt", "b")])
    await repository.set_files("revision-stale", [_file("/home/gem/user-data/outputs/first.txt", "c")])
    await session.commit()
    await repository.publish(second.id)
    await session.commit()
    assert (await session.get(Conversation, conversation.id)).current_output_revision_id == "revision-2"
    with pytest.raises(OutputRevisionConflictError, match="outputs path"):
        await repository.publish("revision-stale")
    await session.rollback()
    assert (await session.get(ThreadOutputRevision, "revision-stale")).status == "staging"


async def test_publish_merges_parent_snapshot_with_new_child_path(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    base_file = _file("/home/gem/user-data/outputs/base.txt", "a")
    child_file = _file("/home/gem/user-data/outputs/child.txt", "b")
    first = await repository.create_staging(
        revision_id="revision-base", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.set_files(first.id, [base_file])
    await repository.publish(first.id)
    await session.commit()

    child = await repository.create_staging(
        revision_id="revision-child", conversation=conversation, run_id=None, base_revision_id=first.id
    )
    parent = await repository.create_staging(
        revision_id="revision-parent", conversation=conversation, run_id=None, base_revision_id=first.id
    )
    await repository.set_files(child.id, [base_file, child_file])
    await repository.set_files(parent.id, [base_file])
    await session.commit()

    await repository.publish(child.id)
    await session.commit()
    published_parent = await repository.publish(parent.id)
    await session.commit()

    assert published_parent.files == [base_file, child_file]
    assert (await session.get(Conversation, conversation.id)).current_output_revision_id == parent.id


async def test_publish_merges_delete_with_unchanged_stale_snapshot(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    kept = _file("/home/gem/user-data/outputs/kept.txt", "a")
    deleted = _file("/home/gem/user-data/outputs/deleted.txt", "b")
    base = await repository.create_staging(
        revision_id="revision-base", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.set_files(base.id, [kept, deleted])
    await repository.publish(base.id)
    await session.commit()

    deleting = await repository.create_staging(
        revision_id="revision-delete", conversation=conversation, run_id=None, base_revision_id=base.id
    )
    stale = await repository.create_staging(
        revision_id="revision-stale", conversation=conversation, run_id=None, base_revision_id=base.id
    )
    await repository.set_files(deleting.id, [kept])
    await repository.set_files(stale.id, [kept, deleted])
    await session.commit()

    await repository.publish(deleting.id)
    await session.commit()
    published_stale = await repository.publish(stale.id)
    await session.commit()

    assert published_stale.files == [kept]


async def test_publish_treats_same_bytes_with_different_mime_as_same_content(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    base_file = _file("/home/gem/user-data/outputs/report.txt", "a")
    base = await repository.create_staging(
        revision_id="revision-base", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.set_files(base.id, [base_file])
    await repository.publish(base.id)
    await session.commit()

    first = await repository.create_staging(
        revision_id="revision-first", conversation=conversation, run_id=None, base_revision_id=base.id
    )
    second = await repository.create_staging(
        revision_id="revision-second", conversation=conversation, run_id=None, base_revision_id=base.id
    )
    first_file = {**_file(base_file["path"], "b"), "content_type": "text/plain"}
    second_file = {**first_file, "content_type": "application/octet-stream"}
    await repository.set_files(first.id, [first_file])
    await repository.set_files(second.id, [second_file])
    await session.commit()

    await repository.publish(first.id)
    await session.commit()
    published_second = await repository.publish(second.id)
    await session.commit()

    assert published_second.files == [first_file]


async def test_unknown_revision_never_becomes_current(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    revision = await repository.create_staging(
        revision_id="revision-unknown", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.mark_status(revision.id, "unknown", "commit response lost")
    await session.commit()

    assert (await session.get(ThreadOutputRevision, revision.id)).status == "unknown"
    assert (await session.get(Conversation, conversation.id)).current_output_revision_id is None
    assert await repository.get_current(conversation) is None


async def test_checkpoint_is_private_and_child_publish_applies_only_child_delta(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    public_file = _file("/home/gem/user-data/outputs/public.txt", "a")
    parent_draft = _file("/home/gem/user-data/outputs/parent-draft.txt", "b")
    child_file = _file("/home/gem/user-data/outputs/child.txt", "c")

    public = await repository.create_staging(
        revision_id="revision-public", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.set_files(public.id, [public_file])
    await repository.publish(public.id)
    checkpoint = await repository.create_staging(
        revision_id="revision-checkpoint",
        conversation=conversation,
        run_id="parent-run",
        base_revision_id=public.id,
    )
    await repository.set_files(checkpoint.id, [public_file, parent_draft])
    await repository.checkpoint(checkpoint.id)
    await session.commit()

    assert conversation.current_output_revision_id == public.id
    assert (await repository.get_current(conversation)).files == [public_file]

    child = await repository.create_staging(
        revision_id="revision-child",
        conversation=conversation,
        run_id="child-run",
        base_revision_id=checkpoint.id,
    )
    await repository.set_files(child.id, [public_file, parent_draft, child_file])
    published = await repository.publish(child.id)
    await session.commit()

    assert published.files == [child_file, public_file]
    assert conversation.current_output_revision_id == published.id
    assert published.id != child.id
    assert (await session.get(ThreadOutputRevision, child.id)).status == "checkpoint"
    assert (await session.get(ThreadOutputRevision, child.id)).files == [public_file, parent_draft, child_file]
    await repository.mark_status(child.id, "unknown", "commit response lost")
    assert (await session.get(ThreadOutputRevision, child.id)).status == "checkpoint"


async def test_checkpoint_child_conflicts_with_concurrent_public_change(session):
    conversation = await _conversation(session)
    repository = ThreadOutputRepository(session)
    path = "/home/gem/user-data/outputs/report.txt"
    original = _file(path, "a")
    parent_draft = _file(path, "b")
    child_change = _file(path, "c")
    concurrent_change = _file(path, "d")

    public = await repository.create_staging(
        revision_id="revision-public", conversation=conversation, run_id=None, base_revision_id=None
    )
    await repository.set_files(public.id, [original])
    await repository.publish(public.id)
    checkpoint = await repository.create_staging(
        revision_id="revision-checkpoint", conversation=conversation, run_id=None, base_revision_id=public.id
    )
    await repository.set_files(checkpoint.id, [parent_draft])
    await repository.checkpoint(checkpoint.id)
    child = await repository.create_staging(
        revision_id="revision-child", conversation=conversation, run_id=None, base_revision_id=checkpoint.id
    )
    await repository.set_files(child.id, [child_change])
    concurrent = await repository.create_staging(
        revision_id="revision-concurrent", conversation=conversation, run_id=None, base_revision_id=public.id
    )
    await repository.set_files(concurrent.id, [concurrent_change])
    await repository.publish(concurrent.id)
    await session.commit()

    with pytest.raises(OutputRevisionConflictError, match="outputs path"):
        await repository.publish(child.id)


async def test_current_pointer_to_unavailable_revision_fails_closed(session):
    conversation = await _conversation(session)
    conversation.current_output_revision_id = "missing-revision"
    await session.commit()

    with pytest.raises(ValueError, match="current revision 不可用"):
        await get_current_output_snapshot(conversation=conversation, db=session)
