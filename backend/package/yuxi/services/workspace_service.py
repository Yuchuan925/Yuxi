from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from yuxi.agents.backends.sandbox.backend import FileTransferLimitError
from yuxi.agents.backends.sandbox.paths import (
    ensure_user_workspace,
    workspace_uid_dirname,
)
from yuxi.config import get_runtime_dir
from yuxi.services.file_preview import (
    MAX_BINARY_PREVIEW_SIZE_BYTES,
    OfficePreviewConversionError,
    convert_office_to_pdf,
    detect_media_type,
    detect_preview_type,
    is_binary_preview_type,
    is_office_pdf_preview_file,
    render_preview_payload,
    render_preview_too_large_payload,
)
from yuxi.services.mention_search_service import invalidate_workspace_mention_cache
from yuxi.services.workspace_filesystem import WorkspaceFilesystem
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import utc_isoformat_from_timestamp
from yuxi.utils.paths import VIRTUAL_PATH_WORKSPACE
from yuxi.utils.upload_utils import MAX_UPLOAD_SIZE_BYTES, write_upload_to_path

EDITABLE_WORKSPACE_SUFFIXES = {".md", ".markdown", ".mdx", ".txt"}
MAX_WORKSPACE_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_BYTES
MAX_WORKSPACE_UPLOAD_FILES = 50
MAX_WORKSPACE_DOWNLOAD_SIZE_BYTES = 1024 * 1024 * 1024

# 搜索返回条数上限，避免超大工作区一次性返回过多结果
WORKSPACE_SEARCH_MAX_RESULTS = 100


async def search_workspace_files(*, query: str, current_user: User) -> dict:
    """按文件名在个人工作区内递归搜索，仅返回文件条目。"""
    normalized_query = (query or "").strip().lower()
    if not normalized_query:
        return {"entries": []}

    response = await list_workspace_tree(
        path="/",
        recursive=True,
        files_only=True,
        current_user=current_user,
    )
    entries = [
        entry for entry in response.get("entries", []) if normalized_query in str(entry.get("name") or "").lower()
    ]
    return {"entries": entries[:WORKSPACE_SEARCH_MAX_RESULTS]}


async def list_workspace_tree(
    *,
    path: str,
    recursive: bool = False,
    files_only: bool = False,
    current_user: User,
) -> dict:
    backend = _workspace_backend(current_user)
    virtual_path = _workspace_virtual_path(path)
    try:
        entries = await asyncio.to_thread(
            _list_workspace_directory,
            backend,
            virtual_path,
            recursive=recursive,
            files_only=files_only,
        )
    except FileNotFoundError:
        return {"entries": []}
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="当前路径不是目录") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    return {"entries": entries}


async def read_workspace_file_bytes(*, path: str, current_user: User) -> tuple[str, bytes]:
    """在 no-follow Workspace 边界内读取知识库导入文件。"""
    backend = _workspace_backend(current_user)
    virtual_path = _workspace_virtual_path(path)
    try:
        content = await asyncio.to_thread(
            backend.read_authorized_file,
            virtual_path,
            MAX_WORKSPACE_UPLOAD_SIZE_BYTES,
        )
    except FileTransferLimitError as exc:
        raise HTTPException(status_code=400, detail="文件过大，当前仅支持 100 MB 以内的工作区文件") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"工作区文件不存在: {path}") from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail=f"当前路径不是文件: {path}") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    return PurePosixPath(virtual_path).name, content


async def read_workspace_file_content(*, path: str, current_user: User) -> dict | StreamingResponse:
    backend = _workspace_backend(current_user)
    virtual_path = _workspace_virtual_path(path)
    try:
        raw_content = await asyncio.to_thread(
            backend.read_authorized_file,
            virtual_path,
            MAX_BINARY_PREVIEW_SIZE_BYTES,
        )
    except FileTransferLimitError:
        return render_preview_too_large_payload()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail="当前路径不是文件") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc

    if is_office_pdf_preview_file(path):
        file_name = PurePosixPath(virtual_path).name
        pdf_content = await _convert_workspace_office_to_pdf(current_user, virtual_path, file_name, raw_content)
        return _preview_binary_response(
            filename=f"{PurePosixPath(file_name).stem or 'preview'}.pdf",
            content=pdf_content,
            media_type="application/pdf",
            preview_type="pdf",
        )

    preview_type, supported, message = detect_preview_type(path, raw_content)
    if is_binary_preview_type(preview_type) and supported:
        return _preview_binary_response(
            filename=PurePosixPath(virtual_path).name or "preview",
            content=raw_content,
            media_type=detect_media_type(path, raw_content),
            preview_type=preview_type,
        )
    if not supported:
        return {
            "content": None,
            "preview_type": preview_type,
            "supported": False,
            "message": message,
            "truncated": False,
            "limit": None,
        }
    return render_preview_payload(path, raw_content)


