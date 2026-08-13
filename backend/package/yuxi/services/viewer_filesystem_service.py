from __future__ import annotations

import asyncio
import io
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.backends import create_agent_composite_backend
from yuxi.agents.backends.sandbox import (
    SKILLS_PATH,
)
from yuxi.agents.backends.sandbox.synchronizer import sandbox_file_operation_lock
from yuxi.agents.backends.skills_backend import SelectedSkillsReadonlyBackend
from yuxi.agents.skills.service import normalize_string_list
from yuxi.services.agent_runtime_service import resolve_thread_agent_runtime_context
from yuxi.services.file_preview import (
    MAX_BINARY_PREVIEW_SIZE_BYTES,
    detect_media_type,
    is_binary_preview_type,
    render_preview_payload,
    render_preview_too_large_payload,
)
from yuxi.services.workspace_service import (
    create_workspace_directory as create_workspace_directory_entry,
)
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
from yuxi.storage.filestore import FileStoreError, get_file_store
from yuxi.utils.datetime_utils import utc_isoformat
from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS, VIRTUAL_PATH_PREFIX, VIRTUAL_PATH_UPLOADS, VIRTUAL_PATH_WORKSPACE
from yuxi.utils.upload_utils import MAX_UPLOAD_SIZE_BYTES, read_upload_with_limit

_PROTECTED_USER_DATA_ROOTS = frozenset(
    {
        VIRTUAL_PATH_PREFIX,
        VIRTUAL_PATH_WORKSPACE,
        VIRTUAL_PATH_UPLOADS,
        VIRTUAL_PATH_OUTPUTS,
    }
)


def _normalize_path(path: str | None) -> str:
    normalized = (path or "/").strip() or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/") if normalized not in {"/", SKILLS_PATH, VIRTUAL_PATH_PREFIX} else normalized


def _is_workspace_path(path: str) -> bool:
    return path == VIRTUAL_PATH_WORKSPACE or path.startswith(f"{VIRTUAL_PATH_WORKSPACE}/")


def _is_object_namespace_path(path: str) -> bool:
    return (
        path == VIRTUAL_PATH_UPLOADS
        or path == VIRTUAL_PATH_OUTPUTS
        or path.startswith((f"{VIRTUAL_PATH_UPLOADS}/", f"{VIRTUAL_PATH_OUTPUTS}/"))
    )


def _object_entry(path: str, *, name: str, is_dir: bool, size: int = 0, modified_at: str = "") -> dict:
    return {
        "path": f"{path}/" if is_dir and not path.endswith("/") else path,
        "name": name,
        "is_dir": is_dir,
        "size": size,
        "modified_at": modified_at,
    }


def _validate_object_child_name(name: str, field_name: str) -> str:
    clean_name = str(name or "").strip()
    if not clean_name or clean_name in {".", ".."} or "/" in clean_name or "\\" in clean_name:
        raise HTTPException(status_code=422, detail=f"{field_name} 不能包含路径分隔符")
    return clean_name


async def _require_object_directory(path) -> None:
    """确认 FileStore 虚拟目录存在，命名空间根目录始终存在。"""
    if not path.relative_path:
        return
    objects = await get_file_store().list(f"{path.key}/")
    if not objects:
        raise HTTPException(status_code=404, detail="目标目录不存在")


def _is_skills_path(path: str) -> bool:
    return path == SKILLS_PATH or path.startswith(f"{SKILLS_PATH}/")


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


def _render_viewer_preview(path: str, raw_content: bytes) -> dict | StreamingResponse:
    if len(raw_content) > MAX_BINARY_PREVIEW_SIZE_BYTES:
        return _preview_too_large_payload()
    payload = render_preview_payload(path, raw_content)
    if is_binary_preview_type(payload["preview_type"]) and payload["supported"]:
        return _preview_binary_response(path, raw_content, payload["preview_type"])
    return payload


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


