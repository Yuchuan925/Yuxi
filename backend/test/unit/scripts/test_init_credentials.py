from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPTS_ROOT = Path(os.getenv("YUXI_TEST_SCRIPTS_ROOT", "/workspace-scripts"))
PUBLIC_DEFAULTS = {
    "POSTGRES_PASSWORD": "postgres",
    "NEO4J_PASSWORD": "0123456789",
    "MINIO_ACCESS_KEY": "minioadmin",
    "MINIO_SECRET_KEY": "minioadmin",
}


def write_env(path: Path, credentials: dict[str, str]) -> None:
    """写入可直接通过初始化前置检查的测试环境文件。"""
    values = {
        "SILICONFLOW_API_KEY": "test-key",
        "JWT_SECRET_KEY": "jwt-test-key",
        "YUXI_INSTANCE_ID": "test-instance",
        "SANDBOX_PROVISIONER_TOKEN": "sandbox-test-token",
        **credentials,
    }
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")


def read_env(path: Path) -> dict[str, str]:
    """读取测试生成的简单键值环境文件。"""
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def run_init(
    tmp_path: Path,
    user_input: str = "",
    *,
    fail_data_inspection: bool = False,
) -> subprocess.CompletedProcess[str]:
    """使用假 Docker 执行初始化脚本，避免拉取真实镜像。"""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    shutil.copy(SCRIPTS_ROOT / "init.sh", scripts_dir / "init.sh")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    if fail_data_inspection:
        find = bin_dir / "find"
        find.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        find.chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "scripts/init.sh"],
        cwd=tmp_path,
        env=env,
        input=user_input,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_init_generates_service_credentials_for_new_install(tmp_path):
    result = run_init(tmp_path, user_input="test-api-key\n\n\n\n\n")

    assert result.returncode == 0, result.stderr
    credentials = read_env(tmp_path / ".env")
    for name, public_default in PUBLIC_DEFAULTS.items():
        assert credentials[name] != public_default
        assert len(credentials[name]) >= 20


def test_init_replaces_public_default_credentials(tmp_path):
    write_env(tmp_path / ".env", PUBLIC_DEFAULTS)

    result = run_init(tmp_path)

    assert result.returncode == 0, result.stderr
    credentials = read_env(tmp_path / ".env")
    for name, public_default in PUBLIC_DEFAULTS.items():
        assert credentials[name] != public_default
        assert len(credentials[name]) >= 20


@pytest.mark.parametrize("quote", ['"', "'"])
def test_init_replaces_quoted_public_default_credentials(tmp_path, quote):
    quoted_defaults = {name: f"{quote}{value}{quote}" for name, value in PUBLIC_DEFAULTS.items()}
    write_env(tmp_path / ".env", quoted_defaults)

    result = run_init(tmp_path)

    assert result.returncode == 0, result.stderr
    credentials = read_env(tmp_path / ".env")
    for name, public_default in PUBLIC_DEFAULTS.items():
        assert credentials[name] != public_default
        assert credentials[name] != f"{quote}{public_default}{quote}"


@pytest.mark.parametrize(
    "decorate",
    [
        lambda value: f"{value} # legacy default",
        lambda value: f'"{value}" # legacy default',
        lambda value: f"'{value}' # legacy default",
    ],
)
def test_init_replaces_commented_public_default_credentials(tmp_path, decorate):
    commented_defaults = {name: decorate(value) for name, value in PUBLIC_DEFAULTS.items()}
    write_env(tmp_path / ".env", commented_defaults)

    result = run_init(tmp_path)

    assert result.returncode == 0, result.stderr
    credentials = read_env(tmp_path / ".env")
    for name, public_default in PUBLIC_DEFAULTS.items():
        assert credentials[name] != public_default
        assert "legacy default" not in credentials[name]


def test_init_uses_last_duplicate_credential_definition(tmp_path):
    custom_credentials = {name: f"custom-{name.lower()}" for name in PUBLIC_DEFAULTS}
    write_env(tmp_path / ".env", custom_credentials)
    with (tmp_path / ".env").open("a", encoding="utf-8") as env_file:
        for name, public_default in PUBLIC_DEFAULTS.items():
            env_file.write(f"{name}={public_default}\n")

    result = run_init(tmp_path)

    assert result.returncode == 0, result.stderr
    env_lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    credentials = read_env(tmp_path / ".env")
    for name, public_default in PUBLIC_DEFAULTS.items():
        assert credentials[name] != public_default
        assert sum(line.startswith(f"{name}=") for line in env_lines) == 1