async def write_workspace_file_content(*, path: str, content: str, current_user: User) -> dict:
    backend = _workspace_backend(current_user)
    virtual_path = _workspace_virtual_path(path)
    if PurePosixPath(virtual_path).suffix.lower() not in EDITABLE_WORKSPACE_SUFFIXES:
        raise HTTPException(status_code=400, detail="当前文件类型不支持编辑")

    try:
        raw_content = await asyncio.to_thread(
            backend.read_authorized_file,
            virtual_path,
            MAX_WORKSPACE_UPLOAD_SIZE_BYTES,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail="当前路径是目录") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    preview_type, supported, _message = detect_preview_type(path, raw_content)
    if preview_type not in {"markdown", "text"} or not supported:
        raise HTTPException(status_code=400, detail="当前文件类型不支持编辑")
    try:
        raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="当前文件不是 UTF-8 文本") from exc

    try:
        item = await asyncio.to_thread(backend.write_authorized_file, virtual_path, content.encode("utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail="当前路径是目录") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc

    return {
        "success": True,
        "path": _normalize_workspace_path(path).as_posix(),
        "entry": _entry_from_metadata(virtual_path, item),
    }


async def delete_workspace_path(*, path: str, current_user: User) -> dict:
    backend = _workspace_backend(current_user)
    virtual_path = _workspace_virtual_path(path)
    if virtual_path == VIRTUAL_PATH_WORKSPACE:
        raise HTTPException(status_code=400, detail="工作区根目录不允许删除")

    try:
        await asyncio.to_thread(
            backend.delete_authorized_path,
            virtual_path,
            root=VIRTUAL_PATH_WORKSPACE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc

    await invalidate_workspace_mention_cache(str(current_user.uid))
    return {"success": True, "path": _normalize_workspace_path(path).as_posix()}


async def create_workspace_directory(*, parent_path: str, name: str, current_user: User) -> dict:
    backend = _workspace_backend(current_user)
    directory_name = _validate_child_name(name, field_name="文件夹名")
    virtual_parent = _workspace_virtual_path(parent_path)
    target = f"{virtual_parent.rstrip('/')}/{directory_name}"

    try:
        item = await asyncio.to_thread(
            backend.create_authorized_directory,
            virtual_parent,
            directory_name,
            root=VIRTUAL_PATH_WORKSPACE,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail="同名文件或文件夹已存在") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="目标目录不存在") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc

    await invalidate_workspace_mention_cache(str(current_user.uid))
    return {"success": True, "entry": _entry_from_metadata(target, item)}


async def upload_workspace_files(*, parent_path: str, files: list[UploadFile], current_user: User) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="请选择至少一个文件")
    if len(files) > MAX_WORKSPACE_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_WORKSPACE_UPLOAD_FILES} 个文件")

    backend = _workspace_backend(current_user)
    parent = _workspace_virtual_path(parent_path)
    try:
        parent_stat = await asyncio.to_thread(backend.stat_authorized_path, parent, root=VIRTUAL_PATH_WORKSPACE)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="目标目录不存在") from exc
    except (PermissionError, NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    if not parent_stat["is_dir"]:
        raise HTTPException(status_code=400, detail="目标路径不是目录")
    seen_names = set()
    upload_targets: list[tuple[UploadFile, str]] = []

    for file in files:
        file_name = _validate_child_name(Path(file.filename or "").name, field_name="文件名")
        if file_name in seen_names:
            raise HTTPException(status_code=400, detail=f"选择的文件中存在重复文件名: {file_name}")
        seen_names.add(file_name)
        upload_targets.append((file, f"{parent.rstrip('/')}/{file_name}"))

    completed_entries: list[tuple[str, dict]] = []
    try:
        for file, target in upload_targets:
            item = await _write_workspace_upload(file, backend, target)
            completed_entries.append((target, item))
    except HTTPException:
        for target, _item in completed_entries:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(
                    backend.delete_authorized_path,
                    target,
                    root=VIRTUAL_PATH_WORKSPACE,
                )
        raise

    await invalidate_workspace_mention_cache(str(current_user.uid))
    entries = [_entry_from_metadata(target, item) for target, item in completed_entries]
    return {"success": True, "entries": entries}


async def download_workspace_file(*, path: str, current_user: User) -> FileResponse:
    backend = _workspace_backend(current_user)
    virtual_path = _workspace_virtual_path(path)
    file_name = PurePosixPath(virtual_path).name or "download"
    media_type = detect_media_type(file_name)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"}
    descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-workspace-download-", suffix=PurePosixPath(file_name).suffix)
    os.close(descriptor)
    try:
        await asyncio.to_thread(
            backend.download_authorized_file_to_path,
            virtual_path,
            temp_path,
            MAX_WORKSPACE_DOWNLOAD_SIZE_BYTES,
        )
    except FileNotFoundError as exc:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    except FileTransferLimitError as exc:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)
        raise HTTPException(status_code=413, detail="文件超过下载大小限制") from exc
    except (PermissionError, IsADirectoryError, NotADirectoryError, ValueError) as exc:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)
        raise HTTPException(status_code=403, detail="Access denied") from exc
    return FileResponse(
        path=temp_path,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(os.unlink, temp_path),
    )


