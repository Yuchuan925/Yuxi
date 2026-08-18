from __future__ import annotations

import asyncio
import io
import mimetypes
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.backends import create_agent_composite_backend
from yuxi.agents.backends.sandbox import (
    SKILLS_PATH,
    USER_DATA_PATH,
    ensure_thread_dirs,
    resolve_virtual_path,
    sandbox_user_data_dir,
    sandbox_workspace_dir,
    virtual_path_for_thread_file,
)
from yuxi.agents.backends.skills_backend import SelectedSkillsReadonlyBackend
from yuxi.agents.skills.service import normalize_string_list
from yuxi.services.agent_runtime_service import resolve_thread_agent_runtime_context
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
from yuxi.services.workspace_service import (
    create_workspace_directory as create_workspace_directory_entry,
)
from yuxi.services.thread_output_service import (
    find_output_descriptor,
    get_user_output_snapshot,
    list_output_entries,
    publish_output_manifest,
)
from yuxi.storage.minio import StorageError, get_minio_client
from yuxi.services.workspace_service import (
    delete_workspace_path,
    list_workspace_tree,
)
from yuxi.services.workspace_service import (
    download_workspace_file as download_workspace_file_response,
)
from yuxi.services.workspace_service import (
    read_workspace_file_content as read_workspace_file_content_response,
)
from yuxi.services.workspace_service import (
    upload_workspace_files as upload_workspace_files_entry,
)
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import utc_isoformat_from_timestamp
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS, VIRTUAL_PATH_UPLOADS, VIRTUAL_PATH_WORKSPACE

_PROTECTED_USER_DATA_ROOTS = frozenset(
    {
        USER_DATA_PATH,
        VIRTUAL_PATH_WORKSPACE,
        VIRTUAL_PATH_UPLOADS,
        VIRTUAL_PATH_OUTPUTS,
    }
)


def _normalize_path(path: str | None) -> str:
    normalized = (path or "/").strip() or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/") if normalized not in {"/", SKILLS_PATH, USER_DATA_PATH} else normalized


def _resolve_local_user_data_path(thread_id: str, uid: str, path: str) -> Path:
    try:
        actual_path = resolve_virtual_path(thread_id, path, uid=uid)
    except ValueError as exc:
        # 真实路径越过允许根目录时，按权限拒绝处理，而不是当作普通参数错误。
        if "path traversal" in str(exc):
            raise HTTPException(status_code=403, detail="Access denied") from exc
        raise
    resolved_path = actual_path.resolve()
    user_data_root = sandbox_user_data_dir(thread_id).resolve()
    workspace_root = sandbox_workspace_dir(thread_id, uid).resolve()
    if not (resolved_path.is_relative_to(user_data_root) or resolved_path.is_relative_to(workspace_root)):
        raise HTTPException(status_code=403, detail="Access denied")
    return resolved_path


def _is_user_data_path(path: str) -> bool:
    return path == USER_DATA_PATH or path.startswith(f"{USER_DATA_PATH}/")


def _is_workspace_path(path: str) -> bool:
    return path == VIRTUAL_PATH_WORKSPACE or path.startswith(f"{VIRTUAL_PATH_WORKSPACE}/")


def _is_outputs_path(path: str) -> bool:
    return path == VIRTUAL_PATH_OUTPUTS or path.startswith(f"{VIRTUAL_PATH_OUTPUTS}/")


def _is_skills_path(path: str) -> bool:
    return path == SKILLS_PATH or path.startswith(f"{SKILLS_PATH}/")


def _is_in_home_gem(path: str) -> bool:
    """检查路径是否在 /home/gem/ 下但不在虚拟挂载点内"""
    if not path.startswith("/home/gem/"):
        return False
    # 排除虚拟挂载点
    if path.startswith(f"{USER_DATA_PATH}/") or path == USER_DATA_PATH:
        return False
    if path.startswith(f"{SKILLS_PATH}/") or path == SKILLS_PATH:
        return False
    return True