def test_init_replaces_interpolated_service_credentials(tmp_path):
    interpolated = {name: f"${{UNSET_{name}:-{public_default}}}" for name, public_default in PUBLIC_DEFAULTS.items()}
    write_env(tmp_path / ".env", interpolated)

    result = run_init(tmp_path)

    assert result.returncode == 0, result.stderr
    credentials = read_env(tmp_path / ".env")
    for name, public_default in PUBLIC_DEFAULTS.items():
        assert credentials[name] != public_default
        assert "$" not in credentials[name]


def test_init_preserves_custom_credentials(tmp_path):
    expected = {name: f"custom-{name.lower()}" for name in PUBLIC_DEFAULTS}
    write_env(tmp_path / ".env", expected)

    result = run_init(tmp_path)

    assert result.returncode == 0, result.stderr
    credentials = read_env(tmp_path / ".env")
    assert {name: credentials[name] for name in PUBLIC_DEFAULTS} == expected


@pytest.mark.parametrize(
    ("name", "data_path"),
    [
        ("POSTGRES_PASSWORD", "docker/volumes/postgresql"),
        ("NEO4J_PASSWORD", "docker/volumes/neo4j/data"),
        ("MINIO_ACCESS_KEY", "docker/volumes/milvus/minio"),
        ("MINIO_SECRET_KEY", "docker/volumes/milvus/minio"),
    ],
)
def test_init_stops_before_replacing_default_for_persisted_service(tmp_path, name, data_path):
    credentials = {key: f"custom-{key.lower()}" for key in PUBLIC_DEFAULTS}
    credentials[name] = PUBLIC_DEFAULTS[name]
    write_env(tmp_path / ".env", credentials)
    persisted_path = tmp_path / data_path
    persisted_path.mkdir(parents=True)
    (persisted_path / "existing-data").write_text("data", encoding="utf-8")

    result = run_init(tmp_path)

    assert result.returncode == 1
    assert f"{name} is missing or insecure" in result.stdout
    assert read_env(tmp_path / ".env")[name] == PUBLIC_DEFAULTS[name]


def test_init_stops_before_generating_missing_credential_for_persisted_service(tmp_path):
    credentials = {key: f"custom-{key.lower()}" for key in PUBLIC_DEFAULTS}
    credentials["POSTGRES_PASSWORD"] = ""
    write_env(tmp_path / ".env", credentials)
    persisted_path = tmp_path / "docker/volumes/postgresql"
    persisted_path.mkdir(parents=True)
    (persisted_path / "existing-data").write_text("data", encoding="utf-8")

    result = run_init(tmp_path)

    assert result.returncode == 1
    assert "POSTGRES_PASSWORD is missing or insecure" in result.stdout
    assert read_env(tmp_path / ".env")["POSTGRES_PASSWORD"] == ""


def test_init_stops_when_persisted_data_path_cannot_be_inspected(tmp_path):
    credentials = {key: f"custom-{key.lower()}" for key in PUBLIC_DEFAULTS}
    credentials["POSTGRES_PASSWORD"] = ""
    write_env(tmp_path / ".env", credentials)
    persisted_path = tmp_path / "docker/volumes/postgresql"
    persisted_path.mkdir(parents=True)

    result = run_init(tmp_path, fail_data_inspection=True)

    assert result.returncode == 1
    assert "Cannot safely inspect persisted data path" in result.stderr
    assert read_env(tmp_path / ".env")["POSTGRES_PASSWORD"] == ""


def test_powershell_init_covers_all_service_credentials():
    script = (SCRIPTS_ROOT / "init.ps1").read_text(encoding="utf-8")

    for name, public_default in PUBLIC_DEFAULTS.items():
        assert f'Ensure-ServiceCredential "{name}" "{public_default}"' in script
        assert f"{name}=${name}" in script
    assert "Confirm-NewInstallHasNoServiceData" in script
