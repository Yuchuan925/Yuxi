import os
from pathlib import Path

import yaml


PROJECT_ROOT = Path(os.environ.get("YUXI_PROJECT_ROOT", Path(__file__).resolve().parents[3]))


def test_api_and_worker_default_to_postgres_checkpointer():
    for filename in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((PROJECT_ROOT / filename).read_text())

        assert compose["x-api-worker-env"]["LANGGRAPH_CHECKPOINTER_BACKEND"] == (
            "${LANGGRAPH_CHECKPOINTER_BACKEND:-postgres}"
        )
        assert compose["services"]["api"]["environment"]["LANGGRAPH_CHECKPOINTER_BACKEND"] == (
            "${LANGGRAPH_CHECKPOINTER_BACKEND:-postgres}"
        )
        assert compose["services"]["worker"]["environment"]["LANGGRAPH_CHECKPOINTER_BACKEND"] == (
            "${LANGGRAPH_CHECKPOINTER_BACKEND:-postgres}"
        )


def test_compose_uses_filestore_without_legacy_runtime_mounts():
    for filename in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((PROJECT_ROOT / filename).read_text())
        services = compose["services"]
        env = compose["x-api-worker-env"]

        assert env["FILESTORE_LOCAL_ROOT"] == "${FILESTORE_LOCAL_ROOT:-/tmp/yuxi-filestore}"
        runtime_services = (services["api"], services["worker"], services["sandbox-provisioner"])
        assert all(
            not any(
                "/app/saves" in str(volume) or "/app/models" in str(volume)
                for volume in service.get("volumes", [])
            )
            for service in runtime_services
        )
        assert all(
            "/var/run/docker.sock" not in str(volume)
            for service_name in ("api", "worker")
            for volume in services[service_name].get("volumes", [])
        )
        serialized = (PROJECT_ROOT / filename).read_text()
        assert "DOCKER_THREADS_HOST_PATH" not in serialized
        assert "THREAD_PVC" not in serialized
        assert "SKILLS_PVC" not in serialized


def test_production_compose_exposes_complete_filestore_configuration():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.prod.yml").read_text())
    env = compose["x-api-worker-env"]

    for key in (
        "FILESTORE_BACKEND",
        "FILESTORE_LOCAL_ROOT",
        "FILESTORE_S3_ENDPOINT",
        "FILESTORE_S3_ACCESS_KEY",
        "FILESTORE_S3_SECRET_KEY",
        "FILESTORE_S3_BUCKET",
        "FILESTORE_S3_REGION",
    ):
        assert key in env

    provisioner_volumes = compose["services"]["sandbox-provisioner"]["volumes"]
    assert provisioner_volumes == [
        "./docker/sandbox_provisioner/app.py:/app/app.py:ro",
        "./docker/sandbox_provisioner/sandbox.env:/app/sandbox.env:ro",
        "/var/run/docker.sock:/var/run/docker.sock",
    ]


def test_api_image_bakes_nltk_data_into_the_runtime_image():
    dockerfile = (PROJECT_ROOT / "docker/api.Dockerfile").read_text()

    assert "NLTK_DATA=/usr/local/share/nltk_data" in dockerfile
    assert "uv run --no-sync --no-dev python" in dockerfile
    assert "cdn.jsdelivr.net/gh/nltk/nltk_data@gh-pages/packages/tokenizers/punkt_tab.zip" in dockerfile
    assert "--connect-timeout 10 --max-time 120 --retry 3 --retry-all-errors" in dockerfile
    assert "sha256sum --check --strict" in dockerfile
    assert "nltk.data.find('tokenizers/punkt_tab')" in dockerfile