def _strip_skills_prefix(path: str) -> str:
    if path == SKILLS_PATH:
        return "/"
    return path[len(SKILLS_PATH) :] or "/"


def _remap_prefixed_entry(entry: dict, prefix: str) -> dict:
    raw_path = str(entry.get("path") or "")
    is_dir = bool(entry.get("is_dir", False))
    remapped = f"{prefix}{raw_path}" if raw_path != "/" else f"{prefix}/"
    if is_dir and not remapped.endswith("/"):
        remapped = f"{remapped}/"
    return {
        "path": remapped,
        "name": PurePosixPath(remapped.rstrip("/")).name or remapped,
        "is_dir": is_dir,
        "size": int(entry.get("size", 0) or 0),
        "modified_at": str(entry.get("modified_at", "") or ""),
    }


def _sort_entries(entries: list[dict]) -> list[dict]:
    """Sort entries: folders first, then files alphabetically."""
    return sorted(
        entries,
        key=lambda e: (
            not bool(e.get("is_dir")),
            PurePosixPath(str(e.get("path") or "").rstrip("/")).name.lower(),
        ),
    )


def _preview_too_large_payload() -> dict:
    return render_preview_too_large_payload()


def _preview_binary_response(path: str, raw_content: bytes, preview_type: str) -> StreamingResponse:
    file_name = PurePosixPath(path).name or "preview"
    media_type = detect_media_type(file_name, raw_content)
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name)}",
        "X-Yuxi-Preview-Type": preview_type,
        "X-Yuxi-Preview-Filename": quote(file_name),
    }
    return StreamingResponse(io.BytesIO(raw_content), media_type=media_type, headers=headers)


async def _render_viewer_preview(path: str, raw_content: bytes) -> dict | StreamingResponse:
    if len(raw_content) > MAX_BINARY_PREVIEW_SIZE_BYTES:
        return _preview_too_large_payload()
    # docx/pptx 是 ZIP 二进制，与工作区一致先转换为 PDF 再预览，避免被判定为二进制文件不可预览
    if is_office_pdf_preview_file(path):
        try:
            pdf_content = await convert_office_to_pdf(path, raw_content)
        except OfficePreviewConversionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _preview_binary_response(f"{PurePosixPath(path).stem or 'preview'}.pdf", pdf_content, "pdf")
    payload = render_preview_payload(path, raw_content)
    if is_binary_preview_type(payload["preview_type"]) and payload["supported"]:
        return _preview_binary_response(path, raw_content, payload["preview_type"])
    return payload


def _entry_for_local_path(thread_id: str, uid: str, path: Path, listing_root: Path) -> dict:
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(listing_root):
        raise HTTPException(status_code=403, detail="Access denied")
    stat = resolved_path.stat()
    is_dir = resolved_path.is_dir()
    display_path = virtual_path_for_thread_file(thread_id, resolved_path, uid=uid)
    if is_dir and not display_path.endswith("/"):
        display_path = f"{display_path}/"
    return {
        "path": display_path,
        "name": resolved_path.name,
        "is_dir": is_dir,
        "size": 0 if is_dir else stat.st_size,
        "modified_at": utc_isoformat_from_timestamp(stat.st_mtime) or "",
    }


def _list_local_entries(thread_id: str, uid: str, actual_path) -> list[dict]:
    """List a local directory and remap children back into viewer virtual paths."""
    listing_root = actual_path.resolve()
    entries: list[dict] = []
    for child in sorted(actual_path.iterdir(), key=lambda item: item.name.lower()):
        resolved_child = child.resolve()
        if not resolved_child.is_relative_to(listing_root):
            continue
        entries.append(_entry_for_local_path(thread_id, uid, resolved_child, listing_root))
    return entries


def _workspace_relative_path(path: str) -> str:
    if path == VIRTUAL_PATH_WORKSPACE:
        return "/"
    if not path.startswith(f"{VIRTUAL_PATH_WORKSPACE}/"):
        raise HTTPException(status_code=400, detail="当前路径不是工作区路径")
    return path[len(VIRTUAL_PATH_WORKSPACE) :] or "/"


