from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.agents.backends.sandbox import paths as sandbox_paths
from yuxi.services import viewer_filesystem_service as svc
from yuxi.services import workspace_service


@pytest.fixture(autouse=True)
def stub_output_snapshot(monkeypatch: pytest.MonkeyPatch):
    async def fake_snapshot(**_kwargs):
        return SimpleNamespace(id=1), None, []

    monkeypatch.setattr(svc, "get_user_output_snapshot", fake_snapshot)


def _prepare_symlink_escape(tmp_path: Path, monkeypatch) -> tuple[str, str]:
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    sandbox_paths.ensure_thread_dirs(thread_id, uid)
    return thread_id, uid


def test_resolve_local_user_data_path_blocks_upload_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    thread_id, uid = _prepare_symlink_escape(tmp_path, monkeypatch)

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    escape_link = sandbox_paths.sandbox_uploads_dir(thread_id) / "escape.txt"
    escape_link.symlink_to(outside_file)

    with pytest.raises(HTTPException) as exc_info:
        svc._resolve_local_user_data_path(thread_id, uid, "/home/gem/user-data/uploads/escape.txt")

    assert exc_info.value.status_code == 403


def test_list_local_entries_skips_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    thread_id, uid = _prepare_symlink_escape(tmp_path, monkeypatch)

    uploads_dir = sandbox_paths.sandbox_uploads_dir(thread_id)
    (uploads_dir / "safe.txt").write_text("safe", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    (uploads_dir / "escape.txt").symlink_to(outside_file)

    entries = svc._list_local_entries(thread_id, uid, uploads_dir)

    assert {entry["path"] for entry in entries} == {"/home/gem/user-data/uploads/safe.txt"}


@pytest.mark.asyncio
async def test_download_published_output_releases_minio_response(monkeypatch) -> None:
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

    response = FakeResponse()
    minio = SimpleNamespace(adownload_response=lambda *_args: _async_value(response))

    monkeypatch.setattr(svc, "_resolve_viewer_state", lambda **_kwargs: _async_value((None, None, [])))
    monkeypatch.setattr(
        svc,
        "get_user_output_snapshot",
        lambda **_kwargs: _async_value(
            (
                SimpleNamespace(id=1),
                "revision-1",
                [
                    {
                        "path": "/home/gem/user-data/outputs/report.txt",
                        "bucket_name": "thread-files",
                        "object_name": "outputs/report.txt",
                        "content_type": "text/plain",
                    }
                ],
            )
        ),
    )
    monkeypatch.setattr(svc, "get_minio_client", lambda: minio)

    download = await svc.download_viewer_file(
        thread_id="thread-1",
        path="/home/gem/user-data/outputs/report.txt",
        current_user=SimpleNamespace(uid="user-1"),
        db=None,
    )
    body = b""
    async for chunk in download.body_iterator:
        body += chunk

    assert body == content
    assert response.closed is True
    assert response.released is True


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_read_viewer_workspace_office_file_returns_pdf_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    user = SimpleNamespace(uid=uid)
    sandbox_paths.ensure_thread_dirs(thread_id, uid)
    target = sandbox_paths.sandbox_workspace_dir(thread_id, uid) / "slides.pptx"
    target.write_bytes(b"presentation")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    async def fake_convert(filename: str, content: bytes) -> bytes:
        assert filename == "slides.pptx"
        assert content == b"presentation"
        return b"%PDF-1.4\npreview"

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(workspace_service, "convert_office_to_pdf", fake_convert)

    response = await svc.read_viewer_file_content(
        thread_id=thread_id,
        path="/home/gem/user-data/workspace/slides.pptx",
        current_user=user,
        db=None,
    )
    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    assert response.media_type == "application/pdf"
    assert response.headers["x-yuxi-preview-type"] == "pdf"
    assert body == b"%PDF-1.4\npreview"


@pytest.mark.asyncio
async def test_read_viewer_outputs_office_file_returns_pdf_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    user = SimpleNamespace(uid=uid)
    sandbox_paths.ensure_thread_dirs(thread_id, uid)
    target = sandbox_paths.sandbox_outputs_dir(thread_id) / "report.docx"
    target.write_bytes(b"document")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    async def fake_convert(filename: str, content: bytes) -> bytes:
        assert filename.endswith("report.docx")
        assert content == b"document"
        return b"%PDF-1.4\npreview"

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(svc, "convert_office_to_pdf", fake_convert)

    response = await svc.read_viewer_file_content(
        thread_id=thread_id,
        path="/home/gem/user-data/outputs/report.docx",
        current_user=user,
        db=None,
    )
    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    assert response.media_type == "application/pdf"
    assert response.headers["x-yuxi-preview-type"] == "pdf"
    assert response.headers["x-yuxi-preview-filename"].endswith("report.pdf")
    assert body == b"%PDF-1.4\npreview"


@pytest.mark.asyncio
async def test_read_viewer_outputs_office_conversion_failure_returns_400(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    user = SimpleNamespace(uid=uid)
    sandbox_paths.ensure_thread_dirs(thread_id, uid)
    target = sandbox_paths.sandbox_outputs_dir(thread_id) / "broken.docx"
    target.write_bytes(b"broken")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    async def fake_convert(filename: str, content: bytes) -> bytes:
        raise svc.OfficePreviewConversionError("Office 文件转换 PDF 失败: 损坏的文件")

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(svc, "convert_office_to_pdf", fake_convert)

    with pytest.raises(HTTPException) as exc_info:
        await svc.read_viewer_file_content(
            thread_id=thread_id,
            path="/home/gem/user-data/outputs/broken.docx",
            current_user=user,
            db=None,
        )

    assert exc_info.value.status_code == 400
    assert "转换 PDF 失败" in exc_info.value.detail


@pytest.mark.asyncio
async def test_search_viewer_files_matches_filenames_recursively(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    thread_id = "thread-1"
    uid = "user-1"
    user = SimpleNamespace(uid=uid)
    sandbox_paths.ensure_thread_dirs(thread_id, uid)
    outputs_dir = sandbox_paths.sandbox_outputs_dir(thread_id)
    (outputs_dir / "年度报告.docx").write_bytes(b"report")
    nested_dir = outputs_dir / "分组"
    nested_dir.mkdir()
    (nested_dir / "record-2026.md").write_text("record", encoding="utf-8")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)

    response = await svc.search_viewer_files(thread_id=thread_id, query="record", current_user=user, db=None)
    assert [entry["name"] for entry in response["entries"]] == ["record-2026.md"]
    assert response["entries"][0]["path"] == "/home/gem/user-data/outputs/分组/record-2026.md"

    chinese_response = await svc.search_viewer_files(thread_id=thread_id, query="报告", current_user=user, db=None)
    assert [entry["name"] for entry in chinese_response["entries"]] == ["年度报告.docx"]

    empty = await svc.search_viewer_files(thread_id=thread_id, query="  ", current_user=user, db=None)
    assert empty["entries"] == []


@pytest.mark.asyncio
async def test_search_viewer_files_skips_unreadable_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SAVE_DIR", str(tmp_path))
    thread_id = "thread-1"
    user = SimpleNamespace(uid="user-1")

    async def fake_resolve_viewer_state(**kwargs):
        return None, None, []

    visited: list[str] = []

    async def flaky_listing(*args, **kwargs):
        normalized_path = kwargs["normalized_path"]
        visited.append(normalized_path)
        if normalized_path == "/":
            return [{"path": "/home/gem/user-data/workspace", "name": "workspace", "is_dir": True}]
        if normalized_path == "/home/gem/user-data/workspace":
            raise HTTPException(status_code=400, detail="目录不可读")
        return []

    monkeypatch.setattr(svc, "_resolve_viewer_state", fake_resolve_viewer_state)
    monkeypatch.setattr(svc, "_list_viewer_directory_entries", flaky_listing)

    response = await svc.search_viewer_files(thread_id=thread_id, query="anything", current_user=user, db=None)
    assert response["entries"] == []
    assert "/home/gem/user-data/workspace" in visited
