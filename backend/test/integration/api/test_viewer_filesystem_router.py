"""Realtime Project Workdir HTTP integration tests."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_thread(test_client, headers) -> tuple[str, str]:
    response = await test_client.get("/api/agent/default", headers=headers)
    assert response.status_code == 200, response.text
    agent = response.json().get("agent") or {}
    agent_id = agent.get("slug") or agent.get("id")
    if not agent_id:
        pytest.skip("default agent unavailable")
    response = await test_client.post(
        "/api/chat/thread",
        json={"agent_id": agent_id, "title": f"viewer-{uuid.uuid4().hex[:8]}", "metadata": {}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return str(payload.get("thread_id") or payload["id"]), str(payload["workdir_path"])


async def test_viewer_tree_requires_authentication(test_client):
    response = await test_client.get("/api/viewer/filesystem/tree", params={"thread_id": "x", "path": "/"})
    assert response.status_code == 401


async def test_created_file_is_immediately_visible_to_tree_preview_and_artifact(
    test_client,
    standard_user,
    admin_headers,
):
    headers = standard_user["headers"]
    thread_id, workdir_path = await _create_thread(test_client, headers)

    upload = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/"},
        files={"files": ("live.txt", b"live bytes\n", "text/plain")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    [entry] = upload.json()["entries"]
    file_path = entry["path"]
    assert file_path.startswith(f"/home/gem/user-data/{workdir_path}/")

    tree = await test_client.get(
        "/api/viewer/filesystem/tree",
        params={"thread_id": thread_id, "path": "/"},
        headers=headers,
    )
    assert tree.status_code == 200, tree.text
    assert "live.txt" in {item["name"] for item in tree.json()["entries"]}

    preview = await test_client.get(
        "/api/viewer/filesystem/file",
        params={"thread_id": thread_id, "path": file_path},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["content"] == "live bytes\n"

    artifact = await test_client.get(
        f"/api/chat/thread/{thread_id}/artifacts/{file_path.lstrip('/')}",
        headers=headers,
    )
    assert artifact.status_code == 200, artifact.text
    assert artifact.content == b"live bytes\n"

    cross_user = await test_client.get(
        f"/api/chat/thread/{thread_id}/artifacts/{file_path.lstrip('/')}",
        headers=admin_headers,
    )
    assert cross_user.status_code == 404

    deleted = await test_client.delete(
        "/api/viewer/filesystem/file",
        params={"thread_id": thread_id, "path": file_path},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    missing = await test_client.get(
        "/api/viewer/filesystem/download",
        params={"thread_id": thread_id, "path": file_path},
        headers=headers,
    )
    assert missing.status_code == 404


async def test_viewer_rejects_paths_outside_current_workdir(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id, _ = await _create_thread(test_client, headers)
    for path in (
        "/home/gem/user-data/agents/skills/private.txt",
        "/home/gem/user-data/projects/other/file.txt",
    ):
        response = await test_client.get(
            "/api/viewer/filesystem/file",
            params={"thread_id": thread_id, "path": path},
            headers=headers,
        )
        assert response.status_code == 403
