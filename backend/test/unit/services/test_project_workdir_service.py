from types import SimpleNamespace

import pytest

from yuxi.services import project_workdir_service as svc


def test_file_backend_uses_workdir_bridge_scope_not_execution_runtime(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class _Backend:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(svc, "ProvisionerSandboxBackend", _Backend)
    binding = svc.ProjectWorkdirBinding(
        conversation_id=1,
        thread_id="child-thread",
        runtime_scope_id="root-thread",
        workdir_id="workdir-1",
        workdir_path="/home/gem/projects/project-workdir-1",
        uid="user-1",
    )

    binding.create_file_backend()

    assert captured["thread_id"] == "workdir-files-workdir-1"
    assert captured["sandbox_instance_id"] == "workdir-files-workdir-1"
    assert captured["workdir_id"] == "workdir-1"


@pytest.mark.asyncio
async def test_nested_subagent_binding_resolves_root_runtime(monkeypatch: pytest.MonkeyPatch):
    conversations = {
        1: SimpleNamespace(id=1, thread_id="root", uid="user-1", status="active", workdir_id="workdir-1"),
        2: SimpleNamespace(id=2, thread_id="child", uid="user-1", status="subagent", workdir_id="workdir-1"),
        3: SimpleNamespace(id=3, thread_id="grandchild", uid="user-1", status="subagent", workdir_id="workdir-1"),
    }
    by_thread = {item.thread_id: item for item in conversations.values()}
    relations = {
        3: SimpleNamespace(parent_conversation_id=2),
        2: SimpleNamespace(parent_conversation_id=1),
    }

    class _ConversationRepository:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, thread_id):
            return by_thread.get(thread_id)

        async def get_conversation_by_id(self, conversation_id):
            return conversations.get(conversation_id)

    class _RelationRepository:
        def __init__(self, _db):
            pass

        async def get_by_child_conversation_for_user(self, conversation_id, _uid):
            return relations.get(conversation_id)

    class _WorkdirRepository:
        def __init__(self, _db):
            pass

        async def require_for_user(self, _workdir_id, _uid):
            return SimpleNamespace(materialization_status="ready", materialization_epoch_id="epoch-1")

    async def active(_db):
        return SimpleNamespace(epoch_id="epoch-1")

    monkeypatch.setattr(svc, "ConversationRepository", _ConversationRepository)
    monkeypatch.setattr(svc, "SubagentThreadRepository", _RelationRepository)
    monkeypatch.setattr(svc, "ProjectWorkdirRepository", _WorkdirRepository)
    monkeypatch.setattr(svc, "require_project_workdir_active", active)

    binding = await svc.resolve_project_workdir_binding(
        thread_id="grandchild",
        uid="user-1",
        db=object(),
    )

    assert binding.runtime_scope_id == "root"
    assert binding.workdir_id == "workdir-1"


@pytest.mark.asyncio
async def test_nested_subagent_binding_rejects_cycle(monkeypatch: pytest.MonkeyPatch):
    conversations = {
        1: SimpleNamespace(id=1, thread_id="root", uid="user-1", status="active", workdir_id="workdir-1"),
        2: SimpleNamespace(id=2, thread_id="child", uid="user-1", status="subagent", workdir_id="workdir-1"),
    }
    relations = {
        1: SimpleNamespace(parent_conversation_id=2),
        2: SimpleNamespace(parent_conversation_id=1),
    }

    class _ConversationRepository:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, _thread_id):
            return conversations[2]

        async def get_conversation_by_id(self, conversation_id):
            return conversations.get(conversation_id)

    class _RelationRepository:
        def __init__(self, _db):
            pass

        async def get_by_child_conversation_for_user(self, conversation_id, _uid):
            return relations.get(conversation_id)

    class _WorkdirRepository:
        def __init__(self, _db):
            pass

        async def require_for_user(self, _workdir_id, _uid):
            return SimpleNamespace(materialization_status="ready", materialization_epoch_id="epoch-1")

    async def active(_db):
        return SimpleNamespace(epoch_id="epoch-1")

    monkeypatch.setattr(svc, "ConversationRepository", _ConversationRepository)
    monkeypatch.setattr(svc, "SubagentThreadRepository", _RelationRepository)
    monkeypatch.setattr(svc, "ProjectWorkdirRepository", _WorkdirRepository)
    monkeypatch.setattr(svc, "require_project_workdir_active", active)

    with pytest.raises(RuntimeError, match="循环"):
        await svc.resolve_project_workdir_binding(thread_id="child", uid="user-1", db=object())
