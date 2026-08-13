from __future__ import annotations

import io

import pytest

from yuxi.services import conversation_service as cs
from yuxi.storage.filestore import LocalFileStore


class _DummyUpload:
    def __init__(self, *, filename: str, content_type: str | None, data: bytes):
        self.filename = filename
        self.content_type = content_type
        self._buffer = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    async def seek(self, offset: int) -> int:
        return self._buffer.seek(offset)


def test_build_attachment_storage_path_uses_thread_upload_key() -> None:
    virtual_path, storage_key = cs._build_attachment_storage_path(
        uid="u-1",
        thread_id="t-1",
        file_name="demo.txt",
    )

    assert virtual_path == "/home/gem/user-data/uploads/attachments/demo.md"
    assert storage_key == "threads/t-1/uploads/attachments/demo.md"


def test_serialize_attachment_includes_storage_keys() -> None:
    serialized = cs.serialize_attachment(
        {
            "file_id": "f-1",
            "file_name": "demo.txt",
            "file_type": "text/plain",
            "file_size": 5,
            "status": "parsed",
            "path": "/home/gem/user-data/uploads/attachments/demo.md",
            "storage_key": "threads/t-1/uploads/attachments/demo.md",
            "original_storage_key": "threads/t-1/uploads/demo.txt",
            "markdown_storage_key": "threads/t-1/uploads/attachments/demo.md",
        }
    )

    assert serialized["storage_key"] == "threads/t-1/uploads/attachments/demo.md"
    assert serialized["original_storage_key"] == "threads/t-1/uploads/demo.txt"
    assert serialized["markdown_storage_key"] == "threads/t-1/uploads/attachments/demo.md"


@pytest.mark.asyncio
async def test_materialize_attachment_files_keeps_original_object_when_conversion_unsupported(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(cs, "get_file_store", lambda: store)

    async def _unsupported(_upload):
        raise ValueError("unsupported")

    monkeypatch.setattr(cs, "_convert_upload_to_markdown", _unsupported)
    upload = _DummyUpload(filename="demo.pdf", content_type="application/pdf", data=b"%PDF-test")

    result = await cs._materialize_attachment_files(
        thread_id="t-1",
        uid="u-1",
        upload=upload,
        file_name="demo.pdf",
        file_content=b"%PDF-test",
    )

    assert result["status"] == "uploaded"
    assert result["storage_key"] == "threads/t-1/uploads/demo.pdf"
    assert "storage_path" not in result
    assert (await store.read(result["storage_key"])).data == b"%PDF-test"


@pytest.mark.asyncio
async def test_materialize_attachment_files_writes_markdown_object_when_conversion_succeeds(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(cs, "get_file_store", lambda: store)

    async def _fake_convert(_upload):
        return cs.ConversionResult(
            file_id="f-1",
            file_name="demo.txt",
            file_type="text/plain",
            file_size=5,
            markdown="hello\nworld",
            truncated=False,
        )

    monkeypatch.setattr(cs, "_convert_upload_to_markdown", _fake_convert)
    upload = _DummyUpload(filename="demo.txt", content_type="text/plain", data=b"hello")

    result = await cs._materialize_attachment_files(
        thread_id="t-1",
        uid="u-1",
        upload=upload,
        file_name="demo.txt",
        file_content=b"hello",
    )

    assert result["status"] == "parsed"
    assert result["storage_key"] == "threads/t-1/uploads/attachments/demo.md"
    assert result["original_storage_key"] == "threads/t-1/uploads/demo.txt"
    assert result["markdown_storage_key"] == "threads/t-1/uploads/attachments/demo.md"
    assert (await store.read(result["original_storage_key"])).data == b"hello"
    assert (await store.read(result["markdown_storage_key"])).data == b"hello\nworld"
