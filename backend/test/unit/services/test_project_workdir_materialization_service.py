from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import yuxi.services.project_workdir_materialization_service as svc


def test_shipping_has_no_legacy_revision_or_hydrate_surface():
    """除启动迁移 Owner 外，生产代码不得恢复第二文件事实源。"""
    backend_root = Path(__file__).resolve().parents[3]
    removed_paths = (
        "package/yuxi/repositories/thread_output_repository.py",
        "package/yuxi/services/thread_output_service.py",
        "package/yuxi/services/scoped_file_store.py",
    )
    assert all(not (backend_root / path).exists() for path in removed_paths)

    forbidden = (
        "ThreadOutputRevision",
        "current_output_revision_id",
        "scoped_file_store",
        "hydrate_attachment_records_to_sandbox",
    )
    migration_owner = (backend_root / "package/yuxi/services/project_workdir_materialization_service.py").resolve()
    offenders: list[str] = []
    for production_root in (backend_root / "package/yuxi", backend_root / "server"):
        for path in production_root.rglob("*.py"):
            if path.resolve() == migration_owner:
                continue
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                offenders.append(str(path.relative_to(backend_root)))
    assert offenders == []


def _source(path: str, content: bytes) -> svc.LegacyFileSource:
    return svc.LegacyFileSource(
        target_path=path,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        inline_content=content,
    )


def test_deduplicate_rejects_different_bytes_at_one_workdir_path():
    with pytest.raises(ValueError, match="内容冲突"):
        svc._deduplicate_sources([_source("uploads/data.csv", b"first"), _source("uploads/data.csv", b"second")])


def test_host_inventory_rejects_symlink(tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "escape.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="符号链接"):
        svc._scan_host_tree(root, "uploads")


@pytest.mark.asyncio
async def test_attachment_inventory_rejects_cross_thread_object_before_download(monkeypatch, tmp_path: Path):
    class _Client:
        KB_BUCKETS = {"documents": "knowledgebases"}

        async def adownload_response(self, _bucket, _object):
            pytest.fail("跨线程对象不得进入下载")

    monkeypatch.setattr(svc, "get_save_dir", lambda: tmp_path)
    monkeypatch.setattr(svc, "get_minio_client", _Client)
    conversation = SimpleNamespace(thread_id="thread-1")
    attachment = {
        "file_id": "file-1",
        "file_name": "report.txt",
        "file_size": 3,
        "bucket_name": "knowledgebases",
        "original_object_name": "threads/other-thread/attachments/file-1/original/report.txt",
        "original_path": "/home/gem/user-data/uploads/file-1_report.txt",
        "path": "/home/gem/user-data/uploads/file-1_report.txt",
    }

    with pytest.raises(ValueError, match="对象作用域无效"):
        await svc._attachment_sources(conversation, [attachment])


@pytest.mark.asyncio
async def test_attachment_inventory_rejects_legacy_symlink(monkeypatch, tmp_path: Path):
    class _Client:
        KB_BUCKETS = {"documents": "knowledgebases"}

    uploads = tmp_path / "threads/thread-1/user-data/uploads"
    uploads.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (uploads / "report.txt").symlink_to(secret)
    monkeypatch.setattr(svc, "get_save_dir", lambda: tmp_path)
    monkeypatch.setattr(svc, "get_minio_client", _Client)
    conversation = SimpleNamespace(thread_id="thread-1")
    attachment = {
        "file_id": "file-1",
        "file_name": "report.txt",
        "file_size": 6,
        "original_path": "/home/gem/user-data/uploads/report.txt",
        "path": "/home/gem/user-data/uploads/report.txt",
    }

    with pytest.raises(OSError):
        await svc._attachment_sources(conversation, [attachment])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bucket_name", "other-bucket", "作用域无效"),
        ("path", "/home/gem/user-data/workspace/report.txt", "路径无效"),
        (
            "object_name",
            "threads/other-thread/outputs/revisions/revision-1/report.txt",
            "对象作用域无效",
        ),
    ],
)
@pytest.mark.asyncio
async def test_output_inventory_rejects_unscoped_descriptor_before_download(monkeypatch, field, value, message):
    class _Client:
        KB_BUCKETS = {"documents": "knowledgebases"}

        async def adownload_response(self, _bucket, _object):
            pytest.fail("跨线程 outputs 对象不得进入下载")

    monkeypatch.setattr(svc, "get_minio_client", _Client)
    conversation = SimpleNamespace(thread_id="thread-1", uid="user-1")
    descriptor = {
        "path": "/home/gem/user-data/outputs/report.txt",
        "bucket_name": "knowledgebases",
        "object_name": "threads/thread-1/outputs/revisions/revision-1/report.txt",
        "size": 6,
        "sha256": hashlib.sha256(b"report").hexdigest(),
    }
    descriptor[field] = value

    with pytest.raises(ValueError, match=message):
        await svc._output_sources(conversation, "revision-1", [descriptor])


@pytest.mark.parametrize(
    ("row_thread_id", "row_uid"),
    [("other-thread", "user-1"), ("thread-1", "other-user")],
)
def test_output_revision_rejects_cross_scope_row(row_thread_id, row_uid):
    conversation = SimpleNamespace(thread_id="thread-1", uid="user-1")
    row = SimpleNamespace(thread_id=row_thread_id, uid=row_uid)

    with pytest.raises(ValueError, match="revision 作用域无效"):
        svc._validate_legacy_output_revision_scope(conversation, row)


