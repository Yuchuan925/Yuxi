"""Tests for sandbox backend components."""

from __future__ import annotations

import base64
import gc
import hashlib
import threading
import weakref
from types import MethodType, SimpleNamespace

import pytest
from deepagents.backends.protocol import GlobResult, ReadResult
from deepagents.backends.sandbox import MAX_BINARY_BYTES
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from yuxi.agents.backends.composite import (
    CustomCompositeBackend,
    create_agent_composite_backend,
    create_agent_filesystem_middleware,
    sync_agent_context_skills,
)
from yuxi.agents.backends.sandbox import ProvisionerSandboxProvider, resolve_virtual_path, sandbox_id_for_thread
from yuxi.agents.backends.sandbox.backend import ProvisionerSandboxBackend
from yuxi.agents.backends.sandbox.provider import SandboxIdentityMismatchError
from yuxi.agents.middlewares.skills import SkillsMiddleware
from yuxi.utils.paths import VIRTUAL_PATH_CONVERSATION_HISTORY, VIRTUAL_PATH_LARGE_TOOL_RESULTS


def _runtime(
    *,
    thread_id: str | None = "thread-1",
    uid: str | None = "user-1",
    skills: list[str] | None = None,
    readable_skills: list[str] | None = None,
    skill_sources: dict[str, str] | None = None,
    visible_kbs: list[dict] | None = None,
):
    configurable = {"thread_id": thread_id, "uid": uid} if thread_id and uid else {}
    return SimpleNamespace(
        config={"configurable": configurable},
        context=SimpleNamespace(
            skills=skills or [],
            _readable_skills=readable_skills,
            _runtime_skill_sources=skill_sources or {},
            _visible_knowledge_bases=visible_kbs or [],
            uid=uid,
        ),
    )


def _make_provider(client) -> ProvisionerSandboxProvider:
    provider = ProvisionerSandboxProvider.__new__(ProvisionerSandboxProvider)
    provider._client = client
    provider._lock = threading.Lock()
    provider._thread_locks = {}
    provider._connections = {}
    provider._last_touch_at = {}
    provider._touch_interval_seconds = 30
    return provider


def test_create_agent_composite_backend_uses_prepared_readable_skills(monkeypatch):
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())

    backend = create_agent_composite_backend(
        _runtime(
            readable_skills=["reporter"],
            skill_sources={"reporter": "/tmp/reporter"},
            visible_kbs=[{"slug": "db-1", "name": "Docs"}],
        )
    )

    assert isinstance(backend.default, ProvisionerSandboxBackend)
    assert backend.routes["/skills/"]._selected_slugs == {"reporter"}
    assert backend.artifacts_root == "/home/gem/user-data/outputs"
    assert "/skills/" in backend.routes
    assert "/home/gem/kbs/" not in backend.routes


def test_sandbox_provider_release_deletes_sandbox_and_clears_cache():
    deleted: list[tuple[str, str | None]] = []
    provider = object.__new__(ProvisionerSandboxProvider)
    provider._lock = threading.Lock()
    provider._thread_locks = {}
    provider._connections = {}
    provider._last_touch_at = {}
    provider._client = SimpleNamespace(
        delete=lambda sandbox_id, *, expected_generation=None: deleted.append(
            (sandbox_id, expected_generation)
        )
    )
    connection = SimpleNamespace(
        sandbox_id="sandbox-1",
        workdir_id=None,
        generation="generation-1",
    )
    cache_key = "user-1::thread-1::thread-1"
    provider._connections[cache_key] = connection
    provider._last_touch_at[cache_key] = 1.0

    provider.release("thread-1", uid="user-1")

    assert deleted == [("sandbox-1", "generation-1")]
    assert cache_key not in provider._connections
    assert cache_key not in provider._last_touch_at


def test_sandbox_provider_discards_unused_thread_locks():
    provider = object.__new__(ProvisionerSandboxProvider)
    provider._lock = threading.Lock()
    provider._thread_locks = weakref.WeakValueDictionary()

    lock = provider._thread_lock("user-1::thread-1::thread-1")
    lock_ref = weakref.ref(lock)
    assert provider._thread_lock("user-1::thread-1::thread-1") is lock

    del lock
    gc.collect()

    assert lock_ref() is None
    assert not provider._thread_locks


def test_sandbox_provider_waiter_keeps_shared_thread_lock_alive():
    provider = object.__new__(ProvisionerSandboxProvider)
    provider._lock = threading.Lock()
    provider._thread_locks = weakref.WeakValueDictionary()
    cache_key = "user-1::thread-1::thread-1"
    first_lock = provider._thread_lock(cache_key)
    waiter_ready = threading.Event()
    waiter_acquired = threading.Event()

    def wait_for_lock() -> None:
        waiting_lock = provider._thread_lock(cache_key)
        assert waiting_lock is first_lock
        waiter_ready.set()
        with waiting_lock:
            waiter_acquired.set()

    first_lock.acquire()
    waiter = threading.Thread(target=wait_for_lock)
    waiter.start()
    assert waiter_ready.wait(timeout=1)
    assert provider._thread_lock(cache_key) is first_lock
    assert not waiter_acquired.is_set()

    first_lock.release()
    waiter.join(timeout=1)

    assert waiter_acquired.is_set()


