from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services import workdir_service as svc


def test_file_backend_is_scoped_to_user_workspace(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class _Filesystem:
        def __init__(self, uid):
            captured["uid"] = uid

    monkeypatch.setattr(svc, "WorkspaceFilesystem", _Filesystem)
    binding = svc.WorkdirBinding(
        conversation_id=1,
        thread_id="thread-1",
        workdir_path="projects/workdir-1",
        virtual_path="/home/gem/user-data/projects/workdir-1",
        uid="user-1",
    )

    binding.create_file_backend()

    assert captured == {"uid": "user-1"}


@pytest.mark.asyncio
async def test_binding_uses_persisted_relative_workdir(monkeypatch: pytest.MonkeyPatch):
    conversation = SimpleNamespace(
        id=1,
        thread_id="thread-1",
        uid="user-1",
        status="active",
        workdir_path="projects/workdir-1",
    )

    class _ConversationRepository:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, _thread_id):
            return conversation

    checked = {}

    def check(uid, workdir_path):
        checked.update(uid=uid, workdir_path=workdir_path)

    monkeypatch.setattr(svc, "ConversationRepository", _ConversationRepository)
    monkeypatch.setattr(svc, "ensure_bound_user_workdir", check)

    binding = await svc.resolve_workdir_binding(thread_id="thread-1", uid="user-1", db=object())

    assert binding.workdir_path == "projects/workdir-1"
    assert binding.virtual_path == "/home/gem/user-data/projects/workdir-1"
    assert checked == {"uid": "user-1", "workdir_path": "projects/workdir-1"}


@pytest.mark.asyncio
async def test_binding_rejects_cross_user_conversation(monkeypatch: pytest.MonkeyPatch):
    conversation = SimpleNamespace(
        id=1,
        thread_id="thread-1",
        uid="other-user",
        status="active",
        workdir_path="projects/workdir-1",
    )

    class _ConversationRepository:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, _thread_id):
            return conversation

    monkeypatch.setattr(svc, "ConversationRepository", _ConversationRepository)

    with pytest.raises(HTTPException) as exc:
        await svc.resolve_workdir_binding(thread_id="thread-1", uid="user-1", db=object())

    assert exc.value.status_code == 404