def _viewer_entry_from_workspace_entry(entry: dict) -> dict:
    path = str(entry.get("virtual_path") or "")
    if not path:
        workspace_path = str(entry.get("path") or "/")
        path = VIRTUAL_PATH_WORKSPACE if workspace_path == "/" else f"{VIRTUAL_PATH_WORKSPACE}{workspace_path}"
    is_dir = bool(entry.get("is_dir", False))
    if is_dir and not path.endswith("/"):
        path = f"{path}/"
    return {
        "path": path,
        "name": str(entry.get("name", "") or PurePosixPath(path.rstrip("/")).name or path),
        "is_dir": is_dir,
        "size": int(entry.get("size", 0) or 0),
        "modified_at": str(entry.get("modified_at", "") or ""),
    }


def _viewer_response_from_workspace_response(response: dict) -> dict:
    result = {**response}
    if "entry" in result and isinstance(result["entry"], dict):
        result["entry"] = _viewer_entry_from_workspace_entry(result["entry"])
    if "entries" in result and isinstance(result["entries"], list):
        result["entries"] = [
            _viewer_entry_from_workspace_entry(entry) for entry in result["entries"] if isinstance(entry, dict)
        ]
    return result


def _list_user_data_root_entries(thread_id: str, uid: str) -> list[dict]:
    """Expose thread-root files while keeping the user workspace entry visible."""
    entries = _list_local_entries(thread_id, uid, sandbox_user_data_dir(thread_id))
    visible_paths = {str(entry.get("path") or "").rstrip("/") for entry in entries}
    workspace_dir = sandbox_workspace_dir(thread_id, uid)
    workspace_virtual_path = virtual_path_for_thread_file(thread_id, workspace_dir, uid=uid).rstrip("/")
    if workspace_virtual_path not in visible_paths:
        # workspace is stored outside the per-thread root, so add it explicitly when needed.
        stat = workspace_dir.stat()
        entries.append(
            {
                "path": f"{workspace_virtual_path}/",
                "name": workspace_dir.name,
                "is_dir": True,
                "size": 0,
                "modified_at": utc_isoformat_from_timestamp(stat.st_mtime) or "",
            }
        )
    return entries


async def _resolve_viewer_state(
    *,
    thread_id: str,
    current_user: User,
    db: AsyncSession,
):
    runtime_context = await resolve_thread_agent_runtime_context(
        thread_id=thread_id,
        user=current_user,
        db=db,
    )
    selected_skills = getattr(runtime_context, "_readable_skills", [])
    selected_skills = normalize_string_list(selected_skills if isinstance(selected_skills, list) else [])
    runtime_stub = type("RuntimeStub", (), {"context": runtime_context})()
    sandbox_backend = create_agent_composite_backend(runtime_stub)
    skills_backend = SelectedSkillsReadonlyBackend(selected_slugs=selected_skills)
    return sandbox_backend, skills_backend, selected_skills


