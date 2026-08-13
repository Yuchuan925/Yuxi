from pathlib import Path

import pytest

from yuxi.storage.filestore import FileStoreError, LocalFileStore


@pytest.mark.asyncio
async def test_local_filestore_supports_write_read_stream_stat_and_list(tmp_path: Path):
    store = LocalFileStore(tmp_path)

    written = await store.put("threads/t1/uploads/report.txt", b"abcdef", content_type="text/custom")
    loaded = await store.read(written.key)
    chunks = [chunk async for chunk in store.stream(written.key, chunk_size=2)]
    listed = await store.list("threads/t1/")

    assert written.size == 6
    assert written.content_type == "text/custom"
    assert loaded.data == b"abcdef"
    assert b"".join(chunks) == b"abcdef"
    assert listed == [written]


@pytest.mark.asyncio
async def test_local_filestore_delete_and_delete_prefix_are_idempotent(tmp_path: Path):
    store = LocalFileStore(tmp_path)
    await store.put("threads/t1/outputs/a.txt", b"a")
    await store.put("threads/t1/outputs/nested/b.txt", b"b")
    await store.put("threads/t2/outputs/c.txt", b"c")

    await store.delete("threads/t1/outputs/missing.txt")
    deleted = await store.delete_prefix("threads/t1/outputs")

    assert deleted == 2
    assert await store.list("threads/t1/outputs") == []
    assert [item.key for item in await store.list("threads/t2")] == ["threads/t2/outputs/c.txt"]


@pytest.mark.asyncio
async def test_local_filestore_rejects_paths_outside_root(tmp_path: Path):
    store = LocalFileStore(tmp_path)

    with pytest.raises(FileStoreError):
        await store.put("../outside.txt", b"unsafe")

    assert not (tmp_path.parent / "outside.txt").exists()
