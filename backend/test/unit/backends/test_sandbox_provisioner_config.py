from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

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
    backend._lock = threading.Lock()
    backend._container_port = 8080
    backend._network_prefix = "yuxi-know-sandbox"
    backend._sandbox_image = "sandbox-image"
    backend._container_prefix = "yuxi-sandbox"
    backend._sandbox_env = {}
    backend._health_timeout_seconds = 1
    backend._threads_host_path = str(tmp_path)
    backend._docker = SimpleNamespace(
        errors=SimpleNamespace(NotFound=KeyError),
        types=SimpleNamespace(Mount=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    volume = SimpleNamespace(
        attrs={"Labels": {"managed-by": "yuxi-sandbox-provisioner", "sandbox-id": "sandbox-1"}},
        name="sandbox-skills",
        remove=lambda **_kwargs: None,
    )
    backend._client = SimpleNamespace(
        containers=SimpleNamespace(run=run_container),
        volumes=SimpleNamespace(get=lambda _name: volume, create=lambda **_kwargs: volume),
    )
    return backend


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

    for value in ["../escape", "thread/name", "thread name", "thread;rm", "thread.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_thread_id(value)

    for value in ["../user", "user/name", "user name", "user;rm", "user.name"]:
        with pytest.raises(ValueError):
            backend_cls._validate_uid(value)


def test_memory_backend_accepts_split_thread_ids(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    backend = module.MemoryProvisionerBackend()

    record = backend.create(
        "sandbox-1",
        "child-thread",
        "user-1",
        file_thread_id="parent-thread",
        skills_thread_id="child-skills-thread",
    )

    assert record.sandbox_id == "sandbox-1"
    assert backend.discover("sandbox-1") is record


def test_docker_backend_uses_ephemeral_container_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    captured = []
    backend = _docker_backend(module, tmp_path, lambda image, **kwargs: captured.append(kwargs) or SimpleNamespace(
        name="sandbox", status="running", attrs={"State": {"Status": "running"}}, reload=lambda: None
    ))
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: None)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda _container: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: True)

    backend.create("sandbox-1", "thread-1", "user-1")

    assert "volumes" not in captured[0]
    assert captured[0]["tmpfs"] == {"/home/gem": "rw,exec,mode=777"}
    assert [(mount.target, mount.read_only) for mount in captured[0]["mounts"]] == [
        ("/home/gem/skills", True),
    ]


def test_docker_sandbox_uses_ephemeral_storage_without_business_mounts(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    captured = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
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
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda _container: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: True)

    backend.create("sandbox-1", "thread-1", "user-1")

    assert "volumes" not in captured[0][1]
    assert captured[0][1]["tmpfs"] == {"/home/gem": "rw,exec,mode=777"}


def test_kubernetes_mount_check_requires_only_ephemeral_home(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    pod = SimpleNamespace(
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    name="sandbox",
                    volume_mounts=[
                        SimpleNamespace(mount_path="/home/gem", read_only=False),
                        SimpleNamespace(mount_path="/home/gem/skills", read_only=True),
                    ],
                )
            ]
        )
    )

    assert module.KubernetesProvisionerBackend._pod_has_expected_mounts(
        pod,
        file_thread_id="parent-thread", skills_thread_id="child-skills-thread", uid="user-1",
    )
    assert module.KubernetesProvisionerBackend._pod_has_expected_mounts(
        pod, file_thread_id="child-thread", skills_thread_id="child-skills-thread", uid="user-1"
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
        delete_response = client.delete(f"/api/sandboxes/{sandbox_id}", headers=headers)

    expected_url = f"http://sandbox-provisioner:8002/api/sandboxes/{sandbox_id}/proxy"
    assert create_response.status_code == 200
    assert create_response.json()["sandbox_url"] == expected_url
    assert list_response.status_code == 200
    assert list_response.json()["sandboxes"] == [
        {"sandbox_id": sandbox_id, "sandbox_url": expected_url, "status": "Running"}
    ]
    assert delete_response.status_code == 200


def test_create_sandbox_forwards_environment_policy(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    calls = []

    def create(*_args, **kwargs):
        calls.append(kwargs)
        return module.SandboxRecord(sandbox_id="sandbox-1", sandbox_url="http://sandbox", status="Running")

    monkeypatch.setattr(module, "backend_impl", SimpleNamespace(create=create))
    monkeypatch.setattr(module.idle_reaper, "touch", lambda _sandbox_id: None)

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
    module = _load_module()
    captured = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
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
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda _container: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: True)

    record = backend.create("sandbox-1", "thread-1", "user-1")

    assert record.sandbox_url == "http://yuxi-sandbox-sandbox-1:8080"
    assert captured[0][0] == "sandbox-image"
    assert captured[0][1]["network"] == "yuxi-know-sandbox-sandbox-1"
    assert "ports" not in captured[0][1]


def test_docker_backend_can_disable_sandbox_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    captured = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(
        module,
        tmp_path,
        lambda image, **kwargs: captured.append((image, kwargs)) or FakeContainer(),
    )
    backend._sandbox_env = {"GLOBAL_SECRET": "value"}
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: None)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda _container: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: True)

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
    backend._sandbox_env = {"GLOBAL_SECRET": "value"}

    pod = backend._build_pod_spec(
        "sandbox-1",
        "thread-1",
        "user-1",
        {"USER_SECRET": "value"},
        file_thread_id="thread-1",
        skills_thread_id="thread-1",
        inherit_env=False,
    )

    assert pod.spec.automount_service_account_token is False
    assert pod.spec.containers[0].env == []
    assert len(pod.spec.volumes) == 2
    assert all(volume.empty_dir is not None for volume in pod.spec.volumes)
    mounts = {mount.mount_path: mount.read_only for mount in pod.spec.containers[0].volume_mounts}
    assert "/home/gem/.yuxi-skills-rw" not in mounts
    assert mounts["/home/gem/skills"] is True