async def _list_viewer_directory_entries(
    sandbox_backend,
    skills_backend,
    selected_skills,
    *,
    thread_id: str,
    current_user: User,
    normalized_path: str,
    output_revision_id: str | None,
    output_files: list[dict],
) -> list[dict]:
    """列出 viewer 命名空间内单个目录的条目，根目录返回虚拟命名空间入口。"""
    if normalized_path == "/":
        # 根目录只显示 viewer 暴露的虚拟命名空间，避免为只读树视图触发 sandbox 冷启动。
        entries = [{"path": f"{USER_DATA_PATH}/", "name": "user-data", "is_dir": True, "size": 0, "modified_at": ""}]
        if selected_skills:
            entries.append({"path": f"{SKILLS_PATH}/", "name": "skills", "is_dir": True, "size": 0, "modified_at": ""})
        return entries

    try:
        if _is_user_data_path(normalized_path):
            uid = str(current_user.uid)
            ensure_thread_dirs(thread_id, uid)
            if _is_workspace_path(normalized_path):
                response = await list_workspace_tree(
                    path=_workspace_relative_path(normalized_path),
                    current_user=current_user,
                )
                return [_viewer_entry_from_workspace_entry(entry) for entry in response.get("entries", [])]
            if _is_outputs_path(normalized_path) and output_revision_id:
                return list_output_entries(output_files, normalized_path)
            if normalized_path == USER_DATA_PATH:
                return await asyncio.to_thread(_list_user_data_root_entries, thread_id, uid)
            actual_path = _resolve_local_user_data_path(thread_id, uid, normalized_path)
            if not actual_path.exists():
                return []
            if not actual_path.is_dir():
                raise HTTPException(status_code=400, detail="当前路径不是目录")
            return await asyncio.to_thread(_list_local_entries, thread_id, uid, actual_path)

        if _is_skills_path(normalized_path):
            result = await asyncio.to_thread(skills_backend.ls, _strip_skills_prefix(normalized_path))
            if result.error:
                raise HTTPException(status_code=400, detail=result.error)
            return [_remap_prefixed_entry(entry, SKILLS_PATH) for entry in (result.entries or [])]
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    raise HTTPException(status_code=400, detail=f"Access denied: '{normalized_path}' is outside viewer namespace")


async def list_viewer_filesystem_tree(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> dict:
    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    normalized_path = _normalize_path(path)
    sandbox_backend, skills_backend, selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )
    _conversation, output_revision_id, output_files = await get_user_output_snapshot(
        thread_id=thread_id, uid=str(current_user.uid), db=db
    )
    entries = await _list_viewer_directory_entries(
        sandbox_backend,
        skills_backend,
        selected_skills,
        thread_id=thread_id,
        current_user=current_user,
        normalized_path=normalized_path,
        output_revision_id=output_revision_id,
        output_files=output_files,
    )
    return {"entries": _sort_entries(entries)}


# 搜索保护上限：匹配条数与遍历目录数，避免异常大的目录树拖垮请求
SEARCH_MAX_RESULTS = 100
SEARCH_MAX_DIRECTORIES = 600


async def search_viewer_files(
    *,
    thread_id: str,
    query: str,
    current_user: User,
    db: AsyncSession,
) -> dict:
    """按文件名在 viewer 命名空间内递归搜索，仅返回文件条目。"""
    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    normalized_query = (query or "").strip().lower()
    if not normalized_query:
        return {"entries": []}

    sandbox_backend, skills_backend, selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )
    _conversation, output_revision_id, output_files = await get_user_output_snapshot(
        thread_id=thread_id, uid=str(current_user.uid), db=db
    )

    matches: list[dict] = []
    pending: list[str] = ["/"]
    visited_directories = 0
    while pending and len(matches) < SEARCH_MAX_RESULTS and visited_directories < SEARCH_MAX_DIRECTORIES:
        directory_path = pending.pop(0)
        visited_directories += 1
        try:
            entries = await _list_viewer_directory_entries(
                sandbox_backend,
                skills_backend,
                selected_skills,
                thread_id=thread_id,
                current_user=current_user,
                normalized_path=directory_path,
                output_revision_id=output_revision_id,
                output_files=output_files,
            )
        except HTTPException as exc:
            logger.warning("搜索时跳过无法访问的目录 %s: %s", directory_path, exc.detail)
            continue
        for entry in entries:
            if entry.get("is_dir"):
                pending.append(str(entry.get("path") or "").rstrip("/"))
            elif normalized_query in str(entry.get("name") or "").lower():
                matches.append(entry)
    return {"entries": _sort_entries(matches)}