@pytest.mark.parametrize("clear_cache_on_delete_failure", [False, True])
def test_sandbox_provider_release_on_delete_failure(clear_cache_on_delete_failure):
    provider = object.__new__(ProvisionerSandboxProvider)
    provider._lock = threading.Lock()
    provider._thread_locks = {}
    provider._connections = {}
    provider._last_touch_at = {}

    def fail_delete(_sandbox_id, *, expected_generation=None):
        _ = expected_generation
        raise RuntimeError("delete failed")

    provider._client = SimpleNamespace(delete=fail_delete)
    connection = SimpleNamespace(
        sandbox_id="sandbox-1",
        workdir_id=None,
        generation="generation-1",
    )
    cache_key = "user-1::thread-1::thread-1"
    provider._connections[cache_key] = connection
    provider._last_touch_at[cache_key] = 1.0

    with pytest.raises(RuntimeError, match="delete failed"):
        provider.release(
            "thread-1",
            uid="user-1",
            clear_cache_on_delete_failure=clear_cache_on_delete_failure,
        )

    if clear_cache_on_delete_failure:
        assert cache_key not in provider._connections
        assert cache_key not in provider._last_touch_at
    else:
        assert provider._connections[cache_key] is connection
        assert provider._last_touch_at[cache_key] == 1.0


@pytest.mark.asyncio
async def test_sync_agent_context_skills_projects_all_user_authorized_skills(monkeypatch):
    """Run 初始化应同步用户授权的全部 Skill，不将选中集合作为文件权限。"""
    calls = []

    async def refresh_user_skill_projection_async(uid):
        calls.append(uid)
        return {
            "worker-skill": "/tmp/worker-skill",
            "authorized-unselected": "/tmp/authorized-unselected",
        }

    monkeypatch.setattr(
        "yuxi.agents.backends.composite.refresh_user_skill_projection_async",
        refresh_user_skill_projection_async,
    )
    context = SimpleNamespace(
        thread_id="child-thread",
        uid="user-1",
        _readable_skills=["worker-skill", "personal-skill"],
        _runtime_skill_sources={
            "worker-skill": "/tmp/worker-skill",
            "authorized-unselected": "/tmp/authorized-unselected",
        },
    )

    await sync_agent_context_skills(context)

    assert calls == ["user-1"]
    assert context._runtime_skill_sources == {
        "worker-skill": "/tmp/worker-skill",
        "authorized-unselected": "/tmp/authorized-unselected",
    }


@pytest.mark.parametrize("invalid_sources", [None, [], {"": "/tmp/demo"}, {"demo": ""}])
def test_create_agent_composite_backend_rejects_missing_or_invalid_skill_sources(invalid_sources):
    """授权来源契约缺失时不得把用户级投影解释为空授权并清理。"""
    context = SimpleNamespace(uid="user-1")
    if invalid_sources is not None:
        context._runtime_skill_sources = invalid_sources
    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": "thread-1", "uid": "user-1"}},
        context=context,
    )

    with pytest.raises(ValueError, match="_runtime_skill_sources"):
        create_agent_composite_backend(runtime)


def test_create_agent_composite_backend_requires_thread_id():
    with pytest.raises(ValueError, match="thread_id is required"):
        create_agent_composite_backend(_runtime(thread_id=None))


def test_create_agent_composite_backend_ignores_unprepared_context_skills(monkeypatch):
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())

    backend = create_agent_composite_backend(_runtime(skills=["configured"], readable_skills=None))

    assert backend.routes["/skills/"]._selected_slugs == set()


@pytest.mark.parametrize("scope_source", ["config", "state"])
def test_create_agent_composite_backend_ignores_legacy_file_thread_scope(monkeypatch, scope_source):
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    runtime = _runtime(
        thread_id="child-thread",
        uid="user-1",
        readable_skills=["worker-skill"],
        skill_sources={"worker-skill": "/tmp/worker-skill"},
    )
    split_scopes = {"file_thread_id": "parent-thread"}
    if scope_source == "config":
        runtime.config["configurable"].update(split_scopes)
    else:
        runtime.state = split_scopes

    backend = create_agent_composite_backend(runtime)

    assert backend.default._thread_id == "child-thread"
    assert not hasattr(backend.default, "_file_thread_id")
    assert backend.routes["/skills/"]._selected_slugs == {"worker-skill"}


def test_create_agent_filesystem_middleware_uses_context_scope(monkeypatch):
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    context = SimpleNamespace(
        thread_id="child-thread",
        uid="user-1",
        file_thread_id="parent-thread",
        _readable_skills=["worker-skill"],
        _runtime_skill_sources={"worker-skill": "/tmp/worker-skill"},
    )

    middleware = create_agent_filesystem_middleware(context=context)
    backend = middleware.backend(None)

    assert backend.default._thread_id == "child-thread"
    assert not hasattr(backend.default, "_file_thread_id")
    assert backend.routes["/skills/"]._selected_slugs == {"worker-skill"}


