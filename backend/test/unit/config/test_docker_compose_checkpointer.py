import os
from pathlib import Path

import pytest
import yaml


def _project_root() -> Path:
    """定位包含 Compose 文件的仓库根目录。"""
    configured = os.environ.get("YUXI_PROJECT_ROOT")
    if configured:
        return Path(configured)

    for parent in Path(__file__).resolve().parents:
        if (parent / "docker-compose.yml").exists():
            return parent
    pytest.skip("当前测试环境未挂载仓库根目录")


def test_api_and_worker_default_to_postgres_checkpointer():
    """开发与生产 Compose 必须给 API/worker 使用相同的 PostgreSQL 默认值。"""
    project_root = _project_root()
    for filename in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((project_root / filename).read_text())

        assert compose["x-api-worker-env"]["LANGGRAPH_CHECKPOINTER_BACKEND"] == (
            "${LANGGRAPH_CHECKPOINTER_BACKEND:-postgres}"
        )
