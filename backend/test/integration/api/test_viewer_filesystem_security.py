from __future__ import annotations

import uuid

import pytest
from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_thread_for_user(test_client, headers: dict[str, str]) -> str:
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
    return thread_id


async def _project_backend(test_client, headers, thread_id: str, uid: str) -> tuple[ProvisionerSandboxBackend, str]:
    response = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/"},
        files={"files": ("probe.txt", b"probe", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    path = response.json()["entries"][0]["path"]
    project_root = path.rsplit("/", 1)[0]
    workdir_id = project_root.removeprefix("/home/gem/projects/project-")
    return (
        ProvisionerSandboxBackend(
            thread_id=thread_id,
            uid=uid,
            workdir_id=workdir_id,
            sandbox_instance_id=thread_id,
            create_if_missing=False,
        ),
        project_root,
    )


async def test_viewer_download_blocks_project_symlink_escape(test_client, standard_user):
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    thread_id = await _create_thread_for_user(test_client, headers)

    backend, project_root = await _project_backend(test_client, headers, thread_id, uid)
    file_path = f"{project_root}/escape.txt"
    result = backend.execute(f"ln -s /etc/hosts {file_path}")
    assert result.exit_code == 0, result.output

    response = await test_client.get(
        "/api/viewer/filesystem/download",
        params={"thread_id": thread_id, "path": file_path},
        headers=headers,
    )

    assert response.status_code == 403, response.text


async def test_viewer_upload_blocks_project_symlink_escape(test_client, standard_user):
    headers = standard_user["headers"]
    uid = str(standard_user["user"]["uid"])
    thread_id = await _create_thread_for_user(test_client, headers)

    backend, project_root = await _project_backend(test_client, headers, thread_id, uid)
    outside_dir = f"/tmp/yuxi-viewer-{uuid.uuid4().hex}"
    parent_path = f"{project_root}/escape-dir"
    result = backend.execute(f"mkdir -p {outside_dir} && ln -s {outside_dir} {parent_path}")
    assert result.exit_code == 0, result.output

    response = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": parent_path},
        files={"files": ("escape.txt", b"outside", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    result = backend.execute(f"test ! -e {outside_dir}/escape.txt")
    assert result.exit_code == 0, result.output
