from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from yuxi.agents.toolkits.buildin import tools
from yuxi.services import ocr_service
from yuxi.storage.filestore import LocalFileStore, thread_output_key, thread_upload_key, user_workspace_key

pytestmark = pytest.mark.unit


def _runtime(*, uid: str = "user-1", file_thread_id: str | None = "file-thread-1") -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(uid=uid, file_thread_id=file_thread_id))


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalFileStore:
    file_store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "get_file_store", lambda: file_store)
    return file_store


@pytest.mark.asyncio
async def test_ocr_parse_file_materializes_workspace_and_writes_markdown(store, monkeypatch) -> None:
    await store.put(user_workspace_key("user-1", "docs/scan.png"), b"fake image", content_type="image/png")
    captured: dict[str, object] = {}

    monkeypatch.setattr(ocr_service, "resolve_ocr_engine_id", lambda engine_id: engine_id)

    async def fake_parse_document(source: str, params: dict | None = None, db=None) -> str:
        del db
        source_path = Path(source)
        assert source_path.read_bytes() == b"fake image"
        captured["source"] = source_path
        captured["params"] = params
        return "识别结果\n" + ("长文本" * 500)

    monkeypatch.setattr(ocr_service, "parse_document", fake_parse_document)

    result = await tools.ocr_parse_file.coroutine(
        file_path="/home/gem/user-data/workspace/docs/scan.png",
        ocr_engine="mineru_ocr",
        runtime=_runtime(),
    )

    output = await store.read(thread_output_key("file-thread-1", "ocr/scan.md"))
    assert output.data.startswith("识别结果".encode())
    assert output.content_type == "text/markdown"
    assert result["source_path"] == "/home/gem/user-data/workspace/docs/scan.png"
    assert result["parsed_path"] == "/home/gem/user-data/outputs/ocr/scan.md"
    assert result["ocr_engine"] == "mineru_ocr"
    assert result["char_count"] == len(output.data.decode())
    assert result["truncated"] is True
    assert len(result["preview"]) <= 1200
    assert captured["params"] == {"ocr_engine": "mineru_ocr"}
    assert not captured["source"].exists()


@pytest.mark.asyncio
async def test_ocr_parse_file_uses_file_thread_id_for_upload_and_avoids_output_conflict(store, monkeypatch) -> None:
    await store.put(thread_upload_key("file-thread-1", "upload.pdf"), b"fake pdf")
    await store.put(thread_output_key("file-thread-1", "ocr/upload.md"), b"existing")

    def resolve_engine(engine_id):
        assert engine_id is None
        return "rapid_ocr"

    monkeypatch.setattr(ocr_service, "resolve_ocr_engine_id", resolve_engine)

    async def fake_parse_document(source: str, params: dict | None = None, db=None) -> str:
        del db
        assert Path(source).read_bytes() == b"fake pdf"
        assert params == {"ocr_engine": "rapid_ocr"}
        return "OCR content"

    monkeypatch.setattr(ocr_service, "parse_document", fake_parse_document)

    result = await tools.ocr_parse_file.coroutine(
        file_path="/home/gem/user-data/uploads/upload.pdf",
        runtime=_runtime(),
    )

    assert result["ocr_engine"] == "rapid_ocr"
    assert result["parsed_path"] == "/home/gem/user-data/outputs/ocr/upload-1.md"
    assert (await store.read(thread_output_key("file-thread-1", "ocr/upload-1.md"))).data == b"OCR content"


@pytest.mark.asyncio
async def test_ocr_parse_file_accepts_outputs_source_and_disable_engine(store, monkeypatch) -> None:
    await store.put(thread_output_key("file-thread-1", "text-layer.pdf"), b"fake pdf")

    async def fake_parse_document(source: str, params: dict | None = None, db=None) -> str:
        del source, db
        assert params == {"ocr_engine": "disable"}
        return "PDF text layer"

    monkeypatch.setattr(ocr_service, "parse_document", fake_parse_document)

    result = await tools.ocr_parse_file.coroutine(
        file_path="/home/gem/user-data/outputs/text-layer.pdf",
        ocr_engine="disable",
        runtime=_runtime(),
    )

    assert result["ocr_engine"] == "disable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    [
        "/etc/passwd",
        "/home/gem/user-data/uploads",
        "/home/gem/user-data/../secrets.png",
        "/home/gem/user-data/uploads//scan.png",
    ],
)
async def test_ocr_parse_file_rejects_noncanonical_path(store, file_path: str) -> None:
    with pytest.raises(ValueError, match="只允许"):
        await tools.ocr_parse_file.coroutine(file_path=file_path, runtime=_runtime())


@pytest.mark.asyncio
async def test_ocr_parse_file_requires_context_file_scope(store) -> None:
    with pytest.raises(ValueError, match="file_thread_id"):
        await tools.ocr_parse_file.coroutine(
            file_path="/home/gem/user-data/uploads/scan.png",
            runtime=_runtime(file_thread_id=None),
        )


@pytest.mark.asyncio
async def test_ocr_parse_file_locks_name_selection_and_put(store, monkeypatch) -> None:
    await store.put(thread_upload_key("file-thread-1", "upload.pdf"), b"fake pdf")
    events = []

    @asynccontextmanager
    async def lock(thread_id: str):
        events.append(("enter", thread_id))
        yield
        events.append(("exit", thread_id))

    async def fake_parse_document(source: str, params=None, db=None) -> str:
        del source, params, db
        return "content"

    monkeypatch.setattr(tools, "file_thread_operation_lock", lock)
    monkeypatch.setattr(ocr_service, "parse_document", fake_parse_document)

    await tools.ocr_parse_file.coroutine(
        file_path="/home/gem/user-data/uploads/upload.pdf",
        ocr_engine="disable",
        runtime=_runtime(),
    )

    assert events == [("enter", "file-thread-1"), ("exit", "file-thread-1")]
