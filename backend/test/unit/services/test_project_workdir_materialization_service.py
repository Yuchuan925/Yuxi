from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import yuxi.services.project_workdir_materialization_service as svc


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
