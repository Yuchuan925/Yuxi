from copy import deepcopy
import os
from pathlib import Path

import pytest
import yaml


FORBIDDEN_API_WORKER_TARGETS = frozenset({"/app/models", "/var/run/docker.sock"})
FORBIDDEN_API_WORKER_ENV_KEYS = frozenset({"YUXI_DOCKER_API_BASE"})
FORBIDDEN_DIRECT_DOCKER_ACCESS_MARKERS = frozenset(
    {"--unix-socket", "/var/run/docker.sock", "YUXI_DOCKER_API_SOCKET", "docker.from_env(", "DockerClient("}
)
SANDBOX_CLEANUP_OWNER_PATHS = (
    "backend/test/integration/conftest.py",
    "backend/test/live_api_cleanup.py",
)


def _project_root() -> Path:
    """定位包含 Compose 文件的仓库根目录。"""
    configured = os.environ.get("YUXI_PROJECT_ROOT")
    if configured:
        return Path(configured)

    for parent in Path(__file__).resolve().parents:
        if (parent / "docker-compose.yml").exists():
            return parent
    pytest.skip("当前测试环境未挂载仓库根目录")


def _load_compose(filename: str) -> dict:
    return yaml.safe_load((_project_root() / filename).read_text())


def _volume_target(volume: object) -> str:
    """读取 Compose 短格式或长格式 volume 的容器目标路径。"""
    if isinstance(volume, dict):
        return str(volume.get("target") or "")
    if not isinstance(volume, str):
        return ""

    parts = volume.split(":")
    return parts[1] if len(parts) >= 2 else parts[0]


def _forbidden_api_worker_mounts(compose: dict) -> set[tuple[str, str]]:
    violations: set[tuple[str, str]] = set()
    for service_name in ("api", "worker"):
        volumes = compose["services"][service_name].get("volumes") or []
        for volume in volumes:
            target = _volume_target(volume)
            if target in FORBIDDEN_API_WORKER_TARGETS:
                violations.add((service_name, target))
    return violations


def _forbidden_api_worker_env_keys(compose: dict) -> set[tuple[str, str]]:
    """识别 API/worker 中已失去 consumer 的环境变量。"""
    violations: set[tuple[str, str]] = set()
    for service_name in ("api", "worker"):
        environment = compose["services"][service_name].get("environment") or {}
        for key in FORBIDDEN_API_WORKER_ENV_KEYS & environment.keys():
            violations.add((service_name, key))
    return violations


def _forbidden_direct_docker_access(source: str) -> set[str]:
    """识别绕过 provisioner 直接访问 Docker daemon 的测试代码。"""
    return {marker for marker in FORBIDDEN_DIRECT_DOCKER_ACCESS_MARKERS if marker in source}


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_api_and_worker_do_not_mount_unused_host_dependencies(filename: str):
    """API/worker 不得重新依赖模型目录或 Docker daemon。"""
    assert _forbidden_api_worker_mounts(_load_compose(filename)) == set()


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_api_and_worker_do_not_expose_removed_docker_api_configuration(filename: str):
    """API/worker 不得保留已删除 Docker daemon 通道的配置表面。"""
    assert _forbidden_api_worker_env_keys(_load_compose(filename)) == set()


@pytest.mark.parametrize("filename", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_docker_provisioner_keeps_required_docker_socket(filename: str):
    """Docker provisioner 仍需拥有创建动态 sandbox 的 Docker socket。"""
    compose = _load_compose(filename)
    volumes = compose["services"]["sandbox-provisioner"].get("volumes") or []

    assert "/var/run/docker.sock" in {_volume_target(volume) for volume in volumes}


def test_integration_cleanup_does_not_bypass_sandbox_provisioner():
    """集成测试清理不得要求 API 容器直接访问 Docker daemon。"""
    source = "\n".join((_project_root() / path).read_text() for path in SANDBOX_CLEANUP_OWNER_PATHS)

    assert _forbidden_direct_docker_access(source) == set()


@pytest.mark.parametrize(
    ("service_name", "mount", "expected_target"),
    [
        ("api", "./docker/volumes/models:/app/models", "/app/models"),
        ("worker", "/var/run/docker.sock:/var/run/docker.sock", "/var/run/docker.sock"),
    ],
)
def test_mount_guard_detects_reintroduced_api_worker_host_dependencies(
    service_name: str,
    mount: str,
    expected_target: str,
):
    """恢复已删除挂载时，边界 guard 必须在正确目标上失败。"""
    compose = deepcopy(_load_compose("docker-compose.yml"))
    compose["services"][service_name].setdefault("volumes", []).append(mount)

    assert _forbidden_api_worker_mounts(compose) == {(service_name, expected_target)}


def test_environment_guard_detects_reintroduced_docker_api_configuration():
    """恢复旧 Docker API 环境变量时，边界 guard 必须报告对应服务。"""
    compose = deepcopy(_load_compose("docker-compose.yml"))
    compose["services"]["api"]["environment"]["YUXI_DOCKER_API_BASE"] = "http://localhost"

    assert _forbidden_api_worker_env_keys(compose) == {("api", "YUXI_DOCKER_API_BASE")}


@pytest.mark.parametrize("marker", sorted(FORBIDDEN_DIRECT_DOCKER_ACCESS_MARKERS))
def test_cleanup_guard_detects_reintroduced_direct_docker_access(marker: str):
    """恢复 Docker socket 清理路径时，边界 guard 必须报告对应标记。"""
    assert _forbidden_direct_docker_access(f"cleanup command: {marker}") == {marker}