@pytest.mark.asyncio
async def test_output_inventory_rejects_content_mismatch_and_releases_connection(monkeypatch):
    class _Response:
        def __init__(self):
            self.chunks = [b"report", b""]
            self.closed = False
            self.released = False

        def read(self, _size):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    response = _Response()

    class _Client:
        KB_BUCKETS = {"documents": "knowledgebases"}

        async def adownload_response(self, _bucket, _object):
            return response

    monkeypatch.setattr(svc, "get_minio_client", _Client)
    conversation = SimpleNamespace(thread_id="thread-1", uid="user-1")
    descriptor = {
        "path": "/home/gem/user-data/outputs/report.txt",
        "bucket_name": "knowledgebases",
        "object_name": "threads/thread-1/outputs/revisions/revision-1/report.txt",
        "size": 7,
        "sha256": hashlib.sha256(b"report").hexdigest(),
    }

    with pytest.raises(ValueError, match="内容不一致"):
        await svc._output_sources(conversation, "revision-1", [descriptor])

    assert response.closed is True
    assert response.released is True


def test_legacy_object_cleanup_grammar_excludes_tmp_and_other_namespaces():
    assert svc._is_legacy_file_storage_object("threads/thread-1/attachments/file-1/original/report.txt")
    assert svc._is_legacy_file_storage_object("threads/thread-1/outputs/revisions/revision-1/nested/report.txt")
    assert not svc._is_legacy_file_storage_object("tmp/chat_attachments/user-1/file-1/original/report.txt")
    assert not svc._is_legacy_file_storage_object("knowledgebases/kb-1/report.txt")
    assert not svc._is_legacy_file_storage_object("threads/thread-1/attachments/file-1/future/report.txt")


def test_host_inventory_closes_directory_fds_and_preserves_empty_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "legacy"
    root.mkdir()
    for index in range(40):
        (root / f"empty-{index}").mkdir()

    original_open = svc.os.open
    original_close = svc.os.close
    tracked: set[int] = set()
    max_open = 0

    def tracking_open(*args, **kwargs):
        nonlocal max_open
        descriptor = original_open(*args, **kwargs)
        tracked.add(descriptor)
        max_open = max(max_open, len(tracked))
        return descriptor

    def tracking_close(descriptor):
        tracked.discard(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(svc.os, "open", tracking_open)
    monkeypatch.setattr(svc.os, "close", tracking_close)

    sources = svc._scan_host_tree(root, "uploads")

    assert tracked == set()
    assert max_open <= 2
    assert len(sources) == 40
    assert all(source.is_directory for source in sources)


@pytest.mark.asyncio
async def test_materialize_epoch_replaces_workdir_only_after_verified_stage(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    final = projects / "workdir-1"
    final.mkdir(parents=True)
    (final / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(svc, "get_save_dir", lambda: tmp_path)
    monkeypatch.setattr(svc, "project_workdir_host_dir", lambda workdir_id: projects / workdir_id)
    sources = svc._deduplicate_sources(
        [
            svc.LegacyFileSource(
                target_path="outputs/empty",
                size=0,
                sha256="",
                is_directory=True,
            ),
            _source("uploads/input.txt", b"input"),
            _source("outputs/report.txt", b"report"),
        ]
    )
    inventory = svc.WorkdirInventory(
        workdir_id="workdir-1",
        uid="user-1",
        sources=sources,
        fingerprint=svc._inventory_fingerprint(sources),
    )

    await svc.materialize_inventory_epoch(epoch_id="epoch-1", inventories=(inventory,))

    assert not (final / "old.txt").exists()
    assert (final / "uploads/input.txt").read_bytes() == b"input"
    assert (final / "outputs/report.txt").read_bytes() == b"report"
    assert (final / "outputs/empty").is_dir()


@pytest.mark.asyncio
async def test_materialize_epoch_keeps_previous_root_when_source_changes(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    final = projects / "workdir-1"
    final.mkdir(parents=True)
    (final / "keep.txt").write_text("keep", encoding="utf-8")
    source = _source("outputs/report.txt", b"expected")
    source = svc.LegacyFileSource(
        target_path=source.target_path,
        size=source.size,
        sha256=source.sha256,
        inline_content=b"changed",
    )
    inventory = svc.WorkdirInventory(
        workdir_id="workdir-1",
        uid="user-1",
        sources=(source,),
        fingerprint=svc._inventory_fingerprint((source,)),
    )
    monkeypatch.setattr(svc, "get_save_dir", lambda: tmp_path)
    monkeypatch.setattr(svc, "project_workdir_host_dir", lambda workdir_id: projects / workdir_id)

    with pytest.raises(ValueError, match="发生变化"):
        await svc.materialize_inventory_epoch(epoch_id="epoch-1", inventories=(inventory,))

    assert (final / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_object_inventory_stops_oversized_stream_and_releases_connection(monkeypatch):
    class _Response:
        def __init__(self):
            self.chunks = [b"1234", b"56", b""]
            self.closed = False
            self.released = False

        def read(self, _size):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    response = _Response()

    class _Client:
        async def adownload_response(self, _bucket, _object):
            return response

    monkeypatch.setattr(svc, "MAX_MATERIALIZED_BYTES_PER_WORKDIR", 5)
    monkeypatch.setattr(svc, "get_minio_client", lambda: _Client())

    with pytest.raises(ValueError, match="物化上限"):
        await svc._download_object_content("documents", "legacy.bin")

    assert response.closed is True
    assert response.released is True