async def list_viewer_filesystem_tree(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> dict:
    from yuxi.services.thread_files_service import list_thread_object_entries

    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    normalized_path = _normalize_path(path)
    sandbox_backend, skills_backend, selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    if normalized_path == "/":
        # 根目录只显示 viewer 暴露的虚拟命名空间，避免为只读树视图触发 sandbox 冷启动。
        entries = []

        entries.append(
            {"path": f"{VIRTUAL_PATH_PREFIX}/", "name": "user-data", "is_dir": True, "size": 0, "modified_at": ""}
        )
        if selected_skills:
            entries.append({"path": f"{SKILLS_PATH}/", "name": "skills", "is_dir": True, "size": 0, "modified_at": ""})

        return {"entries": _sort_entries(entries)}

    try:
        if normalized_path == VIRTUAL_PATH_PREFIX:
            entries = [
                {
                    "path": f"{VIRTUAL_PATH_WORKSPACE}/",
                    "name": "workspace",
                    "is_dir": True,
                    "size": 0,
                    "modified_at": "",
                },
                {
                    "path": f"{VIRTUAL_PATH_UPLOADS}/",
                    "name": "uploads",
                    "is_dir": True,
                    "size": 0,
                    "modified_at": "",
                },
                {
                    "path": f"{VIRTUAL_PATH_OUTPUTS}/",
                    "name": "outputs",
                    "is_dir": True,
                    "size": 0,
                    "modified_at": "",
                },
            ]
            return {"entries": _sort_entries(entries)}
        if _is_workspace_path(normalized_path) or _is_object_namespace_path(normalized_path):
            if _is_workspace_path(normalized_path):
                response = await list_workspace_tree(
                    path=_workspace_relative_path(normalized_path),
                    current_user=current_user,
                )
                entries = [_viewer_entry_from_workspace_entry(entry) for entry in response.get("entries", [])]
                return {"entries": _sort_entries(entries)}
            if _is_object_namespace_path(normalized_path):
                entries = await list_thread_object_entries(thread_id, normalized_path)
                return {"entries": _sort_entries(entries)}

        if _is_skills_path(normalized_path):
            result = await asyncio.to_thread(skills_backend.ls, _strip_skills_prefix(normalized_path))
            if result.error:
                raise HTTPException(status_code=400, detail=result.error)
            remapped = [_remap_prefixed_entry(entry, SKILLS_PATH) for entry in (result.entries or [])]
            return {"entries": _sort_entries(remapped)}
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    raise HTTPException(status_code=400, detail=f"Access denied: '{normalized_path}' is outside viewer namespace")


async def read_viewer_file_content(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> dict | StreamingResponse:
    from yuxi.services.thread_files_service import resolve_thread_object_path

    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")
    normalized_path = _normalize_path(path)

    sandbox_backend, skills_backend, _selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    try:
        if _is_workspace_path(normalized_path) or _is_object_namespace_path(normalized_path):
            if _is_workspace_path(normalized_path):
                return await read_workspace_file_content_response(
                    path=_workspace_relative_path(normalized_path),
                    current_user=current_user,
                )
            if _is_object_namespace_path(normalized_path):
                resolved = resolve_thread_object_path(thread_id, normalized_path)
                try:
                    raw_content = (await get_file_store().read(resolved.key)).data
                except FileStoreError as exc:
                    raise HTTPException(status_code=404, detail="文件不存在") from exc
            if len(raw_content) > MAX_BINARY_PREVIEW_SIZE_BYTES:
                return _preview_too_large_payload()
            return _render_viewer_preview(normalized_path, raw_content)
        elif _is_skills_path(normalized_path):
            responses = await asyncio.to_thread(skills_backend.download_files, [_strip_skills_prefix(normalized_path)])
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
    return _render_viewer_preview(normalized_path, raw_content)


async def download_viewer_file(
    *,
    thread_id: str,
    path: str,
    current_user: User,
    db: AsyncSession,
) -> StreamingResponse | FileResponse:
    from yuxi.services.thread_files_service import resolve_thread_object_path

    normalized_path = _normalize_path(path)
    sandbox_backend, skills_backend, _selected_skills = await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    try:
        if _is_workspace_path(normalized_path) or _is_object_namespace_path(normalized_path):
            if _is_workspace_path(normalized_path):
                return await download_workspace_file_response(
                    path=_workspace_relative_path(normalized_path),
                    current_user=current_user,
                )
            if _is_object_namespace_path(normalized_path):
                resolved = resolve_thread_object_path(thread_id, normalized_path)
                store = get_file_store()
                try:
                    await store.stat(resolved.key)
                except FileStoreError as exc:
                    raise HTTPException(status_code=404, detail="文件不存在") from exc
                file_name = PurePosixPath(resolved.relative_path).name or "download"
                stream = store.stream(resolved.key)
            media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            headers = {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
            }
            return StreamingResponse(stream, media_type=media_type, headers=headers)

        if _is_skills_path(normalized_path):
            responses = await asyncio.to_thread(skills_backend.download_files, [_strip_skills_prefix(normalized_path)])
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
    from yuxi.services.thread_files_service import resolve_thread_object_path

    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    normalized_path = _normalize_path(path)
    await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    if normalized_path == VIRTUAL_PATH_PREFIX:
        raise HTTPException(status_code=400, detail="当前目录不允许删除")
    if not (_is_workspace_path(normalized_path) or _is_object_namespace_path(normalized_path)):
        raise HTTPException(status_code=400, detail="当前路径不支持删除")
    if normalized_path in _PROTECTED_USER_DATA_ROOTS:
        raise HTTPException(status_code=400, detail="当前目录不允许删除")

    try:
        if _is_workspace_path(normalized_path):
            await delete_workspace_path(path=_workspace_relative_path(normalized_path), current_user=current_user)
            return {"success": True, "path": normalized_path}
        if _is_object_namespace_path(normalized_path):
            async with sandbox_file_operation_lock(file_thread_id=thread_id):
                resolved = resolve_thread_object_path(thread_id, normalized_path, allow_root=True)
                if not resolved.relative_path:
                    raise HTTPException(status_code=400, detail="当前目录不允许删除")
                store = get_file_store()
                descendants = await store.list(f"{resolved.key}/")
                if descendants:
                    await store.delete_prefix(f"{resolved.key}/")
                else:
                    try:
                        await store.stat(resolved.key)
                    except FileStoreError:
                        raise HTTPException(status_code=404, detail="文件不存在")
                    await store.delete(resolved.key)
            return {"success": True, "path": normalized_path}
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
    from yuxi.services.thread_files_service import resolve_thread_object_path

    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    normalized_parent = _normalize_path(parent_path)
    if _is_object_namespace_path(normalized_parent):
        async with sandbox_file_operation_lock(file_thread_id=thread_id):
            resolved = resolve_thread_object_path(thread_id, normalized_parent, allow_root=True)
            await _require_object_directory(resolved)
            directory_name = _validate_object_child_name(name, "文件夹名")
            directory_key = f"{resolved.key}/{directory_name}"
            marker_key = f"{resolved.key}/{directory_name}/.keep"
            store = get_file_store()
            try:
                await store.stat(directory_key)
                raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")
            except FileStoreError:
                if await store.list(f"{directory_key}/"):
                    raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")
                await store.put(marker_key, b"", content_type="application/octet-stream")
        return {
            "success": True,
            "entry": _object_entry(
                f"{normalized_parent.rstrip('/')}/{directory_name}", name=directory_name, is_dir=True
            ),
        }
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
    from yuxi.services.thread_files_service import resolve_thread_object_path

    if not thread_id:
        raise HTTPException(status_code=422, detail="thread_id 不能为空")

    await _resolve_viewer_state(
        thread_id=thread_id,
        current_user=current_user,
        db=db,
    )

    normalized_parent = _normalize_path(parent_path)
    if _is_object_namespace_path(normalized_parent):
        if not files:
            raise HTTPException(status_code=400, detail="请选择至少一个文件")
        if len(files) > 50:
            raise HTTPException(status_code=400, detail="一次最多上传 50 个文件")
        async with sandbox_file_operation_lock(file_thread_id=thread_id):
            resolved = resolve_thread_object_path(thread_id, normalized_parent, allow_root=True)
            await _require_object_directory(resolved)
            entries = []
            seen_names: set[str] = set()
            for file in files:
                file_name = _validate_object_child_name(Path(file.filename or "").name, "文件名")
                if file_name in seen_names:
                    raise HTTPException(status_code=400, detail=f"选择的文件中存在重复文件名: {file_name}")
                seen_names.add(file_name)
                key = f"{resolved.key}/{file_name}"
                try:
                    await get_file_store().stat(key)
                except FileStoreError:
                    if await get_file_store().list(f"{key}/"):
                        raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")
                    try:
                        content = await read_upload_with_limit(
                            file,
                            max_size_bytes=MAX_UPLOAD_SIZE_BYTES,
                            too_large_message="文件过大，当前仅支持 100 MB 以内的文件",
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    stat = await get_file_store().put(key, content, content_type=file.content_type)
                    entries.append(
                        _object_entry(
                            f"{normalized_parent.rstrip('/')}/{file_name}",
                            name=file_name,
                            is_dir=False,
                            size=stat.size,
                            modified_at=utc_isoformat(stat.modified),
                        )
                    )
                    continue
                raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")
        return {"success": True, "entries": entries}
    if not _is_workspace_path(normalized_parent):
        raise HTTPException(status_code=400, detail="当前路径不支持写入")

    response = await upload_workspace_files_entry(
        parent_path=_workspace_relative_path(normalized_parent),
        files=files,
        current_user=current_user,
    )
    return _viewer_response_from_workspace_response(response)