def test_docker_backend_cleans_up_container_and_network_when_health_check_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    created_container = None
    deleted_networks = []

    class FakeContainer:
        name = "yuxi-sandbox-sandbox-1"
        status = "running"
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
        created_container = FakeContainer()
        return created_container

    backend = _docker_backend(module, tmp_path, run_container)
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: created_container)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_delete_network", deleted_networks.append)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda _container: None)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda _url, timeout_seconds: False)

    with pytest.raises(RuntimeError, match="is not ready"):
        backend.create("sandbox-1", "thread-1", "user-1")

    assert created_container is not None
    assert created_container.removed is True
    assert deleted_networks == ["sandbox-1"]


def test_docker_backend_cleans_up_network_when_container_start_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    deleted_networks = []

    def fail_to_start(_image, **_kwargs):
        raise RuntimeError("container start failed")

    backend = _docker_backend(module, tmp_path, fail_to_start)
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: None)
    monkeypatch.setattr(backend, "_ensure_network", backend._network_name)
    monkeypatch.setattr(backend, "_delete_network", deleted_networks.append)

    with pytest.raises(RuntimeError, match="container start failed"):
        backend.create("sandbox-1", "thread-1", "user-1")

    assert deleted_networks == ["sandbox-1"]


def test_docker_backend_delete_removes_ephemeral_skills_volume(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    removed = []
    volume = SimpleNamespace(
        attrs={"Labels": {"managed-by": "yuxi-sandbox-provisioner", "sandbox-id": "sandbox-1"}},
        name="yuxi-sandbox-sandbox-1-skills",
        remove=lambda *, force: removed.append(force),
    )
    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: None)
    backend._client.volumes = SimpleNamespace(get=lambda _name: volume)
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: None)
    monkeypatch.setattr(backend, "_delete_network", lambda _sandbox_id: None)

    backend.delete("sandbox-1")

    assert removed == [True]


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
        attrs = {"State": {"Status": "running"}}

        def reload(self):
            return None

    backend = _docker_backend(module, tmp_path, lambda *_args, **_kwargs: pytest.fail("sandbox was recreated"))
    backend._client.networks = SimpleNamespace(get=lambda _name: FakeNetwork())
    backend._provisioner_container = SimpleNamespace(id="provisioner-id")
    monkeypatch.setattr(backend, "_get_container", lambda _sandbox_id: FakeContainer())
    monkeypatch.setattr(backend, "_is_on_expected_network", lambda _container, _sandbox_id: True)
    monkeypatch.setattr(backend, "_has_expected_skills_mounts", lambda _container, _sandbox_id: True)
    monkeypatch.setattr(backend, "_ensure_user_data_writable", lambda _container: None)
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


