from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from yuxi.services import viewer_filesystem_service as svc
from yuxi.services import thread_files_service
from yuxi.services import workspace_service
from yuxi.storage.filestore import LocalFileStore


@pytest.mark.asyncio
async def test_viewer_user_data_root_exposes_only_allowed_namespaces(monkeypatch) -> None:
    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    result = await svc.list_viewer_filesystem_tree(
        thread_id="thread-1",
        path="/home/gem/user-data",
        current_user=SimpleNamespace(uid="user-1"),
        db=None,
    )

    assert {entry["name"] for entry in result["entries"]} == {"workspace", "uploads", "outputs"}


@pytest.mark.asyncio
async def test_read_viewer_workspace_office_file_returns_pdf_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    thread_id = "thread-1"
    uid = "user-1"
    user = SimpleNamespace(uid=uid)
    store = LocalFileStore(tmp_path / "filestore")
    await store.put("users/user-1/workspace/slides.pptx", b"presentation")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    async def fake_convert(filename: str, content: bytes) -> bytes:
        assert filename == "slides.pptx"
        assert content == b"presentation"
        return b"%PDF-1.4\npreview"

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(workspace_service, "get_file_store", lambda: store)
    monkeypatch.setattr(workspace_service, "convert_office_to_pdf", fake_convert)

    response = await svc.read_viewer_file_content(
        thread_id=thread_id,
        path="/home/gem/user-data/workspace/slides.pptx",
        current_user=user,
        db=None,
    )
    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    assert response.media_type == "application/pdf"
    assert response.headers["x-yuxi-preview-type"] == "pdf"
    assert body == b"%PDF-1.4\npreview"


@pytest.mark.asyncio
async def test_viewer_object_directory_lifecycle_uses_keep_marker(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    user = SimpleNamespace(uid="user-1")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(svc, "get_file_store", lambda: store)
    monkeypatch.setattr(thread_files_service, "get_file_store", lambda: store)

    created = await svc.create_viewer_directory(
        thread_id="thread-1",
        parent_path="/home/gem/user-data/outputs",
        name="reports",
        current_user=user,
        db=None,
    )
    listed = await svc.list_viewer_filesystem_tree(
        thread_id="thread-1",
        path="/home/gem/user-data/outputs",
        current_user=user,
        db=None,
    )

    assert created["entry"]["path"] == "/home/gem/user-data/outputs/reports/"
    assert [entry["name"] for entry in listed["entries"]] == ["reports"]
    assert (await store.read("threads/thread-1/outputs/reports/.keep")).data == b""

    deleted = await svc.delete_viewer_file(
        thread_id="thread-1",
        path="/home/gem/user-data/outputs/reports",
        current_user=user,
        db=None,
    )

    assert deleted["success"] is True
    assert await store.list("threads/thread-1/outputs/reports/") == []


@pytest.mark.asyncio
async def test_viewer_output_write_waits_for_sandbox_file_thread_lock(tmp_path: Path, monkeypatch) -> None:
    from yuxi.agents.backends.sandbox.synchronizer import sandbox_file_operation_lock

    store = LocalFileStore(tmp_path / "filestore")
    entered_put = asyncio.Event()
    original_put = store.put

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    async def observed_put(*args, **kwargs):
        entered_put.set()
        return await original_put(*args, **kwargs)

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(svc, "get_file_store", lambda: store)
    monkeypatch.setattr(store, "put", observed_put)

    async with sandbox_file_operation_lock(file_thread_id="thread-1"):
        task = asyncio.create_task(
            svc.create_viewer_directory(
                thread_id="thread-1",
                parent_path="/home/gem/user-data/outputs",
                name="blocked",
                current_user=SimpleNamespace(uid="user-1"),
                db=None,
            )
        )
        await asyncio.sleep(0.05)
        assert not entered_put.is_set()
        assert not task.done()

    await task
    assert entered_put.is_set()
