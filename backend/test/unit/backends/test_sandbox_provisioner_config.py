from __future__ import annotations

import importlib.util
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient


MODULE_NAME = "sandbox_provisioner_app_for_test"


def _find_module_path() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "docker" / "sandbox_provisioner" / "app.py"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("docker/sandbox_provisioner/app.py not found from test path")


MODULE_PATH = _find_module_path()


def _load_module():
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _docker_backend(module, tmp_path, run_container):
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._lock = threading.RLock()
    backend._container_port = 8080
    backend._network_prefix = "yuxi-know-sandbox"
    backend._sandbox_image = "sandbox-image"
    backend._container_prefix = "yuxi-sandbox"
    backend._sandbox_env = {}
    backend._health_timeout_seconds = 1
    backend._threads_host_path = str(tmp_path)
    backend._client = SimpleNamespace(containers=SimpleNamespace(run=run_container))
    return backend


def _docker_backend_with_running_container(monkeypatch, tmp_path):
    module = _load_module()
    captured = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        labels = {"thread-id": "thread-1"}
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(
        module,
        tmp_path,
        lambda image, **kwargs: captured.append((image, kwargs)) or FakeContainer(),
    )
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: None)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda *_args: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: True)
    return module, backend, captured


def test_canonical_backend_name(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    assert module.canonical_backend_name("docker") == "docker"
    assert module.canonical_backend_name("kubernetes") == "kubernetes"


def test_merged_sandbox_env_user_values_override_global(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    assert module.merged_sandbox_env(
        {"SHARED": "global", "GLOBAL_ONLY": "value"},
        {"SHARED": "user", "USER_ONLY": "value"},
    ) == {
        "SHARED": "user",
        "GLOBAL_ONLY": "value",
        "USER_ONLY": "value",
    }


def test_normalize_env_converts_values_to_strings(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    assert module.normalize_env({"A": 1, "B": None, "": "ignored"}) == {"A": "1", "B": ""}


def test_local_container_identity_validation_rejects_unsafe_path_segments(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend_cls = module.LocalContainerProvisionerBackend

    assert backend_cls._validate_thread_id("thread-1_2") == "thread-1_2"
    assert backend_cls._validate_uid("user-1_2") == "user-1_2"
    assert backend_cls._validate_workdir_id("workdir-1_2") == "workdir-1_2"

    for value in ["../escape", "thread/name", "thread name", "thread;rm", "thread.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_thread_id(value)

    for value in ["../user", "user/name", "user name", "user;rm", "user.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_uid(value)

    for value in ["../workdir", "workdir/name", "workdir name", "workdir;rm", "workdir.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_workdir_id(value)


def test_memory_backend_accepts_runtime_thread_id(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = module.MemoryProvisionerBackend()

    record = backend.create(
        "sandbox-1",
        "child-thread",
        "user-1",
    )

    assert record.sandbox_id == "sandbox-1"
    assert backend.discover("sandbox-1") is record


def test_memory_backend_concurrent_get_or_create_returns_one_generation(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = module.MemoryProvisionerBackend()

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(
            executor.map(
                lambda _index: backend.create(
                    "sandbox-shared",
                    "root-thread",
                    "user-1",
                    workdir_id="workdir-1",
                ),
                range(32),
            )
        )

    assert len({id(record) for record in records}) == 1
    assert len({record.generation for record in records}) == 1
    assert records[0].workdir_id == "workdir-1"

    with pytest.raises(ValueError, match="does not match"):
        backend.create("sandbox-shared", "root-thread", "user-1", workdir_id="workdir-2")

    with pytest.raises(module.SandboxGenerationMismatchError, match="generation"):
        backend.delete("sandbox-shared", expected_generation="stale-generation")
    assert backend.discover("sandbox-shared") is records[0]

    backend.delete("sandbox-shared", expected_generation=records[0].generation)
    assert backend.discover("sandbox-shared") is None


def test_idle_reaper_does_not_delete_or_forget_new_generation(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeBackend:
        def __init__(self):
            self.generation = "generation-2"
            self.deleted = []

        def delete(self, sandbox_id, *, expected_generation=None):
            self.deleted.append((sandbox_id, expected_generation))
            if expected_generation != self.generation:
                raise module.SandboxGenerationMismatchError("stale generation")

    backend = FakeBackend()
    reaper = module.SandboxIdleReaper(backend)
    reaper.touch("sandbox-1", generation="generation-1")
    reaper._last_activity_at["sandbox-1"] = ("generation-1", 0)
    expired = reaper._collect_expired_sandboxes()
    reaper.touch("sandbox-1", generation="generation-2")

    reaper._delete_expired_sandbox(*expired[0])

    assert backend.deleted == []
    assert reaper._last_activity_at["sandbox-1"][0] == "generation-2"


def test_operation_pins_drain_started_requests_and_block_new_requests_during_delete(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    pins = module.SandboxOperationPins()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    next_request_started = threading.Event()

    pins.acquire("sandbox-1")

    def delete_generation():
        pins.begin_delete("sandbox-1")
        delete_started.set()
        assert not next_request_started.is_set()
        pins.end_delete("sandbox-1")
        delete_finished.set()

    delete_thread = threading.Thread(target=delete_generation)
    delete_thread.start()
    with pins._condition:
        assert pins._condition.wait_for(lambda: "sandbox-1" in pins._deleting, timeout=1)
    assert not delete_started.is_set()

    def next_request():
        pins.acquire("sandbox-1")
        next_request_started.set()
        pins.release("sandbox-1")

    next_thread = threading.Thread(target=next_request)
    next_thread.start()
    assert not next_request_started.wait(timeout=0.05)

    pins.release("sandbox-1")
    assert delete_finished.wait(timeout=1)
    assert next_request_started.wait(timeout=1)
    delete_thread.join(timeout=1)
    next_thread.join(timeout=1)


def test_docker_mount_checks_reject_uploads_and_outputs_mounts(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._threads_host_path = str(tmp_path)

    workspace = tmp_path / "shared" / "user-1" / "workspace"
    skills = tmp_path.parent / "skill-projections" / "user-1"
    container = SimpleNamespace(
        attrs={
            "Mounts": [
                {"Destination": "/home/gem/user-data/workspace", "Source": str(workspace)},
                {"Destination": "/home/gem/skills", "Source": str(skills), "RW": False, "Mode": "ro"},
            ]
        }
    )

    assert backend._has_expected_user_data_mounts(container, "user-1") is True
    assert backend._is_expected_skills_mount(container, "user-1") is True
    assert backend._is_expected_skills_mount(container, "user-2") is False

    container.attrs["Mounts"][1]["RW"] = True
    assert backend._is_expected_skills_mount(container, "user-1") is False
    container.attrs["Mounts"][1]["RW"] = False

    container.attrs["Mounts"].append(
        {"Destination": "/home/gem/user-data/outputs", "Source": str(tmp_path / "legacy-outputs")}
    )
    assert backend._has_expected_user_data_mounts(container, "user-1") is False
    container.attrs["Mounts"].pop()

    container.attrs["Mounts"].append(
        {"Destination": "/home/gem/user-data/uploads", "Source": str(tmp_path / "legacy-uploads")}
    )
    assert backend._has_expected_user_data_mounts(container, "user-1") is False


def test_docker_sandbox_mounts_shared_workspace_without_thread_history_projection(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)

    backend.create("sandbox-1", "thread-1", "user-1")

    volumes = captured[0][1]["volumes"]
    destinations = {mount["bind"] for mount in volumes.values()}
    assert destinations == {
        "/home/gem/user-data/workspace",
        "/home/gem/skills",
    }
    assert all("/agents/chats" not in destination for destination in destinations)


def test_docker_project_workdir_contract_mounts_shared_posix_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda *_args: None)

    record = backend.create("sandbox-project", "root-thread", "user-1", workdir_id="workdir-1")

    run_kwargs = captured[0][1]
    destinations = {mount["bind"] for mount in run_kwargs["volumes"].values()}
    assert destinations == {
        "/home/gem/user-data",
        "/home/gem/projects/project-workdir-1",
        "/home/gem/skills",
    }
    assert run_kwargs["working_dir"] == "/home/gem/projects/project-workdir-1"
    assert run_kwargs["labels"]["workdir-id"] == "workdir-1"
    assert record.workdir_id is None  # Fake container has no Docker labels; real discover reads the label.


def test_docker_rejects_rebinding_existing_runtime_to_another_workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeContainer:
        status = "running"
        labels = {"thread-id": "root-thread", "workdir-id": "workdir-1"}
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: pytest.fail("sandbox was recreated"))
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: FakeContainer())

    with pytest.raises(ValueError, match="workdir identity"):
        backend.create("sandbox-project", "root-thread", "user-1", workdir_id="workdir-2")


def test_kubernetes_mount_check_rejects_uploads_and_outputs_mounts(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    pod = SimpleNamespace(
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    name="sandbox",
                    volume_mounts=[
                        SimpleNamespace(
                            mount_path="/home/gem/user-data/workspace",
                            sub_path="threads/shared/user-1/workspace",
                        ),
                        SimpleNamespace(
                            mount_path="/home/gem/skills",
                            sub_path="skill-projections/user-1",
                            read_only=True,
                        ),
                    ],
                )
            ]
        )
    )

    assert module.KubernetesProvisionerBackend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )
    pod.spec.containers[0].volume_mounts[1].read_only = False
    assert not module.KubernetesProvisionerBackend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )
    pod.spec.containers[0].volume_mounts[1].read_only = True
    pod.spec.containers[0].volume_mounts.append(
        SimpleNamespace(
            mount_path="/home/gem/user-data/outputs",
            sub_path="threads/parent-thread/user-data/outputs",
        )
    )
    assert not module.KubernetesProvisionerBackend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )
    pod.spec.containers[0].volume_mounts.pop()

    pod.spec.containers[0].volume_mounts.append(
        SimpleNamespace(
            mount_path="/home/gem/user-data/uploads",
            sub_path="threads/parent-thread/user-data/uploads",
        )
    )
    assert not module.KubernetesProvisionerBackend._pod_has_expected_mounts(
        pod,
        uid="user-1",
    )


def test_management_api_requires_bearer_token(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    module = _load_module()

    with TestClient(module.app) as client:
        assert client.get("/api/sandboxes").status_code == 401
        assert client.get("/api/sandboxes", headers={"Authorization": "Bearer wrong"}).status_code == 401

        response = client.get(
            "/api/sandboxes",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"sandboxes": [], "count": 0}


def test_authenticated_management_api_returns_proxied_sandbox_url(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    monkeypatch.setenv("PROVISIONER_PUBLIC_URL", "http://sandbox-provisioner:8002")
    module = _load_module()
    headers = {"Authorization": f"Bearer {token}"}
    sandbox_id = "sandbox-auth-test"

    with TestClient(module.app) as client:
        create_response = client.post(
            "/api/sandboxes",
            headers=headers,
            json={
                "sandbox_id": sandbox_id,
                "thread_id": "thread-1",
                "uid": "user-1",
            },
        )
        list_response = client.get("/api/sandboxes", headers=headers)
        stale_delete_response = client.delete(
            f"/api/sandboxes/{sandbox_id}",
            headers=headers,
            params={"expected_generation": "stale-generation"},
        )
        delete_response = client.delete(
            f"/api/sandboxes/{sandbox_id}",
            headers=headers,
            params={"expected_generation": create_response.json()["generation"]},
        )

    expected_url = f"http://sandbox-provisioner:8002/api/sandboxes/{sandbox_id}/proxy"
    assert create_response.status_code == 200
    assert create_response.json()["sandbox_url"] == expected_url
    assert list_response.status_code == 200
    sandboxes = list_response.json()["sandboxes"]
    assert len(sandboxes) == 1
    assert sandboxes[0]["sandbox_id"] == sandbox_id
    assert sandboxes[0]["sandbox_url"] == expected_url
    assert sandboxes[0]["status"] == "Running"
    assert sandboxes[0]["generation"]
    assert sandboxes[0]["workdir_id"] is None
    assert stale_delete_response.status_code == 409
    assert delete_response.status_code == 200


def test_create_sandbox_forwards_environment_policy(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    calls = []

    def create(*_args, **kwargs):
        calls.append(kwargs)
        return module.SandboxRecord(sandbox_id="sandbox-1", sandbox_url="http://sandbox", status="Running")

    monkeypatch.setattr(module, "backend_impl", SimpleNamespace(create=create))
    monkeypatch.setattr(module.idle_reaper, "touch", lambda _sandbox_id, **_kwargs: None)

    module.create_sandbox(
        module.CreateSandboxRequest(
            sandbox_id="sandbox-1",
            thread_id="thread-1",
            uid="user-1",
            inherit_env=False,
        )
    )

    assert calls[0]["inherit_env"] is False


def test_authenticated_proxy_forwards_request_without_management_token(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    module = _load_module()
    headers = {"Authorization": f"Bearer {token}"}
    captured = []

    def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"X-Ignored": "value"})

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(upstream)

    clients = []

    def create_client(**kwargs):
        client = real_async_client(transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        create_client,
    )

    with TestClient(module.app) as client:
        client.post(
            "/api/sandboxes",
            headers=headers,
            json={
                "sandbox_id": "sandbox-proxy-test",
                "thread_id": "thread-1",
                "uid": "user-1",
            },
        )
        response = client.get(
            "/api/sandboxes/sandbox-proxy-test/proxy/v1/sandbox",
            headers=headers,
            params={"detail": "full"},
        )
        second_response = client.get(
            "/api/sandboxes/sandbox-proxy-test/proxy/v1/sandbox",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert second_response.status_code == 200
    assert len(clients) == 1
    assert clients[0].is_closed
    assert str(captured[0].url) == "http://agent-sandbox:8000/v1/sandbox?detail=full"
    assert str(captured[1].url) == "http://agent-sandbox:8000/v1/sandbox"
    assert "authorization" not in captured[0].headers
    assert "x-ignored" not in response.headers


@pytest.mark.asyncio
async def test_proxy_discovers_sandbox_outside_event_loop_thread(monkeypatch):
    token = "test-provisioner-token-that-is-long-enough"
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", token)
    module = _load_module()
    event_loop_thread = threading.get_ident()
    discover_threads = []

    def discover(sandbox_id):
        discover_threads.append(threading.get_ident())
        return module.SandboxRecord(
            sandbox_id=sandbox_id,
            sandbox_url="http://agent-sandbox:8000",
            status="Running",
        )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    http_client = real_async_client(transport=transport, timeout=None, follow_redirects=False, trust_env=False)
    module.app.state.http_client = http_client
    monkeypatch.setattr(module, "backend_impl", SimpleNamespace(discover=discover))
    request = module.Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/sandboxes/sandbox-proxy-test/proxy/v1/sandbox",
            "headers": [],
            "query_string": b"",
            "app": module.app,
        },
        receive,
    )

    try:
        response = await module.proxy_sandbox_request("sandbox-proxy-test", request, "v1/sandbox")
        body = b"".join([chunk async for chunk in response.body_iterator])
    finally:
        await http_client.aclose()

    assert body == b'{"ok":true}'
    assert discover_threads and discover_threads[0] != event_loop_thread


def test_docker_backend_uses_private_network_without_published_port(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    _, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)

    record = backend.create("sandbox-1", "thread-1", "user-1")

    assert record.sandbox_url == "http://yuxi-sandbox-sandbox-1:8080"
    assert captured[0][0] == "sandbox-image"
    assert captured[0][1]["network"] == "yuxi-know-sandbox-sandbox-1"
    assert "ports" not in captured[0][1]


def test_docker_backend_can_disable_sandbox_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    _, backend, captured = _docker_backend_with_running_container(monkeypatch, tmp_path)
    backend._sandbox_env = {"GLOBAL_SECRET": "value"}

    backend.create("sandbox-1", "thread-1", "user-1", {"USER_SECRET": "value"}, inherit_env=False)

    assert "environment" not in captured[0][1]


def test_kubernetes_sandbox_disables_service_account_token_and_environment(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeKubernetesClient:
        def __getattr__(self, _name):
            return lambda *_args, **kwargs: SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._client = FakeKubernetesClient()
    backend._sandbox_image = "sandbox-image"
    backend._container_port = 8080
    backend._thread_pvc = "threads"
    backend._sandbox_env = {"GLOBAL_SECRET": "value"}

    pod = backend._build_pod_spec(
        "sandbox-1",
        "thread-1",
        "user-1",
        {"USER_SECRET": "value"},
        inherit_env=False,
    )

    assert pod.spec.automount_service_account_token is False
    assert pod.spec.containers[0].env == []
    sandbox_mounts = {mount.mount_path for mount in pod.spec.containers[0].volume_mounts}
    assert "/home/gem/user-data/uploads" not in sandbox_mounts
    assert "/home/gem/user-data/uploads" in pod.spec.init_containers[0].args[0]


def test_kubernetes_project_workdir_contract_uses_rwx_subpaths(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()

    class FakeKubernetesClient:
        def __getattr__(self, _name):
            return lambda *_args, **kwargs: SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._client = FakeKubernetesClient()
    backend._sandbox_image = "sandbox-image"
    backend._container_port = 8080
    backend._thread_pvc = "threads-rwx"
    backend._sandbox_env = {}

    pod = backend._build_pod_spec(
        "sandbox-1",
        "root-thread",
        "user-1",
        {},
        inherit_env=False,
        workdir_id="workdir-1",
    )

    sandbox = pod.spec.containers[0]
    mounts = {mount.mount_path: getattr(mount, "sub_path", None) for mount in sandbox.volume_mounts}
    assert sandbox.working_dir == "/home/gem/projects/project-workdir-1"
    assert mounts["/home/gem/projects/project-workdir-1"] == "projects/workdir-1"
    assert mounts["/home/gem/user-data"] == "threads/shared/user-1"
    assert mounts["/home/gem/skills"] == "skill-projections/user-1"
    skills_mount = next(mount for mount in sandbox.volume_mounts if mount.mount_path == "/home/gem/skills")
    assert skills_mount.read_only is True
    assert pod.metadata.annotations["workdir-id"] == "workdir-1"


def test_kubernetes_rejects_rebinding_existing_runtime_to_another_workdir(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._lock = threading.Lock()
    backend._core_api = SimpleNamespace()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        pass

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)
    monkeypatch.setattr(
        backend,
        "discover",
        lambda _sandbox_id: module.SandboxRecord(
            sandbox_id="sandbox-1",
            sandbox_url="http://sandbox",
            generation="generation-1",
            workdir_id="workdir-1",
        ),
    )
    monkeypatch.setattr(backend, "_discovered_matches_request", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(backend, "delete", lambda *_args, **_kwargs: pytest.fail("sandbox was deleted"))

    with pytest.raises(ValueError, match="identity does not match"):
        backend.create("sandbox-1", "root-thread", "user-1", workdir_id="workdir-2")


def test_kubernetes_pod_conflict_is_revalidated_before_creating_service(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        def __init__(self, status=None):
            self.status = status

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)

    class FakeCoreApi:
        def create_namespaced_pod(self, **_kwargs):
            raise ApiException(status=409)

        def create_namespaced_service(self, **_kwargs):
            pytest.fail("service was created before pod identity validation")

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._lock = threading.RLock()
    backend._core_api = FakeCoreApi()
    backend._namespace = "yuxi"
    backend._pod_name = lambda _sandbox_id: "pod-1"
    backend._service_name = lambda _sandbox_id: "service-1"
    backend.discover = lambda _sandbox_id: None
    backend._build_pod_spec = lambda *_args, **_kwargs: SimpleNamespace()
    backend._discovered_matches_request = lambda *_args, **_kwargs: False

    with pytest.raises(ValueError, match="identity does not match"):
        backend.create("sandbox-1", "root-thread", "user-1", workdir_id="workdir-2")


def test_kubernetes_delete_uses_uid_generation_precondition(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    kubernetes_module = ModuleType("kubernetes")
    client_module = ModuleType("kubernetes.client")
    rest_module = ModuleType("kubernetes.client.rest")

    class ApiException(Exception):
        def __init__(self, status=None):
            self.status = status

    rest_module.ApiException = ApiException
    client_module.rest = rest_module
    kubernetes_module.client = client_module
    monkeypatch.setitem(sys.modules, "kubernetes", kubernetes_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_module)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_module)

    calls = []

    class FakeCoreApi:
        def delete_namespaced_pod(self, **kwargs):
            calls.append(("pod", kwargs))

        def delete_namespaced_service(self, **kwargs):
            calls.append(("service", kwargs))

    class FakeKubernetesClient:
        @staticmethod
        def V1Preconditions(**kwargs):
            return SimpleNamespace(**kwargs)

        @staticmethod
        def V1DeleteOptions(**kwargs):
            return SimpleNamespace(**kwargs)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._core_api = FakeCoreApi()
    backend._client = FakeKubernetesClient()
    backend._lock = threading.RLock()
    backend._namespace = "yuxi"
    backend._pod_name = lambda _sandbox_id: "pod-1"
    backend._service_name = lambda _sandbox_id: "service-1"

    backend.delete("sandbox-1", expected_generation="pod-uid-1")

    assert [kind for kind, _kwargs in calls] == ["pod", "service"]
    assert calls[0][1]["body"].preconditions.uid == "pod-uid-1"


@pytest.mark.parametrize(
    ("start_succeeds", "error_match"),
    [
        (True, "is not ready"),
        (False, "container start failed"),
    ],
)
def test_docker_backend_cleans_up_sandbox_and_network_on_failure(monkeypatch, tmp_path, start_succeeds, error_match):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    created_container = None
    deleted_networks = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        labels = {"thread-id": "thread-1"}
        attrs = {"State": {"Status": "running"}}
        removed = False

        def reload(self):
            return None

        def stop(self, timeout):
            assert timeout == 10
            self.status = "exited"

        def remove(self, *, v, force):
            assert v is True
            assert force is True
            self.removed = True

    def run_container(_image, **_kwargs):
        nonlocal created_container
        if not start_succeeds:
            raise RuntimeError("container start failed")
        created_container = FakeContainer()
        return created_container

    backend = _docker_backend(module, tmp_path, run_container)
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: created_container)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_delete_network", deleted_networks.append)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda *_args: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: False)

    with pytest.raises(RuntimeError, match=error_match):
        backend.create("sandbox-1", "thread-1", "user-1")

    assert deleted_networks == ["sandbox-1"]
    if start_succeeds:
        assert created_container is not None
        assert created_container.removed is True


def test_docker_backend_assigns_each_sandbox_a_distinct_network(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = object.__new__(module.LocalContainerProvisionerBackend)
    backend._network_prefix = "yuxi-know-sandbox"

    first_network = backend._network_name("sandbox-1")
    second_network = backend._network_name("sandbox-2")

    assert first_network == "yuxi-know-sandbox-sandbox-1"
    assert second_network == "yuxi-know-sandbox-sandbox-2"
    assert first_network != second_network
    assert backend._is_on_expected_network(
        SimpleNamespace(attrs={"NetworkSettings": {"Networks": {first_network: {}}}}),
        "sandbox-1",
    )
    assert not backend._is_on_expected_network(
        SimpleNamespace(attrs={"NetworkSettings": {"Networks": {first_network: {}, second_network: {}}}}),
        "sandbox-1",
    )


def test_docker_backend_reconnects_provisioner_before_reusing_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setitem(sys.modules, "docker.errors", SimpleNamespace(NotFound=RuntimeError))
    module = _load_module()
    connected = []

    class FakeNetwork:
        name = "yuxi-know-sandbox-sandbox-1"
        attrs = {
            "Labels": {"managed-by": "yuxi-sandbox-provisioner", "sandbox-id": "sandbox-1"},
            "Containers": {},
        }

        def reload(self):
            return None

        def connect(self, container, aliases):
            connected.append((container.id, aliases))

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        labels = {"thread-id": "thread-1"}
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: pytest.fail("sandbox was recreated"))
    backend._client.networks = SimpleNamespace(get=lambda _name: FakeNetwork())
    backend._provisioner_container = SimpleNamespace(id="provisioner-id")
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: FakeContainer())
    monkeypatch.setattr(backend, "_is_expected_skills_mount", lambda _container, _uid: True)
    monkeypatch.setattr(backend, "_is_on_expected_network", lambda _container, _sandbox_id: True)
    monkeypatch.setattr(backend, "_has_expected_user_data_mounts", lambda _container, _uid, _workdir=None: True)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda *_args: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: bool(connected))

    record = backend.create("sandbox-1", "thread-1", "user-1")

    assert record.sandbox_url == "http://yuxi-sandbox-sandbox-1:8080"
    assert connected == [("provisioner-id", ["sandbox-provisioner"])]


def test_docker_backend_does_not_remove_unowned_network(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    monkeypatch.setitem(sys.modules, "docker.errors", SimpleNamespace(NotFound=RuntimeError))
    module = _load_module()
    disconnected = []
    removed = []

    class FakeNetwork:
        name = "yuxi-know-sandbox-sandbox-1"
        attrs = {
            "Labels": {"managed-by": "operator", "sandbox-id": "sandbox-1"},
            "Containers": {"provisioner-id": {}},
        }

        def reload(self):
            return None

        def disconnect(self, container, force):
            disconnected.append((container.id, force))

        def remove(self):
            removed.append(True)

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: None)
    backend._client.networks = SimpleNamespace(get=lambda _name: FakeNetwork())
    backend._provisioner_container = SimpleNamespace(id="provisioner-id")

    backend._delete_network("sandbox-1")

    assert disconnected == []
    assert removed == []
