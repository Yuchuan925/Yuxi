from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from server.routers import chat_router
from yuxi.storage import minio as minio_module


@pytest.mark.asyncio
async def test_published_artifact_disconnect_releases_minio_response(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        def __init__(self):
            self.stream = io.BytesIO(b"first-second")
            self.closed = False
            self.released = False

        def read(self, size):
            del size
            return self.stream.read(5)

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    response = FakeResponse()

    async def fake_resolve(**_kwargs):
        return {
            "path": "/home/gem/user-data/outputs/report.txt",
            "bucket_name": "thread-files",
            "object_name": "outputs/report.txt",
            "content_type": "text/plain",
        }

    async def fake_download(*_args):
        return response

    monkeypatch.setattr(chat_router, "resolve_thread_artifact_view", fake_resolve)
    monkeypatch.setattr(
        minio_module,
        "get_minio_client",
        lambda: SimpleNamespace(adownload_response=fake_download),
    )

    artifact = await chat_router.get_thread_artifact(
        thread_id="thread-1",
        path="home/gem/user-data/outputs/report.txt",
        download=False,
        db=None,
        current_user=SimpleNamespace(uid="user-1"),
    )
    iterator = artifact.body_iterator
    assert await anext(iterator) == b"first"
    await iterator.aclose()

    assert response.closed is True
    assert response.released is True
