from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import yuxi.services.viewer_filesystem_service as svc
from yuxi.agents.backends.sandbox.backend import FileTransferLimitError
from yuxi.services.project_workdir_service import ProjectWorkdirBinding


class _Backend:
    def __init__(self):
        self.files = {
            "/home/gem/projects/project-workdir-1/report.txt": b"hello\nworld\n",
        }
        self.directories = {
            "/home/gem/projects/project-workdir-1": [
                {"name": "outputs", "is_dir": True, "size": 0, "modified_at": 1},
                {"name": "report.txt", "is_dir": False, "size": 12, "modified_at": 2},
            ],
            "/home/gem/projects/project-workdir-1/outputs": [],
        }

    def ensure_available(self):
        return "sandbox-1"

    def list_authorized_directory(self, path, *, root):
        assert root == "/home/gem/projects/project-workdir-1"
        if path not in self.directories:
            raise FileNotFoundError(path)
        return self.directories[path]

    def download_authorized_file_to_path(self, path, target, max_bytes):
        content = self.files.get(path)
        if content is None:
            raise FileNotFoundError(path)
        if len(content) > max_bytes:
            raise FileTransferLimitError("file exceeds limit")
        Path(target).write_bytes(content)
        return len(content)

    def create_authorized_directory(self, parent, name, *, root):
        assert root == "/home/gem/projects/project-workdir-1"
        return f"{parent}/{name}"

    def delete_authorized_path(self, path, *, root):
        assert root == "/home/gem/projects/project-workdir-1"
        if self.files.pop(path, None) is None:
            raise FileNotFoundError(path)

    def upload_authorized_file_from_path(self, path, source):
        self.files[path] = Path(source).read_bytes()


@pytest.fixture
def realtime_viewer(monkeypatch):
    backend = _Backend()
    binding = ProjectWorkdirBinding(
        conversation_id=1,
        thread_id="thread-1",
        runtime_scope_id="thread-1",
        workdir_id="workdir-1",
        workdir_path="/home/gem/projects/project-workdir-1",
        uid="user-1",
    )

    async def resolve(**kwargs):
        assert kwargs["thread_id"] == "thread-1"
        assert kwargs["uid"] == "user-1"
        return binding

    monkeypatch.setattr(svc, "resolve_project_workdir_binding", resolve)
    monkeypatch.setattr(binding.__class__, "create_file_backend", lambda self, **kwargs: backend)
    return backend


@pytest.mark.asyncio
async def test_viewer_root_is_realtime_project_workdir(realtime_viewer):
    result = await svc.list_viewer_filesystem_tree(
        thread_id="thread-1", path="/", current_user=SimpleNamespace(uid="user-1"), db=object()
    )
    assert [item["name"] for item in result["entries"]] == ["outputs", "report.txt"]
    assert result["entries"][1]["path"] == "/home/gem/projects/project-workdir-1/report.txt"


@pytest.mark.asyncio
async def test_viewer_rejects_other_project_and_user_data(realtime_viewer):
    for path in ("/home/gem/projects/project-other/file.txt", "/home/gem/user-data/workspace/a.txt"):
        with pytest.raises(HTTPException) as exc:
            await svc.read_viewer_file_content(
                thread_id="thread-1", path=path, current_user=SimpleNamespace(uid="user-1"), db=object()
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_viewer_reads_live_file_without_revision(realtime_viewer):
    result = await svc.read_viewer_file_content(
        thread_id="thread-1",
        path="/home/gem/projects/project-workdir-1/report.txt",
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )
    assert result["content"] == "hello\nworld\n"
    assert result["preview_type"] == "text"


@pytest.mark.asyncio
async def test_viewer_missing_live_file_returns_not_found(realtime_viewer):
    with pytest.raises(HTTPException) as exc:
        await svc.read_viewer_file_content(
            thread_id="thread-1",
            path="/home/gem/projects/project-workdir-1/missing.txt",
            current_user=SimpleNamespace(uid="user-1"),
            db=object(),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_viewer_create_and_delete_use_same_live_backend(realtime_viewer):
    created = await svc.create_viewer_directory(
        thread_id="thread-1",
        parent_path="/",
        name="drafts",
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )
    assert created["entry"]["path"] == "/home/gem/projects/project-workdir-1/drafts/"
    deleted = await svc.delete_viewer_file(
        thread_id="thread-1",
        path="/home/gem/projects/project-workdir-1/report.txt",
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )
    assert deleted["success"] is True
    assert realtime_viewer.files == {}


@pytest.mark.asyncio
async def test_viewer_search_walks_current_workdir(realtime_viewer):
    realtime_viewer.directories["/home/gem/projects/project-workdir-1/outputs"] = [
        {"name": "final-report.md", "is_dir": False, "size": 10, "modified_at": 3}
    ]
    result = await svc.search_viewer_files(
        thread_id="thread-1", query="report", current_user=SimpleNamespace(uid="user-1"), db=object()
    )
    assert [item["name"] for item in result["entries"]] == ["report.txt", "final-report.md"]

    directory_result = await svc.search_viewer_files(
        thread_id="thread-1",
        query="output",
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )
    assert directory_result["entries"][0]["name"] == "outputs"
    assert directory_result["entries"][0]["is_dir"] is True
