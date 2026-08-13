from __future__ import annotations

import asyncio
import datetime as dt
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from yuxi.services import workspace_service as svc
from yuxi.storage.filestore import LocalFileStore, thread_output_key, thread_upload_key, user_workspace_key
from yuxi.utils.paths import WORKSPACE_AGENT_CONTEXT_FILES


def _user() -> SimpleNamespace:
    return SimpleNamespace(id="db-id-1", uid="user-1")


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> LocalFileStore:
    filestore = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(svc, "get_file_store", lambda: filestore)
    return filestore


@pytest.mark.asyncio
async def test_workspace_defaults_are_created_without_overwriting(store: LocalFileStore) -> None:
    await store.put(user_workspace_key("user-1", "agents/AGENTS.md"), b"custom")

    await svc.ensure_workspace_defaults("user-1")

    assert (await store.read(user_workspace_key("user-1", "agents/AGENTS.md"))).data == b"custom"
    for filename, content in WORKSPACE_AGENT_CONTEXT_FILES.items():
        stored = await store.read(user_workspace_key("user-1", f"agents/{filename}"))
        assert stored.data == (b"custom" if filename == "AGENTS.md" else content.encode())


@pytest.mark.asyncio
async def test_workspace_crud_and_keep_marker(store: LocalFileStore) -> None:
    user = _user()
    created = await svc.create_workspace_directory(parent_path="/", name="notes", current_user=user)
    uploaded = await svc.upload_workspace_files(
        parent_path="/notes",
        files=[UploadFile(filename="demo.md", file=BytesIO(b"# old"))],
        current_user=user,
    )
    updated = await svc.write_workspace_file_content(path="/notes/demo.md", content="# new", current_user=user)
    listed = await svc.list_workspace_tree(path="/", recursive=True, current_user=user)
    downloaded = await svc.download_workspace_file(path="/notes/demo.md", current_user=user)
    body = b"".join([chunk async for chunk in downloaded.body_iterator])

    assert created["entry"]["path"] == "/notes/"
    assert uploaded["entries"][0]["path"] == "/notes/demo.md"
    assert updated["entry"]["size"] == len(b"# new")
    assert {entry["path"] for entry in listed["entries"]} >= {"/notes/", "/notes/demo.md"}
    assert all(entry["name"] != ".keep" for entry in listed["entries"])
    assert body == b"# new"
    assert (await store.read(user_workspace_key("user-1", "notes/.keep"))).data == b""

    await svc.delete_workspace_path(path="/notes", current_user=user)
    assert await store.list(user_workspace_key("user-1", "notes") + "/") == []


@pytest.mark.asyncio
async def test_workspace_preview_and_edit_validation(store: LocalFileStore, monkeypatch) -> None:
    user = _user()
    await store.put(user_workspace_key("user-1", "bad.txt"), b"\xff\xfe\x00")
    await store.put(user_workspace_key("user-1", "slides.pptx"), b"presentation")
    await store.put(user_workspace_key("user-1", "script.py"), b"print('hello')")

    async def fake_convert(filename: str, content: bytes) -> bytes:
        assert filename == "slides.pptx"
        assert content == b"presentation"
        return b"%PDF-1.4\npreview"

    monkeypatch.setattr(svc, "convert_office_to_pdf", fake_convert)
    unsupported = await svc.read_workspace_file_content(path="/bad.txt", current_user=user)
    office = await svc.read_workspace_file_content(path="/slides.pptx", current_user=user)
    office_body = b"".join([chunk async for chunk in office.body_iterator])

    assert unsupported["supported"] is False
    assert office.media_type == "application/pdf"
    assert office_body == b"%PDF-1.4\npreview"
    with pytest.raises(HTTPException) as exc_info:
        await svc.write_workspace_file_content(path="/script.py", content="x", current_user=user)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_rolls_back_completed_objects_on_failure(store: LocalFileStore, monkeypatch) -> None:
    monkeypatch.setattr(svc, "MAX_WORKSPACE_UPLOAD_SIZE_BYTES", 5)
    uploads = [
        UploadFile(filename="small.txt", file=BytesIO(b"12345")),
        UploadFile(filename="large.txt", file=BytesIO(b"123456")),
    ]

    with pytest.raises(HTTPException):
        await svc.upload_workspace_files(parent_path="/", files=uploads, current_user=_user())

    assert await store.list(user_workspace_key("user-1", "small.txt")) == []
    assert await store.list(user_workspace_key("user-1", "large.txt")) == []