async def read_viewer_file_content(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> dict | StreamingResponse:
    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")
    normalized_path = _normalize_path(path)

    sandbox_backend, skills_backend, _selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )
    _conversation, output_revision_id, output_files = await get_user_output_snapshot(
        thread_id=thread_id, uid=str(current_user.uid), db=db
    )

    try:
        if _is_user_data_path(normalized_path):
            if _is_workspace_path(normalized_path):
                return await read_workspace_file_content_response(
                    path=_workspace_relative_path(normalized_path),
                    current_user=current_user,
                )
            if _is_outputs_path(normalized_path) and output_revision_id:
                descriptor = find_output_descriptor(output_files, normalized_path)
                if descriptor is None:
                    raise HTTPException(status_code=404, detail="文件不存在")
                if int(descriptor.get("size") or 0) > MAX_BINARY_PREVIEW_SIZE_BYTES:
                    return _preview_too_large_payload()
                try:
                    raw_content = await get_minio_client().adownload_file(
                        str(descriptor["bucket_name"]), str(descriptor["object_name"])
                    )
                except StorageError as exc:
                    raise HTTPException(status_code=404, detail="文件对象不存在") from exc
                return await _render_viewer_preview(normalized_path, raw_content)
            actual_path = _resolve_local_user_data_path(thread_id, str(current_user.uid), normalized_path)
            if not actual_path.exists():
                raise HTTPException(status_code=404, detail="文件不存在")
            if not actual_path.is_file():
                raise HTTPException(status_code=400, detail="当前路径是目录")
            if actual_path.stat().st_size > MAX_BINARY_PREVIEW_SIZE_BYTES:
                return _preview_too_large_payload()
            raw_content = await asyncio.to_thread(actual_path.read_bytes)
            return await _render_viewer_preview(normalized_path, raw_content)
        elif _is_skills_path(normalized_path):
            responses = await asyncio.to_thread(skills_backend.download_files, [_strip_skills_prefix(normalized_path)])
        elif _is_in_home_gem(normalized_path):
            # /home/gem/ 下的其他文件（如 workspace 目录）
            responses = await asyncio.to_thread(sandbox_backend.download_files, [normalized_path])
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Access denied: '{normalized_path}' is outside viewer namespace",
            )
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    response = responses[0] if responses else None
    if response is None or response.error == "file_not_found":
        raise HTTPException(status_code=404, detail="文件不存在")
    if response.error == "is_directory":
        raise HTTPException(status_code=400, detail="当前路径是目录")
    if response.error:
        raise HTTPException(status_code=400, detail=str(response.error))

    raw_content = response.content or b""
    return await _render_viewer_preview(normalized_path, raw_content)


async def download_viewer_file(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> StreamingResponse | FileResponse:
    normalized_path = _normalize_path(path)
    sandbox_backend, skills_backend, _selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )
    _conversation, output_revision_id, output_files = await get_user_output_snapshot(
        thread_id=thread_id, uid=str(current_user.uid), db=db
    )

    try:
        if _is_user_data_path(normalized_path):
            if _is_workspace_path(normalized_path):
                return await download_workspace_file_response(
                    path=_workspace_relative_path(normalized_path),
                    current_user=current_user,
                )
            if _is_outputs_path(normalized_path) and output_revision_id:
                descriptor = find_output_descriptor(output_files, normalized_path)
                if descriptor is None:
                    raise HTTPException(status_code=404, detail="文件不存在")
                try:
                    response = await get_minio_client().adownload_response(
                        str(descriptor["bucket_name"]), str(descriptor["object_name"])
                    )
                except StorageError as exc:
                    raise HTTPException(status_code=404, detail="文件对象不存在") from exc

                async def stream_object():
                    try:
                        while chunk := await asyncio.to_thread(response.read, 1024 * 1024):
                            yield chunk
                    finally:
                        response.close()
                        response.release_conn()

                file_name = PurePosixPath(normalized_path).name or "download"
                media_type = str(descriptor.get("content_type") or "application/octet-stream")
                headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"}
                return StreamingResponse(stream_object(), media_type=media_type, headers=headers)
            actual_path = _resolve_local_user_data_path(thread_id, str(current_user.uid), normalized_path)
            if not actual_path.exists():
                raise HTTPException(status_code=404, detail="文件不存在")
            if not actual_path.is_file():
                raise HTTPException(status_code=400, detail="当前路径是目录")

            file_name = actual_path.name or "download"
            media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            headers = {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
            }
            return FileResponse(path=actual_path, media_type=media_type, headers=headers)

        if _is_skills_path(normalized_path):
            responses = await asyncio.to_thread(skills_backend.download_files, [_strip_skills_prefix(normalized_path)])
        elif _is_in_home_gem(normalized_path):
            # /home/gem/ 下的其他文件（如 workspace 目录）
            responses = await asyncio.to_thread(sandbox_backend.download_files, [normalized_path])
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Access denied: '{normalized_path}' is outside viewer namespace",
            )
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    response = responses[0] if responses else None
    if response is None or response.error == "file_not_found":
        raise HTTPException(status_code=404, detail="文件不存在")
    if response.error == "is_directory":
        raise HTTPException(status_code=400, detail="当前路径是目录")
    if response.error:
        raise HTTPException(status_code=400, detail=str(response.error))

    file_name = PurePosixPath(normalized_path).name or "download"
    media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    stream = io.BytesIO(response.content or b"")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
    }
    return StreamingResponse(stream, media_type=media_type, headers=headers)