def _install_fake_kubernetes(monkeypatch) -> dict:
    """在 sys.modules 中安装 fake kubernetes，提供 ApiException/stream/STDIN_CHANNEL。

    app.py 在方法内部惰性 import kubernetes，测试环境未安装该库，
    因此拦截 import 并挂接可断言的桩。
    """
    import types

    class ApiException(Exception):
        def __init__(self, *args, status=404, **kwargs):
            self.status = status
            super().__init__(*args)

    kubernetes = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    rest = types.ModuleType("kubernetes.client.rest")
    stream = types.ModuleType("kubernetes.stream")
    ws_client = types.ModuleType("kubernetes.stream.ws_client")

    rest.ApiException = ApiException
    ws_client.STDIN_CHANNEL = 0
    stream.stream = None
    client.rest = rest
    kubernetes.client = client
    kubernetes.stream = stream

    for name, module in {
        "kubernetes": kubernetes,
        "kubernetes.client": client,
        "kubernetes.client.rest": rest,
        "kubernetes.stream": stream,
        "kubernetes.stream.ws_client": ws_client,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return {
        "ApiException": ApiException,
        "stream": stream,
        "STDIN_CHANNEL": ws_client.STDIN_CHANNEL,
    }


def test_kubernetes_discover_discards_terminating_and_failed_pods(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    api_error = _install_fake_kubernetes(monkeypatch)["ApiException"]

    deleted = []
    pod_state = {"phase": "Running", "deletion_timestamp": None}

    def read_pod(name, namespace):
        if pod_state["phase"] == "NotFound":
            raise api_error(status=404)
        pod = SimpleNamespace(
            metadata=SimpleNamespace(
                annotations={
                    "thread-id": "thread-1",
                    "file-thread-id": "thread-1",
                    "skills-thread-id": "thread-1",
                    "uid": "user-1",
                },
                deletion_timestamp=pod_state["deletion_timestamp"],
            ),
            status=SimpleNamespace(phase=pod_state["phase"]),
        )
        return pod

    def read_service(name, namespace):
        return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(node_port=31234)]))

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._namespace = "yuxi-know"
    backend._node_host = "172.21.0.2"
    backend._core_api = SimpleNamespace(
        read_namespaced_pod=read_pod,
        read_namespaced_service=read_service,
        delete_namespaced_pod=lambda *a, **kw: deleted.append(("pod", kw)),
        delete_namespaced_service=lambda *a, **kw: deleted.append(("svc", kw)),
    )
    monkeypatch.setattr(backend, "_pod_has_expected_mounts", lambda *args, **kwargs: True)

    pod_state["phase"] = "Running"
    assert backend.discover("sandbox-1") is not None

    pod_state["deletion_timestamp"] = "2026-08-13T08:00:00Z"
    assert backend.discover("sandbox-1") is None
    assert any(kind == "pod" and kw.get("grace_period_seconds") == 0 for kind, kw in deleted)

    deleted.clear()
    pod_state["deletion_timestamp"] = None
    pod_state["phase"] = "Failed"
    assert backend.discover("sandbox-1") is None

    pod_state["phase"] = "NotFound"
    assert backend.discover("sandbox-1") is None


def test_kubernetes_delete_forces_pod_and_service_removal(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    _install_fake_kubernetes(monkeypatch)

    delete_calls = []

    def delete_pod(name, namespace, grace_period_seconds):
        delete_calls.append(("pod", name, grace_period_seconds))

    def delete_service(name, namespace, grace_period_seconds):
        delete_calls.append(("svc", name, grace_period_seconds))

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._namespace = "yuxi-know"
    backend._core_api = SimpleNamespace(
        delete_namespaced_pod=delete_pod,
        delete_namespaced_service=delete_service,
    )

    backend.delete("sandbox-1")

    assert sorted(c[0] for c in delete_calls) == ["pod", "svc"]
    assert all(c[2] == 0 for c in delete_calls)


def test_kubernetes_wait_for_sandbox_gone_returns_when_resources_vanish(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    api_error = _install_fake_kubernetes(monkeypatch)["ApiException"]

    reads = {"pod": 0, "svc": 0}

    def read_pod(name, namespace):
        reads["pod"] += 1
        if reads["pod"] <= 2:
            return SimpleNamespace()
        raise api_error(status=404)

    def read_service(name, namespace):
        reads["svc"] += 1
        if reads["svc"] <= 2:
            return SimpleNamespace()
        raise api_error(status=404)

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._namespace = "yuxi-know"
    backend._core_api = SimpleNamespace(
        read_namespaced_pod=read_pod,
        read_namespaced_service=read_service,
    )

    backend._wait_for_sandbox_gone("sandbox-1", timeout_seconds=10)

    assert reads["pod"] == 3
    assert reads["svc"] == 3


def test_kubernetes_replace_skills_uses_websocket_stdin_channel(monkeypatch):
    monkeypatch.setenv("PROVISIONER_BACKEND", "memory")
    module = _load_module()
    fake_k8s = _install_fake_kubernetes(monkeypatch)

    written = []
    channels_closed = []
    updated = []

    class FakeWsResponse:
        returncode = None

        def write_stdin(self, data):
            written.append(data)

        def close_channel(self, channel):
            channels_closed.append(channel)

        def is_open(self):
            return False

        def update(self, timeout=1):
            updated.append(True)

        def read_stderr(self):
            return ""

        def close(self):
            return None

    def fake_stream(*args, **kwargs):
        return FakeWsResponse()

    backend = object.__new__(module.KubernetesProvisionerBackend)
    backend._namespace = "yuxi-know"
    backend._core_api = SimpleNamespace(
        connect_get_namespaced_pod_exec=lambda *a, **kw: None,
    )
    monkeypatch.setattr(backend, "discover", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(fake_k8s["stream"], "stream", fake_stream)

    backend.replace_skills("sandbox-1", {"demo/SKILL.md": b"# demo\n"})

    assert written and written[0]
    assert channels_closed and channels_closed[0] == fake_k8s["STDIN_CHANNEL"]
    assert updated == []
