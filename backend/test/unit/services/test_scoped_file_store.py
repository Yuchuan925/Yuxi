from __future__ import annotations

import hashlib
import io
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.services import thread_output_service as output_service
from yuxi.services.scoped_file_store import (
    replace_scope_with_local_tree,
    replace_scope_with_objects,
    scoped_relative_path,
    validate_scoped_virtual_path,
)
from yuxi.services.thread_output_service import find_output_descriptor, list_output_entries


@pytest.mark.parametrize(
    "path",
    [
        "/home/gem/user-data/workspace/out.txt",
        "/home/gem/user-data/outputs",
        "/home/gem/user-data/outputs/../uploads/secret.txt",
        "/home/gem/user-data/outputs\\escape.txt",
        "home/gem/user-data/outputs/out.txt",
    ],
)
def test_output_scope_rejects_paths_outside_exact_virtual_root(path: str):
    with pytest.raises(ValueError):
        validate_scoped_virtual_path("outputs", path)


def test_output_snapshot_directory_listing_and_exact_lookup():
    files = [
        {"path": "/home/gem/user-data/outputs/report.txt", "size": 6},
        {"path": "/home/gem/user-data/outputs/nested/chart.png", "size": 12},
        {"path": "/home/gem/user-data/outputs/nested/data.csv", "size": 8},
    ]

    root = list_output_entries(files, "/home/gem/user-data/outputs")
    nested = list_output_entries(files, "/home/gem/user-data/outputs/nested")
    recursive = list_output_entries(files, "/home/gem/user-data/outputs", recursive=True)

    assert [(item["name"], item["is_dir"]) for item in root] == [("nested", True), ("report.txt", False)]
    assert [item["name"] for item in nested] == ["chart.png", "data.csv"]
    assert [item["path"] for item in recursive] == [
        "/home/gem/user-data/outputs/nested/",
        "/home/gem/user-data/outputs/nested/chart.png",
        "/home/gem/user-data/outputs/nested/data.csv",
        "/home/gem/user-data/outputs/report.txt",
    ]
    assert find_output_descriptor(files, "/home/gem/user-data/outputs/report.txt") is files[0]
    assert find_output_descriptor(files, "/home/gem/user-data/outputs/missing.txt") is None
    assert scoped_relative_path("outputs", files[1]["path"]) == "nested/chart.png"