def test_context_backend_construction_does_not_sync_skill_projection(monkeypatch, tmp_path) -> None:
    """每轮模型调用重建 backend 时不得扫描或复制 Skill。"""
    from yuxi.agents.skills import service as skill_service

    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    source_dir = tmp_path / "source" / "shared-skill"
    source_dir.mkdir(parents=True)
    (source_dir / "SKILL.md").write_text("# Shared", encoding="utf-8")
    context = SimpleNamespace(
        thread_id="thread-1",
        uid="user-1",
        _readable_skills=["shared-skill"],
        _runtime_skill_sources={"shared-skill": str(source_dir)},
    )

    middleware = create_agent_filesystem_middleware(context=context)
    middleware.backend(None)
    middleware.backend(None)

    user_skill = skill_service.get_user_skills_root_dir("user-1") / "shared-skill"
    assert not user_skill.exists()


def test_context_backend_rebuild_drops_shared_projection_after_personal_override(monkeypatch):
    """运行中同名个人 Skill 生效后，后续文件工具不得恢复旧共享投影。"""
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    context = SimpleNamespace(
        thread_id="thread-1",
        uid="user-1",
        _readable_skills=["demo"],
        _runtime_skill_sources={"demo": "/tmp/shared-demo"},
    )
    middleware = create_agent_filesystem_middleware(context=context)

    before_install = middleware.backend(None)
    context._runtime_skill_sources.pop("demo")
    after_install = middleware.backend(None)

    assert before_install.routes["/skills/"]._selected_slugs == {"demo"}
    assert after_install.routes["/skills/"]._selected_slugs == set()


def test_create_agent_composite_backend_exposes_all_authorized_skill_sources(monkeypatch):
    """用户授权但未选中的 Skill 仍可读，选中状态只由 Prompt 与工具管理。"""
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())

    backend = create_agent_composite_backend(
        _runtime(
            readable_skills=["shared-skill", "personal-skill"],
            skill_sources={
                "shared-skill": "/tmp/shared-skill",
                "personal-skill": "/tmp/personal-skill",
            },
        )
    )

    assert backend.routes["/skills/"]._selected_slugs == {"shared-skill", "personal-skill"}


def test_create_agent_filesystem_middleware_uses_outputs_for_internal_artifacts() -> None:
    middleware = create_agent_filesystem_middleware(tool_token_limit_before_evict=500)

    assert middleware._tool_token_limit_before_evict == 500
    assert middleware._large_tool_results_prefix == VIRTUAL_PATH_LARGE_TOOL_RESULTS
    assert middleware._conversation_history_prefix == VIRTUAL_PATH_CONVERSATION_HISTORY


def test_filesystem_middleware_evicts_large_non_read_file_tool_result() -> None:
    class _Backend:
        artifacts_root = "/"

        def __init__(self):
            self.writes: list[tuple[str, str]] = []

        def write(self, path: str, content: str):
            self.writes.append((path, content))
            return SimpleNamespace(error=None)

    backend = _Backend()
    middleware = create_agent_filesystem_middleware(tool_token_limit_before_evict=1)
    middleware.backend = backend
    request = SimpleNamespace(tool_call={"name": "grep"}, runtime=SimpleNamespace())
    content = "BEGIN\n" + ("middle\n" * 5000) + "END"

    result = middleware.wrap_tool_call(
        request,
        lambda _: ToolMessage(content=content, name="grep", tool_call_id="call-grep"),
    )

    assert backend.writes == [(f"{VIRTUAL_PATH_LARGE_TOOL_RESULTS}/call-grep", content)]
    assert isinstance(result, ToolMessage)
    assert len(result.content) < len(content)
    assert f"{VIRTUAL_PATH_LARGE_TOOL_RESULTS}/call-grep" in result.content


def test_filesystem_middleware_keeps_read_file_result_inline_to_avoid_evict_loop() -> None:
    class _Backend:
        artifacts_root = "/"

        def __init__(self):
            self.writes: list[tuple[str, str]] = []

        def write(self, path: str, content: str):
            self.writes.append((path, content))
            return SimpleNamespace(error=None)

    backend = _Backend()
    middleware = create_agent_filesystem_middleware(tool_token_limit_before_evict=1)
    middleware.backend = backend
    request = SimpleNamespace(tool_call={"name": "read_file"}, runtime=SimpleNamespace())
    content = "x" * 100

    result = middleware.wrap_tool_call(
        request,
        lambda _: ToolMessage(content=content, name="read_file", tool_call_id="call-read"),
    )

    assert backend.writes == []
    assert result.content == content


def test_custom_composite_glob_only_searches_routes_from_root() -> None:
    class _Backend:
        def __init__(self, name: str):
            self.name = name
            self.calls: list[tuple[str, str]] = []

        def glob(self, pattern: str, path: str = "/") -> GlobResult:
            self.calls.append((pattern, path))
            return GlobResult(matches=[{"path": f"{path.rstrip('/')}/{self.name}.md"}])

    default = _Backend("default")
    routed = _Backend("skill")
    backend = CustomCompositeBackend(default=default, routes={"/skills/": routed})

    result = backend.glob("**/*.md", path="/home/gem/user-data")

    assert result.error is None
    assert default.calls == [("**/*.md", "/home/gem/user-data")]
    assert routed.calls == []