def _workspace_backend(user: User) -> WorkspaceFilesystem:
    """物化并返回 uid 级 no-follow 文件系统。"""
    backend = WorkspaceFilesystem(str(user.uid))
    try:
        ensure_user_workspace(str(user.uid))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Access denied") from exc
    return backend


def _normalize_workspace_path(path: str | None) -> PurePosixPath:
    raw_path = (path or "/").strip() or "/"
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"
    normalized = PurePosixPath(raw_path)
    if ".." in normalized.parts:
        raise HTTPException(status_code=403, detail="Access denied")
    return normalized


def _workspace_virtual_path(path: str | None) -> str:
    normalized = _normalize_workspace_path(path)
    if normalized.as_posix() == "/":
        return VIRTUAL_PATH_WORKSPACE
    return f"{VIRTUAL_PATH_WORKSPACE.rstrip('/')}{normalized.as_posix()}"


def _validate_child_name(name: str, *, field_name: str) -> str:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail=f"{field_name} 不能为空")
    if clean_name in {".", ".."} or "/" in clean_name or "\\" in clean_name:
        raise HTTPException(status_code=422, detail=f"{field_name} 不能包含路径分隔符")
    if PurePosixPath(clean_name).name != clean_name:
        raise HTTPException(status_code=422, detail=f"{field_name} 不能包含路径分隔符")
    return clean_name


def _entry_from_metadata(virtual_path: str, item: dict) -> dict:
    is_dir = bool(item["is_dir"])
    relative = PurePosixPath(virtual_path).relative_to(PurePosixPath(VIRTUAL_PATH_WORKSPACE)).as_posix()
    display_path = f"/{relative}" if relative != "." else "/"
    if is_dir and display_path != "/" and not display_path.endswith("/"):
        display_path = f"{display_path}/"
    display_virtual_path = VIRTUAL_PATH_WORKSPACE if display_path == "/" else f"{VIRTUAL_PATH_WORKSPACE}{display_path}"
    return {
        "path": display_path,
        "virtual_path": display_virtual_path,
        "name": PurePosixPath(display_virtual_path.rstrip("/")).name or "工作区",
        "is_dir": is_dir,
        "size": 0 if is_dir else int(item.get("size") or 0),
        "modified_at": utc_isoformat_from_timestamp(float(item.get("modified_at") or 0)) or "",
    }


def _sort_entries(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").lower()))


def _list_workspace_directory(
    backend: WorkspaceFilesystem,
    target: str,
    *,
    recursive: bool = False,
    files_only: bool = False,
) -> list[dict]:
    children = backend.list_authorized_directory(target, root=VIRTUAL_PATH_WORKSPACE)
    entries = []
    child_directories = []
    for child in children:
        child_path = f"{target.rstrip('/')}/{child['name']}"
        if not files_only or not child["is_dir"]:
            entries.append(_entry_from_metadata(child_path, child))
        if child["is_dir"]:
            child_directories.append(child_path)
    if recursive:
        for child_path in child_directories:
            entries.extend(
                _list_workspace_directory(
                    backend,
                    child_path,
                    recursive=True,
                    files_only=files_only,
                )
            )
    return _sort_entries(entries)


def _preview_binary_response(*, filename: str, content: bytes, media_type: str, preview_type: str) -> StreamingResponse:
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
        "X-Yuxi-Preview-Type": preview_type,
        "X-Yuxi-Preview-Filename": quote(filename),
    }
    return StreamingResponse(io.BytesIO(content), media_type=media_type, headers=headers)


async def _write_workspace_upload(file: UploadFile, backend: WorkspaceFilesystem, target: str) -> dict:
    descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-workspace-upload-")
    os.close(descriptor)
    try:
        await write_upload_to_path(
            file,
            Path(temp_path),
            max_size_bytes=MAX_WORKSPACE_UPLOAD_SIZE_BYTES,
            too_large_message="文件过大，当前仅支持 100 MB 以内的文件",
        )
        return await asyncio.to_thread(
            backend.upload_authorized_file_from_path,
            target,
            temp_path,
            overwrite=False,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail="同名文件或文件夹已存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)


async def _convert_workspace_office_to_pdf(
    user: User,
    virtual_path: str,
    file_name: str,
    content: bytes,
) -> bytes:
    cache_dir = get_runtime_dir() / "cache" / "office-previews" / workspace_uid_dirname(str(user.uid))
    digest = hashlib.sha256(virtual_path.encode("utf-8")).hexdigest()
    content_digest = hashlib.sha256(content).hexdigest()
    cache_path = cache_dir / f"{digest}-{content_digest}.pdf"

    try:
        return await asyncio.to_thread(cache_path.read_bytes)
    except FileNotFoundError:
        pass

    try:
        pdf_content = await convert_office_to_pdf(file_name, content)
    except OfficePreviewConversionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await asyncio.to_thread(_store_office_pdf_cache, cache_dir, digest, cache_path, pdf_content)
    return pdf_content


def _store_office_pdf_cache(cache_dir: Path, digest: str, cache_path: Path, pdf_content: bytes) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob(f"{digest}-*.pdf"):
        if stale != cache_path:
            stale.unlink(missing_ok=True)
    cache_path.write_bytes(pdf_content)
