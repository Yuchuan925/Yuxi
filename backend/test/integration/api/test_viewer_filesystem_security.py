from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from test.live_api_cleanup import make_test_session_title
from yuxi.modules.workspace.paths import user_workdir_host_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_thread_for_user(test_client, headers: dict[str, str]) -> tuple[str, str]:
    agent_resp = await test_client.get("/api/agent/default", headers=headers)
    assert agent_resp.status_code == 200, agent_resp.text
    agent = agent_resp.json().get("agent") or {}
    agent_id = agent.get("slug") or agent.get("id")
    if not agent_id:
        pytest.skip("Default agent payload missing id field.")

    create_resp = await test_client.post(
        "/api/v1/agents/sessions",
        json={
            "agent_id": agent_id,
            "title": make_test_session_title("viewer-filesystem-security"),
        },
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_resp.status_code == 201, create_resp.text
    thread_id = create_resp.json()["id"]
    assert thread_id
    connection = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    try:
        workdir_path = await connection.fetchval(
            "SELECT p.workdir_path FROM sessions c JOIN projects p ON p.id = c.project_id WHERE c.thread_id = $1",
            thread_id,
        )
    finally:
        await connection.close()
    assert workdir_path
    return str(thread_id), str(workdir_path)


async def test_viewer_download_blocks_project_symlink_escape(test_client, standard_user):
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    thread_id, workdir_path = await _create_thread_for_user(test_client, headers)

    project_root = f"/home/gem/user-data/{workdir_path}"
    file_path = f"{project_root}/escape.txt"
    (user_workdir_host_dir(uid, workdir_path) / "escape.txt").symlink_to("/etc/hosts")

    response = await test_client.get(
        "/api/viewer/filesystem/download",
        params={"thread_id": thread_id, "path": file_path},
        headers=headers,
    )

    assert response.status_code == 403, response.text


async def test_viewer_upload_blocks_project_symlink_escape(test_client, standard_user, tmp_path: Path):
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    thread_id, workdir_path = await _create_thread_for_user(test_client, headers)

    project_root = f"/home/gem/user-data/{workdir_path}"
    outside_dir = tmp_path / f"yuxi-viewer-{uuid.uuid4().hex}"
    outside_dir.mkdir()
    parent_path = f"{project_root}/escape-dir"
    (user_workdir_host_dir(uid, workdir_path) / "escape-dir").symlink_to(outside_dir)

    response = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": parent_path},
        files={"files": ("escape.txt", b"outside", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert not (outside_dir / "escape.txt").exists()


async def test_viewer_upload_preserves_non_directory_parent_error(test_client, standard_user):
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    thread_id, workdir_path = await _create_thread_for_user(test_client, headers)

    (user_workdir_host_dir(uid, workdir_path) / "occupied").write_text("file", encoding="utf-8")

    response = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/occupied"},
        files={"files": ("child.txt", b"content", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "目录不存在"


async def test_viewer_upload_does_not_replace_hidden_target_symlink(test_client, standard_user, tmp_path: Path):
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    thread_id, workdir_path = await _create_thread_for_user(test_client, headers)

    outside = tmp_path / f"yuxi-viewer-target-{uuid.uuid4().hex}.txt"
    outside.write_text("outside", encoding="utf-8")
    target = user_workdir_host_dir(uid, workdir_path) / "hidden.txt"
    target.symlink_to(outside)

    response = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/"},
        files={"files": ("hidden.txt", b"replacement", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"
