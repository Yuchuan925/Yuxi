from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from fastapi import HTTPException
from yuxi.agents.backends.sandbox.synchronizer import sandbox_file_operation_lock
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.conversation_service import require_user_conversation
from yuxi.services.file_preview import detect_media_type
from yuxi.services.mention_search_service import invalidate_mention_cache, invalidate_workspace_mention_cache
from yuxi.services.workspace_service import (
    ensure_workspace_directory,
    list_workspace_tree,
    next_available_workspace_path,
    put_workspace_file,
    read_workspace_file_object,
)
from yuxi.storage.filestore import FileStoreError, ObjectStat, get_file_store, thread_output_key, thread_upload_key
from yuxi.utils.datetime_utils import utc_isoformat
from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS, VIRTUAL_PATH_PREFIX, VIRTUAL_PATH_UPLOADS, VIRTUAL_PATH_WORKSPACE

_KEEP_MARKER = ".keep"


@dataclass(slots=True)
class ArtifactObject:
    """描述可由 HTTP 层流式返回的线程交付物。"""

    name: str
    media_type: str
    size: int
    stream: AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class ThreadObjectPath:
    """表示 uploads/outputs 虚拟路径对应的 FileStore key。"""

    namespace: str
    relative_path: str
    virtual_path: str
    key: str


def _get_virtual_root() -> str:
    """返回 thread files API 暴露的虚拟根目录。"""
    return "/" + VIRTUAL_PATH_PREFIX.strip("/")


def resolve_thread_object_path(thread_id: str, path: str, *, allow_root: bool = False) -> ThreadObjectPath:
    """严格解析 uploads/outputs 虚拟路径并生成逻辑 key。"""
    raw_path = str(path or "").strip()
    if "\\" in raw_path:
        raise HTTPException(status_code=403, detail="Access denied")
    normalized = "/" + raw_path.lstrip("/")

    for namespace, virtual_root, key_builder in (
        ("uploads", VIRTUAL_PATH_UPLOADS, thread_upload_key),
        ("outputs", VIRTUAL_PATH_OUTPUTS, thread_output_key),
    ):
        if normalized != virtual_root and not normalized.startswith(f"{virtual_root}/"):
            continue

        relative_path = normalized[len(virtual_root) :].lstrip("/")
        if not relative_path:
            if not allow_root:
                raise HTTPException(status_code=400, detail="path must be a file")
            return ThreadObjectPath(namespace, "", virtual_root, f"threads/{thread_id}/{namespace}")

        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise HTTPException(status_code=403, detail="Access denied")
        clean_relative = PurePosixPath(*parts).as_posix()
        return ThreadObjectPath(
            namespace, clean_relative, f"{virtual_root}/{clean_relative}", key_builder(thread_id, clean_relative)
        )

    raise HTTPException(status_code=400, detail="path must be inside uploads or outputs")


async def list_thread_object_entries(thread_id: str, path: str, *, recursive: bool = False) -> list[dict[str, Any]]:
    """从 FileStore 列出指定 uploads/outputs 目录并合成虚拟目录。"""
    resolved = resolve_thread_object_path(thread_id, path, allow_root=True)
    prefix = f"{resolved.key}/"
    objects = await get_file_store().list(prefix)
    entries: dict[str, dict[str, Any]] = {}

    for item in objects:
        relative = item.key[len(prefix) :]
        if not relative or relative == _KEEP_MARKER:
            continue
        parts = relative.split("/")
        if not recursive and len(parts) > 1:
            child_name = parts[0]
            child_path = f"{resolved.virtual_path}/{child_name}"
            entries.setdefault(child_path, _object_directory_entry(child_path, child_name, item))
            continue

        if recursive:
            for index in range(1, len(parts)):
                directory_relative = "/".join(parts[:index])
                directory_path = f"{resolved.virtual_path}/{directory_relative}"
                entries.setdefault(directory_path, _object_directory_entry(directory_path, parts[index - 1], item))

        if parts[-1] == _KEEP_MARKER:
            marker_directory = "/".join(parts[:-1])
            if marker_directory:
                marker_path = f"{resolved.virtual_path}/{marker_directory}"
                entries.setdefault(marker_path, _object_directory_entry(marker_path, parts[-2], item))
            continue

        file_path = f"{resolved.virtual_path}/{relative}"
        entries[file_path] = {
            "path": file_path,
            "name": parts[-1],
            "is_dir": False,
            "size": item.size,
            "modified_at": utc_isoformat(item.modified),
            "artifact_url": f"/api/chat/thread/{thread_id}/artifacts/{file_path.lstrip('/')}",
        }

    return sorted(entries.values(), key=lambda entry: (not entry["is_dir"], entry["name"].lower()))


