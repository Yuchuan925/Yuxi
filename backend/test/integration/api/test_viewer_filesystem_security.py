from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from yuxi.agents.backends.sandbox.paths import user_workdir_host_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_thread_for_user(test_client, headers: dict[str, str]) -> tuple[str, str]:
    agent_resp = await test_client.get("/api/agent/default", headers=headers)
    assert agent_resp.status_code == 200, agent_resp.text
    agent = agent_resp.json().get("agent") or {}
    agent_id = agent.get("slug") or agent.get("id")
    if not agent_id:
        pytest.skip("Default agent payload missing id field.")

    create_resp = await test_client.post(
        "/api/chat/thread",
        json={
            "agent_id": agent_id,
            "title": f"viewer-security-test-{uuid.uuid4().hex[:8]}",
            "metadata": {},
        },
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    payload = create_resp.json()
    thread_id = payload.get("thread_id") or payload.get("id")
    assert thread_id
    return str(thread_id), str(payload["workdir_path"])


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