def test_skills_middleware_extracts_slug_for_new_paths() -> None:
    middleware = SkillsMiddleware()
    assert middleware.skills_sources_for_prompt == [
        "/home/gem/skills/",
        "/home/gem/user-data/workspace/agents/skills/",
    ]
    assert middleware._extract_skill_slug_from_skill_md_path("/home/gem/skills/demo-skill/SKILL.md") == "demo-skill"


def test_resolve_virtual_path_rejects_outside_prefix():
    with pytest.raises(ValueError, match="path must start with"):
        resolve_virtual_path("thread-1", "/etc/passwd", uid="user-1")


def test_resolve_virtual_path_rejects_path_traversal():
    with pytest.raises(ValueError, match="path traversal"):
        resolve_virtual_path("thread-1", "/home/gem/user-data/../secrets", uid="user-1")


def test_sandbox_id_for_thread_is_stable():
    sid1 = sandbox_id_for_thread("thread-1")
    sid2 = sandbox_id_for_thread("thread-1")
    sid3 = sandbox_id_for_thread("thread-2")
    assert sid1 == sid2
    assert sid1 != sid3
    assert len(sid1) == 12


def test_provider_revalidates_runtime_generation_after_keepalive(monkeypatch) -> None:
    class FakeClient:
        def create(self, sandbox_id, *_args, **kwargs):
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox/generation-1",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

        def touch(self, _sandbox_id):
            return True

        def discover(self, sandbox_id):
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox/generation-2",
                generation="generation-2",
                workdir_id="workdir-1",
            )

    provider = _make_provider(FakeClient())
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.load_user_agent_env", lambda _uid: {})
    provider.acquire("root-thread", uid="user-1", workdir_id="workdir-1")
    connection = next(iter(provider._connections.values()))
    provider._last_touch_at[connection.cache_key] = 0

    refreshed = provider.get("root-thread", uid="user-1", workdir_id="workdir-1")

    assert refreshed is connection
    assert refreshed.generation == "generation-2"
    assert refreshed.sandbox_url == "http://sandbox/generation-2"


def test_provider_rejects_project_workdir_drift_after_keepalive(monkeypatch) -> None:
    class FakeClient:
        def create(self, sandbox_id, *_args, **kwargs):
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox/generation-1",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

        def touch(self, _sandbox_id):
            return True

        def discover(self, sandbox_id):
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox/generation-2",
                generation="generation-2",
                workdir_id="workdir-2",
            )

    provider = _make_provider(FakeClient())
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.load_user_agent_env", lambda _uid: {})
    provider.acquire("root-thread", uid="user-1", workdir_id="workdir-1")
    connection = next(iter(provider._connections.values()))
    provider._last_touch_at[connection.cache_key] = 0

    with pytest.raises(SandboxIdentityMismatchError, match="changed"):
        provider.get("root-thread", uid="user-1", workdir_id="workdir-1")


def test_provider_rejects_rebinding_cached_runtime_to_another_workdir(monkeypatch) -> None:
    created: list[str | None] = []

    class FakeClient:
        def create(self, sandbox_id, *_args, **kwargs):
            created.append(kwargs["workdir_id"])
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

    provider = _make_provider(FakeClient())
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.load_user_agent_env", lambda _uid: {})
    provider.acquire("root-thread", uid="user-1", workdir_id="workdir-1")

    with pytest.raises(SandboxIdentityMismatchError, match="existing runtime scope"):
        provider.acquire("root-thread", uid="user-1", workdir_id="workdir-2")

    assert created == ["workdir-1"]


def test_provider_release_uses_cached_generation() -> None:
    deleted: list[tuple[str, str | None]] = []

    class FakeClient:
        def delete(self, sandbox_id, *, expected_generation=None):
            deleted.append((sandbox_id, expected_generation))

    provider = _make_provider(FakeClient())
    cache_key = "user-1::root-thread::root-thread"
    provider._connections[cache_key] = SimpleNamespace(
        sandbox_id="sandbox-1",
        uid="user-1",
        workdir_id="workdir-1",
        generation="generation-1",
    )

    provider.release("root-thread", uid="user-1", workdir_id="workdir-1")

    assert deleted == [("sandbox-1", "generation-1")]


def test_provider_uses_distinct_sandbox_scope_for_different_uid(monkeypatch) -> None:
    created = []

    class FakeClient:
        def create(self, sandbox_id, thread_id, uid, env, **kwargs):
            created.append((sandbox_id, thread_id, uid, env))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url=f"http://sandbox/{uid}",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

        def touch(self, _sandbox_id):
            return True

    provider = _make_provider(FakeClient())
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.load_user_agent_env", lambda uid: {"A": uid})

    sandbox_1 = provider.acquire(
        "child-thread",
        uid="user-1",
    )
    sandbox_2 = provider.acquire(
        "child-thread",
        uid="user-2",
    )

    assert sandbox_1 != sandbox_2
    assert created[0][2] == "user-1"
    assert created[1][2] == "user-2"