def _object_directory_entry(path: str, name: str, item: ObjectStat) -> dict[str, Any]:
    return {
        "path": f"{path}/",
        "name": name,
        "is_dir": True,
        "size": 0,
        "modified_at": utc_isoformat(item.modified),
        "artifact_url": None,
    }


def _thread_workspace_entry(thread_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    virtual_path = str(entry.get("virtual_path") or VIRTUAL_PATH_WORKSPACE)
    is_dir = bool(entry.get("is_dir"))
    if is_dir and not virtual_path.endswith("/"):
        virtual_path = f"{virtual_path}/"
    return {
        "path": virtual_path,
        "name": entry.get("name") or "workspace",
        "is_dir": is_dir,
        "size": int(entry.get("size") or 0),
        "modified_at": str(entry.get("modified_at") or ""),
        "artifact_url": None
        if is_dir
        else f"/api/chat/thread/{thread_id}/artifacts/{virtual_path.lstrip('/')}",
    }


async def list_thread_files_view(
    *, thread_id: str, current_uid: str, db, path: str | None = None, recursive: bool = False
) -> dict:
    conv_repo = ConversationRepository(db)
    conversation = await require_user_conversation(conv_repo, thread_id, str(current_uid))
    virtual_path = path or _get_virtual_root()

    if virtual_path.rstrip("/") in {VIRTUAL_PATH_UPLOADS, VIRTUAL_PATH_OUTPUTS} or virtual_path.startswith(
        (f"{VIRTUAL_PATH_UPLOADS}/", f"{VIRTUAL_PATH_OUTPUTS}/")
    ):
        entries = await list_thread_object_entries(thread_id, virtual_path, recursive=recursive)
        return {"path": virtual_path, "files": entries}

    if virtual_path.rstrip("/") == _get_virtual_root():
        entries = [
            {
                "path": f"{VIRTUAL_PATH_UPLOADS}/",
                "name": "uploads",
                "is_dir": True,
                "size": 0,
                "modified_at": "",
                "artifact_url": None,
            },
            {
                "path": f"{VIRTUAL_PATH_OUTPUTS}/",
                "name": "outputs",
                "is_dir": True,
                "size": 0,
                "modified_at": "",
                "artifact_url": None,
            },
        ]
        entries.append(
            {
                "path": f"{VIRTUAL_PATH_WORKSPACE}/",
                "name": "workspace",
                "is_dir": True,
                "size": 0,
                "modified_at": "",
                "artifact_url": None,
            }
        )
        if recursive:
            workspace = await list_workspace_tree(
                path="/", recursive=True, current_user=conversation, thread_titles=None
            )
            entries.extend(_thread_workspace_entry(thread_id, entry) for entry in workspace["entries"])
            entries.extend(await list_thread_object_entries(thread_id, VIRTUAL_PATH_UPLOADS, recursive=True))
            entries.extend(await list_thread_object_entries(thread_id, VIRTUAL_PATH_OUTPUTS, recursive=True))
        return {"path": virtual_path, "files": entries}

    if virtual_path == VIRTUAL_PATH_WORKSPACE or virtual_path.startswith(f"{VIRTUAL_PATH_WORKSPACE}/"):
        relative = virtual_path[len(VIRTUAL_PATH_WORKSPACE) :] or "/"
        workspace = await list_workspace_tree(
            path=relative, recursive=recursive, current_user=conversation, thread_titles=None
        )
        return {
            "path": virtual_path,
            "files": [_thread_workspace_entry(thread_id, entry) for entry in workspace["entries"]],
        }
    raise HTTPException(status_code=400, detail="path must be inside workspace, uploads or outputs")


async def read_thread_file_content_view(
    *, thread_id: str, current_uid: str, db, path: str, offset: int = 0, limit: int = 2000
) -> dict:
    conv_repo = ConversationRepository(db)
    conversation = await require_user_conversation(conv_repo, thread_id, str(current_uid))
    normalized = "/" + path.lstrip("/")

    if normalized.startswith((f"{VIRTUAL_PATH_UPLOADS}/", f"{VIRTUAL_PATH_OUTPUTS}/")):
        resolved = resolve_thread_object_path(thread_id, normalized)
        try:
            text = (await get_file_store().read(resolved.key)).data.decode("utf-8", errors="replace")
        except FileStoreError as exc:
            raise HTTPException(status_code=404, detail="file not found") from exc
    else:
        if normalized != VIRTUAL_PATH_WORKSPACE and not normalized.startswith(f"{VIRTUAL_PATH_WORKSPACE}/"):
            raise HTTPException(status_code=400, detail="path must be inside workspace, uploads or outputs")
        relative = normalized[len(VIRTUAL_PATH_WORKSPACE) :] or "/"
        stored = await read_workspace_file_object(path=relative, current_user=conversation)
        text = stored.data.decode("utf-8", errors="replace")

    lines = text.splitlines()
    start = max(0, int(offset))
    count = min(max(1, int(limit)), 5000)
    return {
        "path": path,
        "content": lines[start : start + count],
        "offset": start,
        "limit": count,
        "total_lines": len(lines),
        "artifact_url": f"/api/chat/thread/{thread_id}/artifacts/{path.lstrip('/')}",
    }


async def resolve_thread_artifact_view(*, thread_id: str, current_uid: str, db, path: str) -> ArtifactObject:
    conv_repo = ConversationRepository(db)
    conversation = await require_user_conversation(conv_repo, thread_id, str(current_uid))
    normalized = "/" + path.lstrip("/")

    if normalized.startswith((f"{VIRTUAL_PATH_UPLOADS}/", f"{VIRTUAL_PATH_OUTPUTS}/")):
        resolved = resolve_thread_object_path(thread_id, normalized)
        store = get_file_store()
        try:
            stat = await store.stat(resolved.key)
            stream = store.stream(resolved.key)
            first_chunk = await anext(stream, b"")
        except FileStoreError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        return ArtifactObject(
            name=PurePosixPath(resolved.relative_path).name,
            media_type=detect_media_type(PurePosixPath(resolved.relative_path).name, first_chunk[:512]),
            size=stat.size,
            stream=_prepend_chunk(first_chunk, stream),
        )

    if normalized != VIRTUAL_PATH_WORKSPACE and not normalized.startswith(f"{VIRTUAL_PATH_WORKSPACE}/"):
        raise HTTPException(status_code=403, detail="access denied")
    relative = normalized[len(VIRTUAL_PATH_WORKSPACE) :] or "/"
    stored = await read_workspace_file_object(path=relative, current_user=conversation)
    return ArtifactObject(
        name=PurePosixPath(relative).name,
        media_type=detect_media_type(relative, stored.data[:512]),
        size=stored.size,
        stream=_stream_bytes(stored.data),
    )


async def _prepend_chunk(first_chunk: bytes, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if first_chunk:
        yield first_chunk
    async for chunk in stream:
        yield chunk


async def _stream_bytes(content: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(content), 64 * 1024):
        yield content[offset : offset + 64 * 1024]


async def save_thread_artifact_to_workspace_view(*, thread_id: str, current_uid: str, db, path: str) -> dict[str, str]:
    conv_repo = ConversationRepository(db)
    conversation = await require_user_conversation(conv_repo, thread_id, str(current_uid))
    uid = str(conversation.uid)
    async with sandbox_file_operation_lock(uid=uid, file_thread_id=thread_id):
        artifact = await resolve_thread_artifact_view(thread_id=thread_id, current_uid=current_uid, db=db, path=path)
        content = b"".join([chunk async for chunk in artifact.stream])
        await ensure_workspace_directory(uid=uid, path="/saved_artifacts")
        saved_path = await next_available_workspace_path(
            uid=uid, parent_path="/saved_artifacts", filename=artifact.name
        )
        entry = await put_workspace_file(
            uid=uid, path=saved_path, content=content, content_type=artifact.media_type
        )

    await invalidate_mention_cache(thread_id)
    await invalidate_workspace_mention_cache(uid)
    saved_virtual_path = str(entry["virtual_path"])
    return {
        "name": str(entry["name"]),
        "source_path": "/" + path.lstrip("/"),
        "saved_path": saved_virtual_path,
        "saved_artifact_url": f"/api/chat/thread/{thread_id}/artifacts/{saved_virtual_path.lstrip('/')}",
    }
