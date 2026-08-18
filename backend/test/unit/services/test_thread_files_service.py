from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

import yuxi.services.thread_files_service as svc
from yuxi.agents.backends.sandbox.backend import FileTransferLimitError
from yuxi.services.project_workdir_service import ProjectWorkdirBinding


class _Backend:
    def __init__(self):
        self.files = {
            "/home/gem/projects/project-workdir-1/report.md": b"one\ntwo\n",
            "/home/gem/user-data/notes.txt": b"private",
            "/home/gem/skills/reporter/SKILL.md": b"skill",
        }
        self.directories = {
            "/home/gem/projects/project-workdir-1": [
                {"name": "outputs", "is_dir": True, "size": 0},
                {"name": "report.md", "is_dir": False, "size": 8},
            ],
            "/home/gem/projects/project-workdir-1/outputs": [{"name": "nested.txt", "is_dir": False, "size": 3}],
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
        assert len(content) <= max_bytes
        Path(target).write_bytes(content)
        return len(content)

    def regular_file_exists(self, path):
        return path in self.files

    def upload_authorized_file_from_path(self, path, source):
        self.files[path] = Path(source).read_bytes()


@pytest.fixture
def live_files(monkeypatch):
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
        assert kwargs["uid"] == "user-1"
        return binding

    async def invalidate(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr(svc, "resolve_project_workdir_binding", resolve)
    monkeypatch.setattr(binding.__class__, "create_file_backend", lambda self, **kwargs: backend)
    monkeypatch.setattr(svc, "invalidate_mention_cache", invalidate)
    monkeypatch.setattr(svc, "invalidate_workspace_mention_cache", invalidate)
    monkeypatch.setattr(
        svc,
        "UserRepository",
        lambda _db: type(
            "Repo",
            (),
            {"get_by_uid": lambda self, uid: _async_value(type("User", (), {"uid": uid, "is_deleted": False})())},
        )(),
    )
    monkeypatch.setattr(
        svc,
        "list_accessible_skills",
        lambda _db, _user: _async_value([type("Skill", (), {"slug": "reporter"})()]),
    )
    return backend


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_list_thread_files_reads_live_project_tree(live_files):
    result = await svc.list_thread_files_view(
        thread_id="thread-1", current_uid="user-1", db=object(), path="/", recursive=False
    )
    assert [item["name"] for item in result["files"]] == ["outputs", "report.md"]
    assert result["path"] == "/home/gem/projects/project-workdir-1"


@pytest.mark.asyncio
async def test_recursive_thread_files_includes_nested_current_files(live_files):
    result = await svc.list_thread_files_view(
        thread_id="thread-1", current_uid="user-1", db=object(), path="/", recursive=True
    )
    assert [item["path"] for item in result["files"]] == [
        "/home/gem/projects/project-workdir-1/outputs/",
        "/home/gem/projects/project-workdir-1/report.md",
        "/home/gem/projects/project-workdir-1/outputs/nested.txt",
    ]


@pytest.mark.asyncio
async def test_read_thread_file_uses_live_workdir(live_files):
    result = await svc.read_thread_file_content_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=object(),
        path="/home/gem/projects/project-workdir-1/report.md",
        offset=1,
        limit=1,
    )
    assert result["content"] == ["two"]


@pytest.mark.asyncio
async def test_thread_file_transfer_limit_is_not_reported_as_missing(live_files, monkeypatch):
    def reject_large_file(*_args, **_kwargs):
        raise FileTransferLimitError("file exceeds transfer limit")

    monkeypatch.setattr(live_files, "download_authorized_file_to_path", reject_large_file)
    with pytest.raises(HTTPException) as exc:
        await svc.read_thread_file_content_view(
            thread_id="thread-1",
            current_uid="user-1",
            db=object(),
            path="/home/gem/projects/project-workdir-1/report.md",
        )
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_artifact_allows_project_user_data_and_authorized_skills(live_files):
    for path in (
        "/home/gem/projects/project-workdir-1/report.md",
        "/home/gem/user-data/notes.txt",
        "/home/gem/skills/reporter/SKILL.md",
    ):
        response = await svc.resolve_thread_artifact_view(
            thread_id="thread-1", current_uid="user-1", db=object(), path=path
        )
        assert Path(response.path).read_bytes() == live_files.files[path]
        await response.background()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_name", ["报告.txt", 'quoted"name.txt', "line\nbreak.txt"])
async def test_artifact_download_encodes_untrusted_posix_filename(live_files, file_name):
    path = f"/home/gem/projects/project-workdir-1/{file_name}"
    live_files.files[path] = b"safe"

    response = await svc.resolve_thread_artifact_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=object(),
        path=path,
        download=True,
    )

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "\r" not in disposition and "\n" not in disposition
    assert file_name not in disposition
    await response.background()


@pytest.mark.asyncio
async def test_artifact_rejects_other_project(live_files):
    with pytest.raises(HTTPException) as exc:
        await svc.resolve_thread_artifact_view(
            thread_id="thread-1",
            current_uid="user-1",
            db=object(),
            path="/home/gem/projects/project-other/secret.txt",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_artifact_rechecks_current_skill_authorization(live_files, monkeypatch):
    monkeypatch.setattr(svc, "list_accessible_skills", lambda _db, _user: _async_value([]))

    with pytest.raises(HTTPException) as exc:
        await svc.resolve_thread_artifact_view(
            thread_id="thread-1",
            current_uid="user-1",
            db=object(),
            path="/home/gem/skills/reporter/SKILL.md",
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_artifact_transfer_limit_is_not_reported_as_missing(live_files, monkeypatch):
    def reject_large_file(*_args, **_kwargs):
        raise FileTransferLimitError("file exceeds transfer limit")

    monkeypatch.setattr(live_files, "download_authorized_file_to_path", reject_large_file)
    with pytest.raises(HTTPException) as exc:
        await svc.resolve_thread_artifact_view(
            thread_id="thread-1",
            current_uid="user-1",
            db=object(),
            path="/home/gem/projects/project-workdir-1/report.md",
        )
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_save_artifact_copies_live_bytes_to_user_data(live_files):
    result = await svc.save_thread_artifact_to_workspace_view(
        thread_id="thread-1",
        current_uid="user-1",
        db=object(),
        path="/home/gem/projects/project-workdir-1/report.md",
    )
    assert result["saved_path"] == "/home/gem/user-data/workspace/saved_artifacts/report.md"
    assert live_files.files[result["saved_path"]] == b"one\ntwo\n"