@pytest.mark.asyncio
async def test_object_hydrate_closes_and_releases_minio_response():
    content = b"published output"

    class FakeResponse:
        def __init__(self):
            self.stream = io.BytesIO(content)
            self.closed = False
            self.released = False

        def read(self, size):
            return self.stream.read(size)

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    class FakeMinio:
        def __init__(self):
            self.response = FakeResponse()

        async def adownload_response(self, bucket_name, object_name):
            assert (bucket_name, object_name) == ("thread-files", "outputs/report.txt")
            return self.response

    class FakeBackend:
        def __init__(self):
            self.uploaded = b""

        def clear_scope_files(self, scope):
            assert scope == "outputs"

        def upload_scope_file_from_path(self, scope, path, source_path):
            assert (scope, path) == ("outputs", "/home/gem/user-data/outputs/report.txt")
            self.uploaded = Path(source_path).read_bytes()

    minio = FakeMinio()
    backend = FakeBackend()
    await replace_scope_with_objects(
        backend=backend,
        scope="outputs",
        files=[
            {
                "path": "/home/gem/user-data/outputs/report.txt",
                "bucket_name": "thread-files",
                "object_name": "outputs/report.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        minio_client=minio,
        max_files=10,
        max_bytes=1024,
    )

    assert backend.uploaded == content
    assert minio.response.closed is True
    assert minio.response.released is True


@pytest.mark.asyncio
async def test_object_hydrate_stops_when_response_exceeds_descriptor_size():
    content = b"too large"

    class FakeResponse:
        def __init__(self):
            self.stream = io.BytesIO(content)
            self.closed = False
            self.released = False

        def read(self, size):
            return self.stream.read(size)

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    response = FakeResponse()

    class FakeBackend:
        def __init__(self):
            self.clear_count = 0

        def clear_scope_files(self, _scope):
            self.clear_count += 1

        def upload_scope_file_from_path(self, *_args):
            raise AssertionError("oversized object must not reach sandbox")

    backend = FakeBackend()
    minio = type(
        "FakeMinio",
        (),
        {"adownload_response": lambda _self, *_args: _async_value(response)},
    )()

    with pytest.raises(ValueError, match="exceeds declared size"):
        await replace_scope_with_objects(
            backend=backend,
            scope="outputs",
            files=[
                {
                    "path": "/home/gem/user-data/outputs/report.txt",
                    "bucket_name": "thread-files",
                    "object_name": "outputs/report.txt",
                    "size": 3,
                    "sha256": hashlib.sha256(b"abc").hexdigest(),
                }
            ],
            minio_client=minio,
            max_files=10,
            max_bytes=1024,
        )

    assert backend.clear_count == 2
    assert response.closed is True
    assert response.released is True


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_stage_outputs_rejects_file_count_before_downloading(monkeypatch):
    class FakeDB:
        async def get(self, _model, _identifier):
            return SimpleNamespace(id=1, uid="user-1", thread_id="thread-1")

        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_session_context():
        yield FakeDB()

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def create_staging(self, **_kwargs):
            return None

        async def mark_status(self, *_args):
            return None

    class FakeBackend:
        def __init__(self, **kwargs):
            assert kwargs["create_if_missing"] is False

        def list_output_files(self):
            return [
                f"/home/gem/user-data/outputs/{index}.txt"
                for index in range(output_service.MAX_OUTPUT_SNAPSHOT_FILES + 1)
            ]

        def download_output_file_to_path(self, *_args):
            raise AssertionError("oversized snapshot must fail before downloading")

    monkeypatch.setattr(output_service.pg_manager, "get_async_session_context", fake_session_context)
    monkeypatch.setattr(output_service, "ThreadOutputRepository", FakeRepository)
    monkeypatch.setattr(output_service, "ProvisionerSandboxBackend", FakeBackend)

    with pytest.raises(ValueError, match="文件数超过限制"):
        await output_service.stage_thread_outputs(
            runtime_thread_id="thread-1",
            file_thread_id="thread-1",
            uid="user-1",
            conversation_id=1,
            run_id=None,
            base_revision_id=None,
        )


@pytest.mark.asyncio
async def test_legacy_local_tree_rehydrates_nested_outputs_without_following_symlinks(tmp_path):
    root = tmp_path / "outputs"
    (root / "nested").mkdir(parents=True)
    (root / "report.txt").write_bytes(b"report")
    (root / "nested" / "chart.csv").write_bytes(b"x,y\n1,2\n")

    class FakeBackend:
        def __init__(self):
            self.clear_count = 0
            self.files: dict[str, bytes] = {}

        def clear_scope_files(self, scope):
            assert scope == "outputs"
            self.clear_count += 1
            self.files.clear()

        def upload_scope_file_from_path(self, scope, path, source_path):
            assert scope == "outputs"
            self.files[path] = Path(source_path).read_bytes()

    backend = FakeBackend()
    await replace_scope_with_local_tree(
        backend=backend,
        scope="outputs",
        root=root,
        max_files=10,
        max_bytes=1024,
    )

    assert backend.clear_count == 1
    assert backend.files == {
        "/home/gem/user-data/outputs/report.txt": b"report",
        "/home/gem/user-data/outputs/nested/chart.csv": b"x,y\n1,2\n",
    }

    (root / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="non-regular"):
        await replace_scope_with_local_tree(
            backend=backend,
            scope="outputs",
            root=root,
            max_files=10,
            max_bytes=1024,
        )
    assert backend.clear_count == 3
    assert backend.files == {}
