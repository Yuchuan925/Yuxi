from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import HTTPException

from yuxi.services import thread_files_service as svc


class _Conversation:
    uid = "user-1"
    extra_metadata = None


async def _fake_require_user_conversation(_repo, _thread_id: str, _current_uid: str):
    return _Conversation()


@pytest.mark.asyncio
async def test_read_thread_file_content_runs_file_read_in_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    file_path = tmp_path / "notes.txt"
    file_path.write_text("first\nsecond\nthird", encoding="utf-8")
    threaded_calls = []

    async def _fake_to_thread(func, *args, **kwargs):
        threaded_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())
    monkeypatch.setattr(svc, "resolve_virtual_path", lambda _thread_id, _path, *, uid: file_path)
    monkeypatch.setattr(svc.asyncio, "to_thread", _fake_to_thread)

    result = await svc.read_thread_file_content_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/workspace/notes.txt",
        offset=1,
        limit=1,
    )

    assert result["content"] == ["second"]
    assert threaded_calls == [file_path.read_text]


@pytest.mark.asyncio
async def test_list_thread_files_runs_directory_scan_in_worker_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = tmp_path / "outputs"
    directory.mkdir()
    (directory / "result.txt").write_text("result", encoding="utf-8")
    threaded_calls = []

    async def _fake_to_thread(func, *args, **kwargs):
        threaded_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())
    monkeypatch.setattr(svc, "ensure_thread_dirs", lambda _thread_id, _uid: None)
    monkeypatch.setattr(svc, "resolve_virtual_path", lambda _thread_id, _path, *, uid: directory)
    monkeypatch.setattr(
        svc,
        "virtual_path_for_thread_file",
        lambda _thread_id, path, *, uid: f"/home/gem/user-data/outputs/{path.name}",
    )
    monkeypatch.setattr(svc.asyncio, "to_thread", _fake_to_thread)

    result = await svc.list_thread_files_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/outputs",
    )

    assert [item["name"] for item in result["files"]] == ["result.txt"]
    assert threaded_calls == [svc._list_directory_entries]


@pytest.mark.asyncio
async def test_list_published_outputs_honors_recursive_flag(monkeypatch: pytest.MonkeyPatch):
    files = [
        {"path": "/home/gem/user-data/outputs/report.txt", "size": 6},
        {"path": "/home/gem/user-data/outputs/nested/chart.csv", "size": 8},
    ]

    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())
    monkeypatch.setattr(svc, "materialize_attachment_records", lambda *_args, **_kwargs: _async_none())
    monkeypatch.setattr(svc, "get_current_output_snapshot", lambda **_kwargs: _async_value(("revision-1", files)))

    result = await svc.list_thread_files_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/outputs",
        recursive=True,
    )

    assert [item["path"] for item in result["files"]] == [
        "/home/gem/user-data/outputs/nested/",
        "/home/gem/user-data/outputs/nested/chart.csv",
        "/home/gem/user-data/outputs/report.txt",
    ]


@pytest.mark.asyncio
async def test_recursive_user_data_root_replaces_legacy_output_descendants(monkeypatch: pytest.MonkeyPatch):
    files = [{"path": "/home/gem/user-data/outputs/nested/current.csv", "size": 8}]
    root_scan_kwargs = {}

    async def fake_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    def fake_root_entries(*_args, **kwargs):
        root_scan_kwargs.update(kwargs)
        return {
            "path": "/home/gem/user-data",
            "files": [
                {"path": "/home/gem/user-data/outputs/", "name": "outputs", "is_dir": True},
                {
                    "path": "/home/gem/user-data/outputs/legacy.txt",
                    "name": "legacy.txt",
                    "is_dir": False,
                },
                {"path": "/home/gem/user-data/workspace/", "name": "workspace", "is_dir": True},
            ],
        }

    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())
    monkeypatch.setattr(svc, "materialize_attachment_records", lambda *_args, **_kwargs: _async_none())
    monkeypatch.setattr(svc, "get_current_output_snapshot", lambda **_kwargs: _async_value(("revision-1", files)))
    monkeypatch.setattr(svc, "ensure_thread_dirs", lambda *_args: None)
    monkeypatch.setattr(svc, "_list_user_data_root_entries", fake_root_entries)
    monkeypatch.setattr(svc.asyncio, "to_thread", fake_to_thread)

    result = await svc.list_thread_files_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data",
        recursive=True,
    )

    paths = {item["path"] for item in result["files"]}
    assert "/home/gem/user-data/outputs/" in paths
    assert "/home/gem/user-data/outputs/legacy.txt" not in paths
    assert "/home/gem/user-data/outputs/nested/current.csv" in paths
    assert root_scan_kwargs == {"recursive": True, "recursive_skip_names": {"outputs"}}


async def _async_none():
    return None


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_resolve_thread_artifact_view_blocks_symlink_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    thread_root = tmp_path / "threads" / "thread-1" / "user-data"
    uploads_dir = thread_root / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    (uploads_dir / "escape.txt").symlink_to(outside_file)

    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ensure_thread_dirs", lambda _thread_id, _uid: None)
    monkeypatch.setattr(
        svc,
        "sandbox_workspace_dir",
        lambda _thread_id, _uid: tmp_path / "shared" / _uid / "workspace",
    )
    monkeypatch.setattr(svc, "sandbox_uploads_dir", lambda _thread_id: uploads_dir)
    monkeypatch.setattr(svc, "sandbox_outputs_dir", lambda _thread_id: thread_root / "outputs")
    monkeypatch.setattr(svc, "resolve_virtual_path", lambda _thread_id, _path, *, uid: uploads_dir / "escape.txt")
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())

    with pytest.raises(HTTPException, match="access denied"):
        await svc.resolve_thread_artifact_view(
            thread_id="thread-1",
            current_uid="user-1",
            db=None,
            path="/home/gem/user-data/uploads/escape.txt",
        )


@pytest.mark.asyncio
async def test_save_published_artifact_releases_minio_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    content = b"published artifact"

    class FakeResponse:
        def __init__(self):
            self.stream = io.BytesIO(content)
            self.closed = False
            self.released = False

        def read(self, size):
            return self.stream.read(size)

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    response = FakeResponse()
    minio = type(
        "FakeMinio",
        (),
        {"adownload_response": lambda _self, *_args: _async_value(response)},
    )()

    monkeypatch.setattr(
        svc,
        "resolve_thread_artifact_view",
        lambda **_kwargs: _async_value(
            {
                "path": "/home/gem/user-data/outputs/report.txt",
                "bucket_name": "thread-files",
                "object_name": "outputs/report.txt",
            }
        ),
    )
    monkeypatch.setattr(svc, "require_user_conversation", _fake_require_user_conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: object())
    monkeypatch.setattr(svc, "sandbox_workspace_dir", lambda *_args: tmp_path / "workspace")
    monkeypatch.setattr(svc, "get_minio_client", lambda: minio)
    monkeypatch.setattr(svc, "invalidate_mention_cache", lambda *_args: _async_none())
    monkeypatch.setattr(svc, "invalidate_workspace_mention_cache", lambda *_args: _async_none())
    monkeypatch.setattr(
        svc,
        "virtual_path_for_thread_file",
        lambda *_args, **_kwargs: "/home/gem/user-data/workspace/saved_artifacts/report.txt",
    )

    result = await svc.save_thread_artifact_to_workspace_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=None,
        path="/home/gem/user-data/outputs/report.txt",
    )

    assert result["name"] == "report.txt"
    assert (tmp_path / "workspace" / "saved_artifacts" / "report.txt").read_bytes() == content
    assert response.closed is True
    assert response.released is True