@pytest.mark.asyncio
async def test_workspace_rejects_traversal_and_chat_writes(store: LocalFileStore) -> None:
    with pytest.raises(HTTPException) as traversal:
        await svc.list_workspace_tree(path="/../secret", current_user=_user())
    with pytest.raises(HTTPException) as readonly:
        await svc.delete_workspace_path(path="/agents/chats/thread-1/uploads/a.txt", current_user=_user())

    assert traversal.value.status_code == 403
    assert readonly.value.status_code == 403


@pytest.mark.asyncio
async def test_workspace_virtual_chats_map_thread_objects_and_filter_internal_dirs(store: LocalFileStore) -> None:
    thread_id = "thread-1"
    titles = {thread_id: "2026-08-12-对话"}
    await store.put(thread_upload_key(thread_id, "note.md"), b"# upload")
    await store.put(thread_output_key(thread_id, "result.txt"), b"result")
    await store.put(thread_output_key(thread_id, "large_tool_results/internal.txt"), b"internal")
    await store.put(thread_output_key(thread_id, "empty/.keep"), b"")

    root = await svc.list_workspace_tree(path="/agents/chats", current_user=_user(), thread_titles=titles)
    recursive = await svc.list_workspace_tree(
        path="/agents/chats", recursive=True, files_only=True, current_user=_user(), thread_titles=titles
    )
    content = await svc.read_workspace_file_content(
        path=f"/agents/chats/{thread_id}/uploads/note.md", current_user=_user(), thread_titles=titles
    )

    assert root["entries"][0]["title"] == "2026-08-12-对话"
    assert {entry["path"] for entry in recursive["entries"]} == {
        f"/agents/chats/{thread_id}/uploads/note.md",
        f"/agents/chats/{thread_id}/outputs/result.txt",
    }
    assert content["content"] == "# upload"
    with pytest.raises(HTTPException) as exc_info:
        await svc.read_workspace_file_content(
            path=f"/agents/chats/{thread_id}/outputs/large_tool_results/internal.txt",
            current_user=_user(),
            thread_titles=titles,
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_physical_chats_object_conflicts_with_virtual_mapping(store: LocalFileStore) -> None:
    await store.put(user_workspace_key("user-1", "agents/chats/file.txt"), b"occupied")
    with pytest.raises(HTTPException) as exc_info:
        await svc.list_workspace_tree(path="/agents", current_user=_user(), thread_titles={})
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_build_owned_thread_titles_uses_active_conversations(monkeypatch) -> None:
    class FakeRepository:
        def __init__(self, _db):
            pass

        async def list_active_conversations_for_user(self, _uid):
            return [
                SimpleNamespace(thread_id="thread-1", title="对话", created_at=dt.datetime(2026, 8, 12)),
                SimpleNamespace(thread_id="invalid.thread", title="非法", created_at=dt.datetime(2026, 8, 11)),
            ]

    monkeypatch.setattr(svc, "ConversationRepository", FakeRepository)
    assert await svc.build_owned_thread_titles(object(), "user-1") == {"thread-1": "2026-08-12-对话"}


@pytest.mark.asyncio
async def test_workspace_write_waits_for_sandbox_uid_lock(store: LocalFileStore, monkeypatch) -> None:
    from yuxi.agents.backends.sandbox.synchronizer import sandbox_file_operation_lock

    entered_put = asyncio.Event()
    original_put = store.put

    async def observed_put(*args, **kwargs):
        entered_put.set()
        return await original_put(*args, **kwargs)

    monkeypatch.setattr(store, "put", observed_put)
    async with sandbox_file_operation_lock(uid="user-1"):
        task = asyncio.create_task(
            svc.create_workspace_directory(parent_path="/", name="blocked", current_user=_user())
        )
        await asyncio.sleep(0.05)
        assert not entered_put.is_set()
        assert not task.done()

    await task
    assert entered_put.is_set()