def test_provider_maps_external_uid_only_at_provisioner_filesystem_boundary(monkeypatch) -> None:
    from yuxi.agents.backends.sandbox.paths import workspace_uid_dirname

    calls = []

    class FakeClient:
        def create(self, sandbox_id, thread_id, uid, env, **kwargs):
            calls.append((uid, env))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

        def touch(self, _sandbox_id):
            return True

    provider = _make_provider(FakeClient())
    logical_uid = "oidc:12345678-1234-1234-1234-123456789abc"
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.load_user_agent_env", lambda uid: {"OWNER": uid})

    provider.acquire("thread-1", uid=logical_uid)

    assert calls == [(workspace_uid_dirname(logical_uid), {"OWNER": logical_uid})]
    assert calls[0][0].startswith("uid-")
    assert ":" not in calls[0][0]


def test_provider_get_create_if_missing_ensures_expected_runtime_scope(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def create(self, sandbox_id, thread_id, uid, env, **kwargs):
            calls.append((sandbox_id, thread_id, uid, env))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

        def discover(self, _sandbox_id):
            raise AssertionError("create_if_missing should ensure sandbox through provisioner create")

    provider = _make_provider(FakeClient())
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.load_user_agent_env", lambda uid: {"A": uid})

    connection = provider.get(
        "child-thread",
        uid="user-1",
        create_if_missing=True,
    )

    sandbox_id = sandbox_id_for_thread("child-thread", uid="user-1")
    assert connection.sandbox_id == sandbox_id
    assert calls == [
        (
            sandbox_id,
            "child-thread",
            "user-1",
            {"A": "user-1"},
        )
    ]


def test_provider_can_create_sandbox_without_environment(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def create(self, sandbox_id, thread_id, uid, env, **kwargs):
            calls.append((env, kwargs["inherit_env"]))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                sandbox_url="http://sandbox",
                generation="generation-1",
                workdir_id=kwargs["workdir_id"],
            )

    provider = _make_provider(FakeClient())
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.provider.load_user_agent_env",
        lambda _uid: pytest.fail("隔离 Sandbox 不应加载用户环境变量"),
    )

    provider.get("remote-skill-test", uid="remote-skill-test", create_if_missing=True, inherit_env=False)

    assert calls == [({}, False)]


def test_provisioner_uses_runtime_thread_and_instance(monkeypatch) -> None:
    provider_calls = []

    class FakeProvider:
        def get(self, thread_id, **kwargs):
            provider_calls.append((thread_id, kwargs))
            return SimpleNamespace(sandbox_url="http://sandbox")

    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: FakeProvider())

    backend = ProvisionerSandboxBackend(
        thread_id="child-thread",
        uid="user-1",
    )
    backend._build_client = MethodType(lambda self, sandbox_url: SimpleNamespace(url=sandbox_url), backend)

    client = backend._get_client()

    assert client.url == "http://sandbox"
    assert provider_calls == [
        (
            "child-thread",
            {
                "uid": "user-1",
                "create_if_missing": True,
                "inherit_env": True,
                "sandbox_instance_id": "child-thread",
                "workdir_id": None,
            },
        )
    ]