async def delete_viewer_file(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> dict:
    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    normalized_path = _normalize_path(path)
    if not _is_user_data_path(normalized_path):
        raise HTTPException(status_code=400, detail="当前路径不支持删除")
    if normalized_path in _PROTECTED_USER_DATA_ROOTS:
        raise HTTPException(status_code=400, detail="当前目录不允许删除")

    await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )
    conversation, output_revision_id, output_files = await get_user_output_snapshot(
        thread_id=thread_id, uid=str(current_user.uid), db=db
    )

    if _is_outputs_path(normalized_path) and output_revision_id:
        prefix = f"{normalized_path.rstrip('/')}/"
        remaining = [
            item
            for item in output_files
            if item.get("path") != normalized_path and not str(item.get("path") or "").startswith(prefix)
        ]
        if len(remaining) == len(output_files):
            raise HTTPException(status_code=404, detail="文件不存在")
        await publish_output_manifest(
            db=db,
            conversation=conversation,
            base_revision_id=output_revision_id,
            files=remaining,
        )
        return {"success": True, "path": normalized_path}

    try:
        if _is_workspace_path(normalized_path):
            await delete_workspace_path(path=_workspace_relative_path(normalized_path), current_user=current_user)
            return {"success": True, "path": normalized_path}
        actual_path = _resolve_local_user_data_path(thread_id, str(current_user.uid), normalized_path)
        if not actual_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        if actual_path.is_dir():
            await asyncio.to_thread(shutil.rmtree, actual_path)
        else:
            await asyncio.to_thread(actual_path.unlink)
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    return {"success": True, "path": normalized_path}


async def create_viewer_directory(
    *,
    thread_id: str,
    parent_path: str,
    name: str,
    current_user: User,
    db: AsyncSession,
) -> dict:
    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    normalized_parent = _normalize_path(parent_path)
    if not _is_workspace_path(normalized_parent):
        raise HTTPException(status_code=400, detail="当前路径不支持写入")

    response = await create_workspace_directory_entry(
        parent_path=_workspace_relative_path(normalized_parent),
        name=name,
        current_user=current_user,
    )
    return _viewer_response_from_workspace_response(response)


async def upload_viewer_files(
    *,
    thread_id: str,
    parent_path: str,
    files: list[UploadFile],
    current_user: User,
    db: AsyncSession,
) -> dict:
    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    normalized_parent = _normalize_path(parent_path)
    if not _is_workspace_path(normalized_parent):
        raise HTTPException(status_code=400, detail="当前路径不支持写入")

    response = await upload_workspace_files_entry(
        parent_path=_workspace_relative_path(normalized_parent),
        files=files,
        current_user=current_user,
    )
    return _viewer_response_from_workspace_response(response)
