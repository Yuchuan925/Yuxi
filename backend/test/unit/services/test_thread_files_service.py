from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from yuxi.services import thread_files_service as svc
from yuxi.storage.filestore import LocalFileStore


class _Conversation:
    uid = "user-1"


async def _fake_require_user_conversation(_repo, _thread_id: str, _current_uid: str):
    return _Conversation()


@pytest.mark.parametrize(
    "path",
    [
        "/home/gem/user-data/uploads/../secret.txt",
        "/home/gem/user-data/outputs/a//b.txt",
        "/home/gem/user-data/uploads/a\\b.txt",
    ],
)
def test_resolve_thread_object_path_rejects_traversal(path: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        svc.resolve_thread_object_path("thread-1", path)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_list_thread_files_uses_filestore_and_hides_keep_marker(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    await store.put("threads/thread-1/outputs/report.txt", b"result")
    await store.put("threads/thread-1/outputs/empty/.keep", b"")

    monkeypatch.setattr(svc, "get_file_store", lambda: store)
    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())

    result = await svc.list_thread_files_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/outputs",
    )

    assert [(item["name"], item["is_dir"]) for item in result["files"]] == [
        ("empty", True),
        ("report.txt", False),
    ]
    assert all(item["name"] != ".keep" for item in result["files"])


@pytest.mark.asyncio
async def test_read_thread_file_content_reads_filestore_object(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    await store.put("threads/thread-1/uploads/notes.txt", b"first\nsecond\nthird")
    monkeypatch.setattr(svc, "get_file_store", lambda: store)
    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())

    result = await svc.read_thread_file_content_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/uploads/notes.txt",
        offset=1,
        limit=1,
    )

    assert result["content"] == ["second"]


@pytest.mark.asyncio
async def test_resolve_thread_artifact_returns_filestore_stream(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    await store.put("threads/thread-1/outputs/image.jpg", b"\x89PNG\r\n\x1a\nimage", content_type="image/jpeg")
    monkeypatch.setattr(svc, "get_file_store", lambda: store)
    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())

    artifact = await svc.resolve_thread_artifact_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/outputs/image.jpg",
    )

    assert artifact.name == "image.jpg"
    assert artifact.media_type == "image/png"
    assert b"".join([chunk async for chunk in artifact.stream]) == b"\x89PNG\r\n\x1a\nimage"


@pytest.mark.asyncio
async def test_copy_thread_artifact_to_workspace_uses_filestore(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    await store.put("threads/thread-1/outputs/report.md", b"# report", content_type="text/markdown")
    monkeypatch.setattr(svc, "get_file_store", lambda: store)
    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())
    monkeypatch.setattr("yuxi.services.workspace_service.get_file_store", lambda: store)

    result = await svc.save_thread_artifact_to_workspace_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/outputs/report.md",
    )

    assert result["saved_path"] == "/home/gem/user-data/workspace/saved_artifacts/report.md"
    assert (await store.read("users/user-1/workspace/saved_artifacts/report.md")).data == b"# report"