def test_provisioner_denies_reads_outside_allowed_roots(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    result = backend.read("/etc/passwd")

    assert result.error == "permission denied for read on '/etc/passwd'"


def test_provisioner_denies_upload_writes(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    write_result = backend.write("/home/gem/user-data/uploads/blocked.txt", "blocked")
    upload_result = backend.upload_files([("/home/gem/user-data/uploads/blocked.bin", b"blocked")])

    assert write_result.error and "permission denied" in write_result.error
    assert upload_result[0].error == "permission_denied"


def test_provisioner_trusted_hydrate_clears_and_writes_uploads(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    calls = []

    def _exec_command(**kwargs):
        calls.append(("clear", kwargs["command"]))
        return SimpleNamespace(data=SimpleNamespace(exit_code=0, output=""))

    def _upload_file(**kwargs):
        calls.append(("upload", kwargs["path"], kwargs["file"].read()))
        return SimpleNamespace(success=True, message="")

    backend._get_client = MethodType(
        lambda self: SimpleNamespace(
            shell=SimpleNamespace(exec_command=_exec_command),
            file=SimpleNamespace(upload_file=_upload_file),
        ),
        backend,
    )

    backend.clear_upload_files()
    backend.write_upload_file("/home/gem/user-data/uploads/file.bin", b"\x00\xff")
    backend.write_upload_file("/home/gem/user-data/uploads/attachments/file.md", b"content")

    assert [call[0] for call in calls] == ["clear", "upload", "clear", "upload", "clear"]
    assert calls[1][2] == b"\x00\xff"
    assert calls[3][2] == b"content"
    assert all(call[1].startswith("/tmp/.yuxi-hydrate-") for call in (calls[1], calls[3]))


def test_provisioner_trusted_hydrate_rejects_failed_or_outside_write(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    clear_calls = []

    def _exec_command(**_kwargs):
        clear_calls.append(True)
        return SimpleNamespace(data=SimpleNamespace(exit_code=0, output=""))

    backend._get_client = MethodType(
        lambda self: SimpleNamespace(
            shell=SimpleNamespace(exec_command=_exec_command),
            file=SimpleNamespace(upload_file=lambda **_kwargs: SimpleNamespace(success=False, message="write failed")),
        ),
        backend,
    )

    with pytest.raises(RuntimeError, match="write failed"):
        backend.write_upload_file("/home/gem/user-data/uploads/file.bin", b"content")
    assert clear_calls == []

    with pytest.raises(ValueError, match="must be under"):
        backend.write_upload_file("/home/gem/user-data/outputs/escape.bin", b"content")
    assert clear_calls == []


def test_provisioner_allows_outputs_writes(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    def _missing_file(path, offset=0, limit=None):
        raise FileNotFoundError

    monkeypatch.setattr(backend, "_read_binary", _missing_file)

    calls = []

    def _write_file(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(success=True, message="")

    fake_client = SimpleNamespace(file=SimpleNamespace(write_file=_write_file))
    backend._get_client = MethodType(lambda self: fake_client, backend)

    result = backend.write("/home/gem/user-data/outputs/report.md", "ok")

    assert result.error is None
    assert result.path == "/home/gem/user-data/outputs/report.md"
    assert calls[0]["file"] == "/home/gem/user-data/outputs/report.md"


def test_provisioner_glob_root_searches_readable_roots(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    calls = []

    def _find_files(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=SimpleNamespace(files=[f"{kwargs['path']}/match.md"]))

    fake_client = SimpleNamespace(file=SimpleNamespace(find_files=_find_files))
    backend._get_client = MethodType(lambda self: fake_client, backend)

    result = backend.glob("**/*.md")

    assert [call["path"] for call in calls] == ["/home/gem/user-data", "/home/gem/skills"]
    assert [item["path"] for item in result.matches] == [
        "/home/gem/skills/match.md",
        "/home/gem/user-data/match.md",
    ]


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        (None, b"SGVsbG8="),
        ("base64", b"Hello"),
    ],
)
def test_provisioner_read_binary_preserves_or_decodes_base64_content(monkeypatch, encoding, expected) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    fake_client = SimpleNamespace(
        file=SimpleNamespace(
            read_file=lambda **_kwargs: SimpleNamespace(data=SimpleNamespace(content="SGVsbG8=", encoding=encoding))
        )
    )
    backend._get_client = MethodType(lambda self: fake_client, backend)

    assert backend._read_binary("/home/gem/user-data/outputs/file.bin") == expected


def test_provisioner_read_file_base64_reads_temp_file_not_shell_output(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    expected = base64.b64encode(b"\x89PNG\r\n\x1a\nimage-bytes").decode("ascii")
    shell_calls = []

    def _exec_command(**kwargs):
        shell_calls.append(kwargs)
        return SimpleNamespace(
            data=SimpleNamespace(
                exit_code=0,
                output="broken\n[... Observation truncated due to length ...]\nbase64",
            )
        )

    def _read_file(**kwargs):
        assert kwargs["file"].startswith("/tmp/yuxi-read-file-")
        return SimpleNamespace(data=SimpleNamespace(content=expected))

    fake_client = SimpleNamespace(
        shell=SimpleNamespace(exec_command=_exec_command),
        file=SimpleNamespace(read_file=_read_file),
    )
    backend._get_client = MethodType(lambda self: fake_client, backend)

    result = backend._read_file_base64("/home/gem/user-data/workspace/image.png")

    assert result == expected
    assert len(shell_calls) == 2
    assert shell_calls[0]["command"].startswith("python3 -c")
    assert shell_calls[1]["command"].startswith("rm -f /tmp/yuxi-read-file-")


@pytest.mark.parametrize(
    ("path", "base64_content"),
    [
        ("/home/gem/user-data/image.png", "iVBORw0KGgo="),
        ("/home/gem/user-data/image.gif", "R0lGODlh"),
    ],
)
def test_provisioner_read_treats_image_files_as_base64(monkeypatch, path, base64_content) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    monkeypatch.setattr(backend, "_file_size_bytes", lambda _path: 6)
    monkeypatch.setattr(backend, "_read_binary", lambda path, offset=0, limit=None: pytest.fail("file API used"))
    monkeypatch.setattr(backend, "_read_file_base64", lambda _path: base64_content)

    result = backend.read(path)

    assert result.file_data == {"content": base64_content, "encoding": "base64"}


def test_provisioner_read_rejects_large_known_binary_before_read(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    read_calls: list[tuple[str, int, int | None]] = []
    monkeypatch.setattr(backend, "_file_size_bytes", lambda _path: MAX_BINARY_BYTES + 1)

    def _read_binary(path, offset=0, limit=None):
        read_calls.append((path, offset, limit))
        return b"\x89PNG\r\n\x1a\n"

    monkeypatch.setattr(backend, "_read_binary", _read_binary)
    monkeypatch.setattr(backend, "_read_file_base64", lambda _path: pytest.fail("binary file was read"))

    result = backend.read("/home/gem/user-data/large.png")

    assert result.file_data is None
    assert result.error == f"Binary file exceeds maximum preview size of {MAX_BINARY_BYTES} bytes"
    assert read_calls == []


def test_provisioner_read_rejects_unknown_binary(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    read_calls: list[tuple[str, int, int | None]] = []

    def _read_binary(path, offset=0, limit=None):
        read_calls.append((path, offset, limit))
        return b"\x00binary prefix"

    monkeypatch.setattr(backend, "_read_binary", _read_binary)

    result = backend.read("/home/gem/user-data/large.unknown")

    assert result.file_data is None
    assert result.error == "read_file only supports UTF-8 text and image files. This file type is not supported."
    assert read_calls == [("/home/gem/user-data/large.unknown", 0, 2000)]


def test_provisioner_read_rejects_unknown_file_on_sandbox_utf8_decode_failure(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    def _read_binary_raises(path, offset=0, limit=None):
        raise RuntimeError("'utf-8' codec can't decode byte 0x89 in position 0")

    monkeypatch.setattr(backend, "_read_binary", _read_binary_raises)

    result = backend.read("/home/gem/user-data/workspace/uploaded.bin")

    assert result.file_data is None
    assert result.error == "read_file only supports UTF-8 text and image files. This file type is not supported."


@pytest.mark.parametrize("extension", ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx"])
def test_provisioner_read_routes_documents_to_ocr(monkeypatch, extension: str) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    monkeypatch.setattr(backend, "_file_size_bytes", lambda _path: 8)
    monkeypatch.setattr(backend, "_read_binary", lambda *_args, **_kwargs: pytest.fail("document was read"))

    result = backend.read(f"/home/gem/user-data/uploads/document.{extension}")

    assert result.file_data is None
    assert result.error == (
        "read_file does not support PDF or Office documents. Use ocr_parse_file to convert the file to Markdown first."
    )


@pytest.mark.parametrize("extension", ["mp3", "mp4", "wav"])
def test_provisioner_read_rejects_other_known_modalities(monkeypatch, extension: str) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    monkeypatch.setattr(backend, "_file_size_bytes", lambda _path: 8)
    monkeypatch.setattr(backend, "_read_file_base64", lambda _path: pytest.fail("binary file was read"))

    result = backend.read(f"/home/gem/user-data/uploads/media.{extension}")

    assert result.file_data is None
    assert result.error == "read_file only supports UTF-8 text and image files. This file type is not supported."


def test_read_file_tool_returns_multimodal_block_for_small_binary() -> None:
    class _Backend:
        def read(self, path: str, offset: int = 0, limit: int = 100):
            return ReadResult(file_data={"content": "R0lGODlh", "encoding": "base64"})

    middleware = create_agent_filesystem_middleware(tool_token_limit_before_evict=None)
    middleware.backend = _Backend()
    read_tool = next(tool for tool in middleware.tools if tool.name == "read_file")
    runtime = ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-read",
        store=None,
    )

    result = read_tool.func(file_path="/home/gem/user-data/uploads/image.gif", runtime=runtime)

    assert result.status == "success"
    assert result.content_blocks == [{"type": "image", "base64": "R0lGODlh", "mime_type": "image/gif"}]
    assert result.additional_kwargs == {
        "read_file_path": "/home/gem/user-data/uploads/image.gif",
        "read_file_media_type": "image/gif",
    }


def test_provisioner_read_reports_invalid_path(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    result = backend.read("secret.txt")

    assert result.error == "Invalid path 'secret.txt': path must start with /"


def test_provisioner_read_reports_path_traversal(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    result = backend.read("/home/gem/user-data/../secret.txt")

    assert result.error == "Invalid path '/home/gem/user-data/../secret.txt': path traversal is not allowed"


def test_provisioner_download_files_distinguishes_invalid_path_from_read_failure(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    def download_file(**_kwargs):
        raise RuntimeError("sandbox read timeout")

    backend._get_client = MethodType(
        lambda self: SimpleNamespace(file=SimpleNamespace(download_file=download_file)),
        backend,
    )

    responses = backend.download_files(["bad-path", "/home/gem/user-data/read-failed"])

    assert responses[0].error == "invalid_path"
    assert responses[1].error.startswith("read_failed")


def test_provisioner_download_files_treats_sandbox_404_as_missing(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    def download_file(**_kwargs):
        raise RuntimeError("status_code: 404, body: {'message': 'File does not exist'}")

    backend._get_client = MethodType(
        lambda self: SimpleNamespace(file=SimpleNamespace(download_file=download_file)),
        backend,
    )

    responses = backend.download_files(["/home/gem/user-data/outputs/missing.md"])

    assert responses[0].error == "file_not_found"


def test_provisioner_execute_returns_error_response_on_client_failure(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")

    class _FakeClient:
        class shell:
            @staticmethod
            def exec_command(**kwargs):
                raise RuntimeError("boom")

    backend._get_client = MethodType(lambda self: _FakeClient(), backend)
    result = backend.execute("echo hi")

    assert result.exit_code == 1
    assert "Error:" in result.output


def test_provisioner_execute_applies_timeout_to_command_and_http_request(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    calls: list[dict] = []

    def execute(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=SimpleNamespace(exit_code=0, output="done"))

    fake_client = SimpleNamespace(shell=SimpleNamespace(exec_command=execute))
    backend._get_client = MethodType(lambda self: fake_client, backend)

    result = backend.execute("echo hi", timeout=300)

    assert result.exit_code == 0
    assert calls == [
        {
            "command": "echo hi",
            "timeout": 300,
            "hard_timeout": 300,
            "request_options": {"timeout_in_seconds": 300},
        }
    ]


def test_provisioner_download_files_streams_binary_bytes(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    calls: list[dict] = []

    def download_file(**kwargs):
        calls.append(kwargs)
        return iter([b"\x00\xff", b"binary"])

    backend._get_client = MethodType(
        lambda self: SimpleNamespace(file=SimpleNamespace(download_file=download_file)),
        backend,
    )

    response = backend.download_files(["/home/gem/user-data/outputs/demo.bin"])[0]

    assert response.content == b"\x00\xffbinary"
    assert calls == [
        {
            "path": "/home/gem/user-data/outputs/demo.bin",
            "request_options": {"timeout_in_seconds": backend._command_timeout_seconds},
        }
    ]


def test_trusted_output_listing_rejects_symlink_or_scope_escape(monkeypatch) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    backend._get_client = MethodType(
        lambda self: SimpleNamespace(
            file=SimpleNamespace(
                list_path=lambda **_kwargs: SimpleNamespace(
                    data=SimpleNamespace(
                        files=[
                            SimpleNamespace(
                                path="/home/gem/user-data/outputs/escape.txt",
                                is_directory=False,
                            )
                        ]
                    )
                )
            )
        ),
        backend,
    )
    monkeypatch.setattr(
        backend,
        "execute",
        lambda _command: SimpleNamespace(exit_code=2, output="not regular", truncated=False),
    )

    with pytest.raises(ValueError, match="not a regular scoped file"):
        backend.list_output_files()


def test_output_download_enforces_limit_during_actual_transfer(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    content = b"12345678"
    execute_calls = 0

    def execute(_command):
        nonlocal execute_calls
        execute_calls += 1
        return SimpleNamespace(
            exit_code=0,
            output=f"YUXI_OUTPUT_SNAPSHOT {len(content)} {hashlib.sha256(content).hexdigest()}",
            truncated=False,
        )

    backend.execute = execute
    backend._get_client = MethodType(
        lambda self: SimpleNamespace(
            file=SimpleNamespace(download_file=lambda **_kwargs: iter([content[:4], content[4:]]))
        ),
        backend,
    )
    target_path = tmp_path / "snapshot.bin"

    with pytest.raises(ValueError, match="exceeds transfer limit"):
        backend.download_output_file_to_path(
            "/home/gem/user-data/outputs/growing.bin",
            str(target_path),
            max_bytes=5,
        )

    assert not target_path.exists()
    assert execute_calls == 2


def test_output_snapshot_never_creates_missing_sandbox(monkeypatch) -> None:
    calls = []

    class FakeProvider:
        def get(self, *_args, **kwargs):
            calls.append(kwargs)
            return None

    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", FakeProvider)
    backend = ProvisionerSandboxBackend(
        thread_id="thread-1",
        uid="user-1",
        create_if_missing=False,
    )

    with pytest.raises(RuntimeError, match="sandbox is unavailable"):
        backend.list_output_files()

    assert calls[0]["create_if_missing"] is False


def test_output_snapshot_attempts_cleanup_after_snapshot_command_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    execute_results = iter(
        [
            SimpleNamespace(exit_code=1, output="connection lost", truncated=False),
            SimpleNamespace(exit_code=0, output="", truncated=False),
        ]
    )
    backend.execute = lambda _command: next(execute_results)

    with pytest.raises(ValueError, match="snapshot failed"):
        backend.download_output_file_to_path(
            "/home/gem/user-data/outputs/report.txt",
            str(tmp_path / "report.txt"),
            max_bytes=1024,
        )

    with pytest.raises(StopIteration):
        next(execute_results)


def test_output_snapshot_cleanup_failure_blocks_publication(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("yuxi.agents.backends.sandbox.backend.get_sandbox_provider", lambda: object())
    backend = ProvisionerSandboxBackend(thread_id="thread-1", uid="user-1")
    content = b"report"
    execute_results = iter(
        [
            SimpleNamespace(
                exit_code=0,
                output=f"YUXI_OUTPUT_SNAPSHOT {len(content)} {hashlib.sha256(content).hexdigest()}",
                truncated=False,
            ),
            SimpleNamespace(exit_code=1, output="cleanup failed", truncated=False),
        ]
    )
    backend.execute = lambda _command: next(execute_results)
    backend._get_client = MethodType(
        lambda self: SimpleNamespace(file=SimpleNamespace(download_file=lambda **_kwargs: iter([content]))),
        backend,
    )
    target = tmp_path / "report.txt"

    with pytest.raises(RuntimeError, match="cleanup failed"):
        backend.download_output_file_to_path(
            "/home/gem/user-data/outputs/report.txt",
            str(target),
            max_bytes=1024,
        )

    assert not target.exists()


def test_project_workdir_paths_use_one_safe_opaque_segment(monkeypatch, tmp_path) -> None:
    from yuxi.agents.backends.sandbox import paths

    monkeypatch.setattr(paths, "get_save_dir", lambda: tmp_path)

    assert paths.project_workdir_virtual_dir("workdir-1") == "/home/gem/projects/project-workdir-1"
    assert paths.project_workdir_host_dir("workdir-1") == tmp_path / "projects" / "workdir-1"

    for unsafe in ("../escape", "nested/workdir", "workdir name", "workdir.1"):
        with pytest.raises(ValueError, match="invalid characters"):
            paths.project_workdir_virtual_dir(unsafe)
