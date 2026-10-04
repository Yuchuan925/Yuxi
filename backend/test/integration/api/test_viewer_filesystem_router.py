"""Realtime Project Workdir HTTP integration tests."""

from __future__ import annotations

import uuid

import pytest
from test.live_api_cleanup import make_test_session_title

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _create_thread(test_client, headers) -> str:
    response = await test_client.get("/api/agent/default", headers=headers)
    assert response.status_code == 200, response.text
    agent = response.json().get("agent") or {}
    agent_id = agent.get("slug") or agent.get("id")
    if not agent_id:
        pytest.skip("default agent unavailable")
    response = await test_client.post(
        "/api/v1/agents/threads",
        json={
            "agent_id": agent_id,
            "title": make_test_session_title("viewer-filesystem"),
        },
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return str(payload.get("thread_id") or payload["id"])


async def test_viewer_tree_requires_authentication(test_client):
    response = await test_client.get("/api/viewer/filesystem/tree", params={"thread_id": "x", "path": "/"})
    assert response.status_code == 401


async def test_created_file_is_immediately_visible_to_tree_preview_and_artifact(
    test_client,
    standard_user,
    admin_headers,
):
    headers = standard_user["headers"]
    thread_id = await _create_thread(test_client, headers)

    upload = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/"},
        files={"files": ("live.txt", b"live bytes\n", "text/plain")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    [entry] = upload.json()["entries"]
    file_path = entry["path"]
    assert file_path == "/live.txt"

    collision = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/"},
        files={"files": ("live.txt", b"replacement", "text/plain")},
        headers=headers,
    )
    assert collision.status_code == 409, collision.text

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

    artifact = await test_client.get(entry["artifact_url"], headers=headers)
    assert artifact.status_code == 200, artifact.text
    assert artifact.content == b"live bytes\n"

    cross_user = await test_client.get(entry["artifact_url"], headers=admin_headers)
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
    thread_id = await _create_thread(test_client, headers)
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


async def test_mention_search_observes_live_viewer_files_without_cache(test_client, standard_user):
    headers = standard_user["headers"]
    thread_id = await _create_thread(test_client, headers)
    filename = "mention-live-file.txt"
    upload = await test_client.post(
        "/api/viewer/filesystem/upload",
        data={"thread_id": thread_id, "parent_path": "/"},
        files={"files": (filename, b"live", "text/plain")},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    artifact_path = upload.json()["entries"][0]["artifact_url"].split("/artifacts/", 1)[1]

    found = await test_client.get(
        "/api/mention/search",
        params={"thread_id": thread_id, "query": "mention-live", "sources": "thread"},
        headers=headers,
    )
    assert found.status_code == 200, found.text
    assert found.json() == [
        {
            "name": filename,
            "path": f"/{artifact_path}",
            "is_dir": False,
            "source": "thread",
        }
    ]

    deleted = await test_client.delete(
        "/api/viewer/filesystem/file",
        params={"thread_id": thread_id, "path": f"/{filename}"},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text
    missing = await test_client.get(
        "/api/mention/search",
        params={"thread_id": thread_id, "query": "mention-live", "sources": "thread"},
        headers=headers,
    )
    assert missing.status_code == 200, missing.text
    assert missing.json() == []
