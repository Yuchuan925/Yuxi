"""线程文件 API 的实时 Project Workdir 适配。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from yuxi.agents.backends.sandbox.backend import FileTransferLimitError
from yuxi.agents.skills.service import list_accessible_skills
from yuxi.repositories.user_repository import UserRepository
from yuxi.services.file_preview import detect_media_type
from yuxi.services.mention_search_service import invalidate_mention_cache, invalidate_workspace_mention_cache
from yuxi.services.project_workdir_service import resolve_project_workdir_binding
from yuxi.utils.paths import VIRTUAL_SKILLS_PATH

MAX_THREAD_FILE_READ_BYTES = 10 * 1024 * 1024
MAX_ARTIFACT_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_RECURSIVE_ENTRIES = 5000
MAX_RECURSIVE_DIRECTORIES = 600
MAX_SAVED_ARTIFACT_NAME_ATTEMPTS = 1000


def _thread_file_entry(thread_id: str, directory: str, item: dict) -> dict:
    child_path = f"{directory.rstrip('/')}/{item['name']}"
    is_dir = bool(item.get("is_dir"))
    return {
        "path": f"{child_path}/" if is_dir else child_path,
        "name": item["name"],
        "is_dir": is_dir,
        "size": int(item.get("size") or 0),
        "modified_at": "",
        "artifact_url": None if is_dir else f"/api/chat/thread/{thread_id}/artifacts/{child_path.lstrip('/')}",
    }


def _normalize_project_path(workdir_path: str, path: str | None) -> str:
    raw = str(path or "/").strip() or "/"
    if raw == "/":
        return workdir_path
    normalized = str(PurePosixPath(raw if raw.startswith("/") else f"/{raw}"))
    if ".." in PurePosixPath(raw).parts:
        raise HTTPException(status_code=403, detail="access denied")
    if normalized != workdir_path and not normalized.startswith(f"{workdir_path}/"):
        raise HTTPException(status_code=403, detail="thread files only expose the Project Workdir")
    return normalized


def _normalize_artifact_path(workdir_path: str, path: str) -> str:
    raw = str(path or "").strip()
    normalized = str(PurePosixPath(raw if raw.startswith("/") else f"/{raw}"))
    if ".." in PurePosixPath(raw).parts:
        raise HTTPException(status_code=403, detail="access denied")
    allowed = normalized.startswith(f"{workdir_path}/") or normalized.startswith("/home/gem/user-data/")
    allowed = allowed or normalized.startswith(f"{VIRTUAL_SKILLS_PATH}/")
    if not allowed:
        raise HTTPException(status_code=403, detail="artifact is outside the current user's visible roots")
    return normalized


async def _require_skill_artifact_access(*, normalized_path: str, current_uid: str, db) -> None:
    skills_prefix = f"{VIRTUAL_SKILLS_PATH}/"
    if not normalized_path.startswith(skills_prefix):
        return
    slug = normalized_path[len(skills_prefix) :].split("/", 1)[0]
    user = await UserRepository(db).get_by_uid(str(current_uid))
    if user is None or bool(user.is_deleted):
        raise HTTPException(status_code=403, detail="artifact access denied")
    accessible_slugs = {skill.slug for skill in await list_accessible_skills(db, user)}
    if slug not in accessible_slugs:
        raise HTTPException(status_code=403, detail="artifact access denied")


async def _binding_backend(*, thread_id: str, current_uid: str, db):
    binding = await resolve_project_workdir_binding(thread_id=thread_id, uid=current_uid, db=db)
    backend = binding.create_file_backend(create_if_missing=True)
    await asyncio.to_thread(backend.ensure_available)
    return binding, backend


async def list_thread_files_view(
    *,
    thread_id: str,
    current_uid: str,
    db,
    path: str | None = None,
    recursive: bool = False,
) -> dict:
    """列出实时 Project Workdir，兼容 thread-files 响应结构。"""
    binding, backend = await _binding_backend(thread_id=thread_id, current_uid=current_uid, db=db)
    normalized = _normalize_project_path(binding.workdir_path, path)
    if not recursive:
        try:
            items = await asyncio.to_thread(
                backend.list_authorized_directory,
                normalized,
                root=binding.workdir_path,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="path not found") from exc
        files = [_thread_file_entry(thread_id, normalized, item) for item in items]
        return {"path": normalized, "files": files}

    files: list[dict] = []
    pending = [normalized]
    visited_directories = 0
    while pending and len(files) < MAX_RECURSIVE_ENTRIES:
        directory = pending.pop(0)
        visited_directories += 1
        if visited_directories > MAX_RECURSIVE_DIRECTORIES:
            break
        try:
            items = await asyncio.to_thread(backend.list_authorized_directory, directory, root=binding.workdir_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="path not found") from exc
        for item in items:
            child_path = f"{directory.rstrip('/')}/{item['name']}"
            entry = _thread_file_entry(thread_id, directory, item)
            files.append(entry)
            if len(files) >= MAX_RECURSIVE_ENTRIES:
                break
            if item.get("is_dir"):
                pending.append(child_path)
    return {"path": normalized, "files": files}


async def read_thread_file_content_view(
    *,
    thread_id: str,
    current_uid: str,
    db,
    path: str,
    offset: int = 0,
    limit: int = 2000,
) -> dict:
    """从实时 Workdir 读取文本行。"""
    binding, backend = await _binding_backend(thread_id=thread_id, current_uid=current_uid, db=db)
    normalized = _normalize_project_path(binding.workdir_path, path)
    descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-thread-file-")
    os.close(descriptor)
    try:
        await asyncio.to_thread(
            backend.download_authorized_file_to_path,
            normalized,
            temp_path,
            MAX_THREAD_FILE_READ_BYTES,
        )
        text = await asyncio.to_thread(Path(temp_path).read_text, encoding="utf-8", errors="replace")
    except FileTransferLimitError as exc:
        raise HTTPException(status_code=413, detail="file exceeds transfer limit") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
    lines = text.splitlines()
    start = max(0, int(offset))
    count = min(max(1, int(limit)), 5000)
    return {
        "path": normalized,
        "content": lines[start : start + count],
        "offset": start,
        "limit": count,
        "total_lines": len(lines),
        "artifact_url": f"/api/chat/thread/{thread_id}/artifacts/{normalized.lstrip('/')}",
    }


async def resolve_thread_artifact_view(
    *,
    thread_id: str,
    current_uid: str,
    db,
    path: str,
    download: bool = False,
) -> FileResponse:
    """把实时授权文件导出为自动清理的 HTTP 文件响应。"""
    binding, backend = await _binding_backend(thread_id=thread_id, current_uid=current_uid, db=db)
    normalized = _normalize_artifact_path(binding.workdir_path, path)
    await _require_skill_artifact_access(normalized_path=normalized, current_uid=current_uid, db=db)
    descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-artifact-", suffix=PurePosixPath(normalized).suffix)
    os.close(descriptor)
    try:
        await asyncio.to_thread(
            backend.download_authorized_file_to_path,
            normalized,
            temp_path,
            MAX_ARTIFACT_DOWNLOAD_BYTES,
        )
    except PermissionError as exc:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=403, detail="artifact access denied") from exc
    except IsADirectoryError as exc:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=400, detail="artifact path is not a regular file") from exc
    except FileTransferLimitError as exc:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=413, detail="artifact exceeds transfer limit") from exc
    except (FileNotFoundError, ValueError) as exc:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    file_name = PurePosixPath(normalized).name or "artifact"
    with open(temp_path, "rb") as artifact_file:
        media_type = detect_media_type(file_name, artifact_file.read(16 * 1024))
    return FileResponse(
        temp_path,
        media_type=media_type,
        filename=file_name if download else None,
        content_disposition_type="attachment",
        background=BackgroundTask(os.unlink, temp_path),
    )


async def save_thread_artifact_to_workspace_view(*, thread_id: str, current_uid: str, db, path: str) -> dict[str, str]:
    """把可见 artifact 复制到用户级 User Data saved_artifacts。"""
    binding, backend = await _binding_backend(thread_id=thread_id, current_uid=current_uid, db=db)
    normalized = _normalize_artifact_path(binding.workdir_path, path)
    await _require_skill_artifact_access(normalized_path=normalized, current_uid=current_uid, db=db)
    descriptor, temp_path = tempfile.mkstemp(prefix="yuxi-save-artifact-")
    os.close(descriptor)
    try:
        try:
            await asyncio.to_thread(
                backend.download_authorized_file_to_path,
                normalized,
                temp_path,
                MAX_ARTIFACT_DOWNLOAD_BYTES,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="artifact access denied") from exc
        except IsADirectoryError as exc:
            raise HTTPException(status_code=400, detail="artifact path is not a regular file") from exc
        except FileTransferLimitError as exc:
            raise HTTPException(status_code=413, detail="artifact exceeds transfer limit") from exc
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        file_name = PurePosixPath(normalized).name or "artifact"
        target = f"/home/gem/user-data/workspace/saved_artifacts/{file_name}"
        index = 1
        stem = PurePosixPath(file_name).stem
        suffix = PurePosixPath(file_name).suffix
        while await asyncio.to_thread(backend.regular_file_exists, target):
            if index > MAX_SAVED_ARTIFACT_NAME_ATTEMPTS:
                raise HTTPException(status_code=409, detail="saved artifact name space is exhausted")
            target = f"/home/gem/user-data/workspace/saved_artifacts/{stem} ({index}){suffix}"
            index += 1
        await asyncio.to_thread(backend.upload_authorized_file_from_path, target, temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
    await invalidate_mention_cache(binding.thread_id)
    await invalidate_workspace_mention_cache(current_uid)
    return {
        "name": PurePosixPath(target).name,
        "source_path": normalized,
        "saved_path": target,
        "saved_artifact_url": f"/api/chat/thread/{thread_id}/artifacts/{target.lstrip('/')}",
    }
