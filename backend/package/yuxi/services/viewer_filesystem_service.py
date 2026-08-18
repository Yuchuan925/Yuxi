"""AgentPanel Viewer 的实时 Project Workdir 文件服务。"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from yuxi.agents.backends.sandbox.backend import FileTransferLimitError
from yuxi.services.file_preview import (
    MAX_BINARY_PREVIEW_SIZE_BYTES,
    OfficePreviewConversionError,
    convert_office_to_pdf,
    detect_media_type,
    is_binary_preview_type,
    is_office_pdf_preview_file,
    render_preview_payload,
    render_preview_too_large_payload,
)
from yuxi.services.mention_search_service import invalidate_mention_cache
from yuxi.services.project_workdir_service import ProjectWorkdirBinding, resolve_project_workdir_binding
from yuxi.utils.datetime_utils import utc_isoformat_from_timestamp
from yuxi.utils.upload_utils import write_upload_to_path

SEARCH_MAX_RESULTS = 100
SEARCH_MAX_DIRECTORIES = 600
MAX_VIEWER_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_VIEWER_DOWNLOAD_BYTES = 1024 * 1024 * 1024


def _normalize_viewer_path(binding: ProjectWorkdirBinding, path: str | None) -> str:
    raw = str(path or "/").strip() or "/"
    if raw == "/":
        return binding.workdir_path
    candidate = str(PurePosixPath(raw if raw.startswith("/") else f"/{raw}"))
    if ".." in PurePosixPath(raw).parts:
        raise HTTPException(status_code=403, detail="Access denied")
    if candidate != binding.workdir_path and not candidate.startswith(f"{binding.workdir_path}/"):
        raise HTTPException(status_code=403, detail="Viewer 只允许访问当前 Project Workdir")
    return candidate


async def _viewer_state(*, thread_id: str, current_user, db) -> tuple[ProjectWorkdirBinding, object]:
    binding = await resolve_project_workdir_binding(
        thread_id=thread_id,
        uid=str(current_user.uid),
        db=db,
    )
    backend = binding.create_file_backend(create_if_missing=True)
    await asyncio.to_thread(backend.ensure_available)
    return binding, backend


def _entry(binding: ProjectWorkdirBinding, parent: str, item: dict) -> dict:
    path = f"{parent.rstrip('/')}/{item['name']}"
    is_dir = bool(item.get("is_dir"))
    return {
        "path": f"{path}/" if is_dir else path,
        "name": str(item["name"]),
        "is_dir": is_dir,
        "size": int(item.get("size") or 0),
        "modified_at": utc_isoformat_from_timestamp(float(item.get("modified_at") or 0)) or "",
        "artifact_url": None if is_dir else f"/api/chat/thread/{binding.thread_id}/artifacts/{path.lstrip('/')}",
    }


async def _list_directory(binding: ProjectWorkdirBinding, backend, path: str) -> list[dict]:
    try:
        items = await asyncio.to_thread(backend.list_authorized_directory, path, root=binding.workdir_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="目录不存在") from exc
    return sorted(
        (_entry(binding, path, item) for item in items),
        key=lambda item: (not item["is_dir"], item["name"].lower()),
    )


async def list_viewer_filesystem_tree(*, thread_id: str, path: str, current_user, db) -> dict:
    """列出实时 Project Workdir；根路径 `/` 直接表示 Workdir 根。"""
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    normalized = _normalize_viewer_path(binding, path)
    return {"entries": await _list_directory(binding, backend, normalized)}


async def search_viewer_files(*, thread_id: str, query: str, current_user, db) -> dict:
    """在实时 Workdir 内按文件名搜索。"""
    normalized_query = str(query or "").strip().lower()
    if not normalized_query:
        return {"entries": []}
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    matches: list[dict] = []
    pending = [binding.workdir_path]
    visited = 0
    while pending and len(matches) < SEARCH_MAX_RESULTS and visited < SEARCH_MAX_DIRECTORIES:
        directory = pending.pop(0)
        visited += 1
        for item in await _list_directory(binding, backend, directory):
            if item["is_dir"]:
                pending.append(str(item["path"]).rstrip("/"))
            elif normalized_query in str(item["name"]).lower():
                matches.append(item)
                if len(matches) >= SEARCH_MAX_RESULTS:
                    break
    return {"entries": matches}


async def _download_to_temp(backend, path: str, max_bytes: int) -> str:
    descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-viewer-", suffix=PurePosixPath(path).suffix)
    os.close(descriptor)
    try:
        await asyncio.to_thread(backend.download_authorized_file_to_path, path, temp_path, max_bytes)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
    return temp_path


def _binary_preview_response(path: str, raw_content: bytes, preview_type: str) -> StreamingResponse:
    file_name = PurePosixPath(path).name or "preview"
    return StreamingResponse(
        iter([raw_content]),
        media_type=detect_media_type(file_name, raw_content),
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name)}",
            "X-Yuxi-Preview-Type": preview_type,
            "X-Yuxi-Preview-Filename": quote(file_name),
        },
    )


async def _render_preview(path: str, raw_content: bytes) -> dict | StreamingResponse:
    if is_office_pdf_preview_file(path):
        try:
            raw_content = await convert_office_to_pdf(path, raw_content)
        except OfficePreviewConversionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _binary_preview_response(f"{PurePosixPath(path).stem or 'preview'}.pdf", raw_content, "pdf")
    payload = render_preview_payload(path, raw_content)
    if is_binary_preview_type(payload["preview_type"]) and payload["supported"]:
        return _binary_preview_response(path, raw_content, payload["preview_type"])
    return payload


async def read_viewer_file_content(*, thread_id: str, path: str, current_user, db) -> dict | StreamingResponse:
    """从实时 Workdir 读取预览，不经过 MinIO revision。"""
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    normalized = _normalize_viewer_path(binding, path)
    try:
        temp_path = await _download_to_temp(backend, normalized, MAX_BINARY_PREVIEW_SIZE_BYTES)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail="路径不是普通文件") from exc
    except FileTransferLimitError:
        return render_preview_too_large_payload()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    try:
        raw_content = await asyncio.to_thread(Path(temp_path).read_bytes)
    finally:
        await asyncio.to_thread(os.unlink, temp_path)
    return await _render_preview(normalized, raw_content)


async def download_viewer_file(*, thread_id: str, path: str, current_user, db) -> FileResponse:
    """从实时 Workdir 流式下载普通文件。"""
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    normalized = _normalize_viewer_path(binding, path)
    try:
        temp_path = await _download_to_temp(backend, normalized, MAX_VIEWER_DOWNLOAD_BYTES)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail="路径不是普通文件") from exc
    except FileTransferLimitError as exc:
        raise HTTPException(status_code=413, detail="文件超过下载大小限制") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    file_name = PurePosixPath(normalized).name or "download"
    return FileResponse(
        temp_path,
        media_type=mimetypes.guess_type(file_name)[0] or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"},
        background=BackgroundTask(os.unlink, temp_path),
    )


async def delete_viewer_file(*, thread_id: str, path: str, current_user, db) -> dict:
    """实时删除 Workdir 内文件或目录。"""
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    normalized = _normalize_viewer_path(binding, path)
    if normalized == binding.workdir_path:
        raise HTTPException(status_code=400, detail="Project Workdir 根目录不允许删除")
    try:
        await asyncio.to_thread(backend.delete_authorized_path, normalized, root=binding.workdir_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    await invalidate_mention_cache(binding.thread_id)
    return {"success": True, "path": normalized}


async def create_viewer_directory(*, thread_id: str, parent_path: str, name: str, current_user, db) -> dict:
    """实时创建 Workdir 目录。"""
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    parent = _normalize_viewer_path(binding, parent_path)
    try:
        path = await asyncio.to_thread(
            backend.create_authorized_directory,
            parent,
            str(name or "").strip(),
            root=binding.workdir_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await invalidate_mention_cache(binding.thread_id)
    return {
        "entry": {
            "path": f"{path}/",
            "name": PurePosixPath(path).name,
            "is_dir": True,
            "size": 0,
            "modified_at": "",
        }
    }


async def upload_viewer_files(*, thread_id: str, parent_path: str, files: list[UploadFile], current_user, db) -> dict:
    """把用户上传直接写入实时 Workdir。"""
    binding, backend = await _viewer_state(thread_id=thread_id, current_user=current_user, db=db)
    parent = _normalize_viewer_path(binding, parent_path)
    entries: list[dict] = []
    for upload in files:
        file_name = PurePosixPath(str(upload.filename or "")).name
        if not file_name or file_name in {".", ".."}:
            raise HTTPException(status_code=400, detail="无法识别的文件名")
        descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-viewer-upload-")
        os.close(descriptor)
        try:
            size = await write_upload_to_path(
                upload,
                Path(temp_path),
                max_size_bytes=MAX_VIEWER_UPLOAD_BYTES,
                too_large_message="文件过大",
            )
            target = f"{parent.rstrip('/')}/{file_name}"
            try:
                await asyncio.to_thread(backend.upload_authorized_file_from_path, target, temp_path)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail="Access denied") from exc
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="目录不存在") from exc
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        entries.append(
            {
                "path": target,
                "name": file_name,
                "is_dir": False,
                "size": size,
                "modified_at": "",
            }
        )
    await invalidate_mention_cache(binding.thread_id)
    return {"entries": entries}
