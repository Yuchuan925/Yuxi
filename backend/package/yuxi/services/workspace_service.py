from __future__ import annotations

import contextlib
import io
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.backends.sandbox.paths import validate_thread_id
from yuxi.agents.backends.sandbox.synchronizer import sandbox_file_operation_lock
from yuxi.repositories.conversation_repository import ConversationRepository
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
from yuxi.storage.filestore import (
    FileStoreError,
    ObjectStat,
    StoredObject,
    get_file_store,
    thread_output_key,
    thread_upload_key,
    user_workspace_key,
)
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import utc_isoformat
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import (
    CONVERSATION_HISTORY_DIR_NAME,
    LARGE_TOOL_RESULTS_DIR_NAME,
    OUTPUTS_DIR_NAME,
    UPLOADS_DIR_NAME,
    VIRTUAL_PATH_WORKSPACE,
    WORKSPACE_AGENT_CONTEXT_FILES,
    WORKSPACE_AGENTS_DIR_NAME,
)
from yuxi.utils.upload_utils import MAX_UPLOAD_SIZE_BYTES, read_upload_with_limit

EDITABLE_WORKSPACE_SUFFIXES = {".md", ".markdown", ".mdx", ".txt"}
MAX_WORKSPACE_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_BYTES
MAX_WORKSPACE_UPLOAD_FILES = 50
WORKSPACE_CHATS_DIR_NAME = "chats"
KEEP_MARKER = ".keep"
_CHAT_READONLY_MESSAGE = "历史对话文件为只读，请在对应对话中修改"
_CHAT_INTERMEDIATE_DIR_NAMES = frozenset(
    {
        LARGE_TOOL_RESULTS_DIR_NAME,
        "large-tool-results",
        "large_tool_history",
        CONVERSATION_HISTORY_DIR_NAME,
    }
)


async def list_workspace_tree(
    *,
    path: str,
    recursive: bool = False,
    files_only: bool = False,
    current_user: User,
    thread_titles: dict[str, str] | None = None,
) -> dict:
    """列出用户 workspace，并合成只读的历史对话虚拟目录。"""
    uid = str(current_user.uid)
    await ensure_workspace_defaults(uid)

    if workspace_path_uses_chat_mapping(path) and await _workspace_path_exists(uid, _physical_chats_path()):
        raise HTTPException(status_code=409, detail="工作区 agents/chats 已被现有文件或目录占用")
    if _chat_path_parts(path) is not None:
        entries = await _list_chat_directory(
            path,
            thread_titles=thread_titles or {},
            recursive=recursive,
            files_only=files_only,
        )
        return {"entries": entries, "readonly": True}

    relative = _workspace_relative_path(path)
    kind = await _workspace_path_kind(uid, relative)
    if kind is None:
        return {"entries": []}
    if kind != "directory":
        raise HTTPException(status_code=400, detail="当前路径不是目录")

    entries = await _list_workspace_directory(uid, relative, recursive=recursive, files_only=files_only)
    normalized_path = _normalize_workspace_path(path).as_posix().rstrip("/")
    if thread_titles is not None and normalized_path == f"/{WORKSPACE_AGENTS_DIR_NAME}":
        entries = [entry for entry in entries if entry["name"] != WORKSPACE_CHATS_DIR_NAME]
        chats_path = f"/{WORKSPACE_AGENTS_DIR_NAME}/{WORKSPACE_CHATS_DIR_NAME}"
        if not files_only:
            entries.append(_virtual_entry(chats_path, name=WORKSPACE_CHATS_DIR_NAME, title="历史对话", is_dir=True))
        if recursive:
            entries.extend(
                await _list_chat_directory(
                    chats_path,
                    thread_titles=thread_titles,
                    recursive=True,
                    files_only=files_only,
                )
            )
        entries = _sort_entries(entries)
    return {"entries": entries}


async def read_workspace_file_object(
    *, path: str, current_user: User, thread_titles: dict[str, str] | None = None
) -> StoredObject:
    """读取 workspace 或历史对话映射中的文件对象。"""
    uid = str(current_user.uid)
    await ensure_workspace_defaults(uid)
    key, _display_path = await _resolve_readable_file_key(uid, path, thread_titles)
    try:
        return await get_file_store().read(key)
    except FileStoreError as exc:
        raise HTTPException(status_code=404, detail=f"工作区文件不存在: {path}") from exc


async def read_workspace_file_content(
    *, path: str, current_user: User, thread_titles: dict[str, str] | None = None
) -> dict | StreamingResponse:
    """读取并渲染 workspace 文件预览。"""
    stored = await read_workspace_file_object(path=path, current_user=current_user, thread_titles=thread_titles)
    if stored.size > MAX_BINARY_PREVIEW_SIZE_BYTES:
        return render_preview_too_large_payload()

    filename = PurePosixPath(_normalize_workspace_path(path).as_posix()).name or "preview"
    if is_office_pdf_preview_file(path):
        try:
            pdf_content = await convert_office_to_pdf(filename, stored.data)
        except OfficePreviewConversionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _preview_binary_response(
            filename=f"{PurePosixPath(filename).stem or 'preview'}.pdf",
            content=pdf_content,
            media_type="application/pdf",
            preview_type="pdf",
        )

    preview_type, supported, message = detect_preview_type(path, stored.data)
    if is_binary_preview_type(preview_type) and supported:
        return _preview_binary_response(
            filename=filename,
            content=stored.data,
            media_type=detect_media_type(path, stored.data),
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
    return render_preview_payload(path, stored.data)


async def write_workspace_file_content(*, path: str, content: str, current_user: User) -> dict:
    """更新 workspace 中已有的 UTF-8 文本文件。"""
    _reject_chat_write(path)
    uid = str(current_user.uid)
    async with sandbox_file_operation_lock(uid=uid):
        relative = _workspace_relative_path(path)
        kind = await _workspace_path_kind(uid, relative)
        if kind is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        if kind != "file":
            raise HTTPException(status_code=400, detail="当前路径是目录")
        if PurePosixPath(relative).suffix.lower() not in EDITABLE_WORKSPACE_SUFFIXES:
            raise HTTPException(status_code=400, detail="当前文件类型不支持编辑")

        stored = await read_workspace_file_object(path=path, current_user=current_user)
        preview_type, supported, _message = detect_preview_type(path, stored.data)
        if preview_type not in {"markdown", "text"} or not supported:
            raise HTTPException(status_code=400, detail="当前文件类型不支持编辑")
        try:
            stored.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="当前文件不是 UTF-8 文本") from exc

        stat = await get_file_store().put(
            user_workspace_key(uid, relative), content.encode("utf-8"), content_type=stored.content_type
        )
    return {"success": True, "path": _display_path(relative), "entry": _workspace_file_entry(relative, stat)}


async def delete_workspace_path(*, path: str, current_user: User) -> dict:
    """删除 workspace 文件或目录前缀。"""
    _reject_chat_write(path)
    uid = str(current_user.uid)
    async with sandbox_file_operation_lock(uid=uid):
        relative = _workspace_relative_path(path)
        if not relative:
            raise HTTPException(status_code=400, detail="工作区根目录不允许删除")
        kind = await _workspace_path_kind(uid, relative)
        if kind is None:
            raise HTTPException(status_code=404, detail="文件不存在")

        store = get_file_store()
        if kind == "directory":
            await store.delete_prefix(f"{user_workspace_key(uid, relative)}/")
        else:
            await store.delete(user_workspace_key(uid, relative))
    await invalidate_workspace_mention_cache(uid)
    return {"success": True, "path": _display_path(relative)}


async def create_workspace_directory(*, parent_path: str, name: str, current_user: User) -> dict:
    """使用隐藏 marker 创建 workspace 空目录。"""
    _reject_chat_write(parent_path)
    uid = str(current_user.uid)
    async with sandbox_file_operation_lock(uid=uid):
        parent = _workspace_relative_path(parent_path)
        await _require_workspace_directory(uid, parent)
        directory_name = _validate_child_name(name, field_name="文件夹名")
        relative = _join_relative(parent, directory_name)
        if await _workspace_path_kind(uid, relative) is not None:
            raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")

        stat = await get_file_store().put(
            user_workspace_key(uid, f"{relative}/{KEEP_MARKER}"), b"", content_type="application/octet-stream"
        )
    await invalidate_workspace_mention_cache(uid)
    return {"success": True, "entry": _workspace_directory_entry(relative, stat)}


async def upload_workspace_files(*, parent_path: str, files: list[UploadFile], current_user: User) -> dict:
    """批量上传 workspace 文件，失败时回滚本批次已写对象。"""
    if not files:
        raise HTTPException(status_code=400, detail="请选择至少一个文件")
    if len(files) > MAX_WORKSPACE_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_WORKSPACE_UPLOAD_FILES} 个文件")
    _reject_chat_write(parent_path)

    uid = str(current_user.uid)
    async with sandbox_file_operation_lock(uid=uid):
        parent = _workspace_relative_path(parent_path)
        await _require_workspace_directory(uid, parent)
        seen_names: set[str] = set()
        targets: list[tuple[UploadFile, str]] = []
        for file in files:
            filename = _validate_child_name(PurePosixPath(file.filename or "").name, field_name="文件名")
            if filename in seen_names:
                raise HTTPException(status_code=400, detail=f"选择的文件中存在重复文件名: {filename}")
            seen_names.add(filename)
            relative = _join_relative(parent, filename)
            if await _workspace_path_kind(uid, relative) is not None:
                raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")
            targets.append((file, relative))

        store = get_file_store()
        completed: list[str] = []
        entries: list[dict] = []
        try:
            for file, relative in targets:
                try:
                    content = await read_upload_with_limit(
                        file,
                        max_size_bytes=MAX_WORKSPACE_UPLOAD_SIZE_BYTES,
                        too_large_message="文件过大，当前仅支持 100 MB 以内的文件",
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                key = user_workspace_key(uid, relative)
                stat = await store.put(key, content, content_type=file.content_type)
                completed.append(key)
                entries.append(_workspace_file_entry(relative, stat))
        except Exception:
            for key in completed:
                await store.delete(key)
            raise

    await invalidate_workspace_mention_cache(uid)
    return {"success": True, "entries": entries}


async def put_workspace_file(*, uid: str, path: str, content: bytes, content_type: str | None = None) -> dict:
    """将字节写入 workspace 新文件，供服务间复制使用。"""
    async with sandbox_file_operation_lock(uid=uid):
        relative = _workspace_relative_path(path)
        if not relative:
            raise HTTPException(status_code=400, detail="当前路径必须是文件")
        parent = PurePosixPath(relative).parent.as_posix()
        parent = "" if parent == "." else parent
        await _require_workspace_directory(uid, parent)
        if await _workspace_path_kind(uid, relative) is not None:
            raise HTTPException(status_code=400, detail="同名文件或文件夹已存在")
        stat = await get_file_store().put(user_workspace_key(uid, relative), content, content_type=content_type)
    await invalidate_workspace_mention_cache(uid)
    return _workspace_file_entry(relative, stat)


async def ensure_workspace_directory(*, uid: str, path: str) -> dict:
    """确保 workspace 目录存在，不存在时写入隐藏 marker。"""
    async with sandbox_file_operation_lock(uid=uid):
        relative = _workspace_relative_path(path)
        kind = await _workspace_path_kind(uid, relative)
        if kind == "file":
            raise HTTPException(status_code=400, detail="目标路径不是目录")
        if kind == "directory":
            return _workspace_directory_entry(relative)
        parent = PurePosixPath(relative).parent.as_posix()
        parent = "" if parent == "." else parent
        await _require_workspace_directory(uid, parent)
        stat = await get_file_store().put(
            user_workspace_key(uid, f"{relative}/{KEEP_MARKER}"), b"", content_type="application/octet-stream"
        )
        return _workspace_directory_entry(relative, stat)


async def next_available_workspace_path(*, uid: str, parent_path: str, filename: str) -> str:
    """返回 workspace 目录内可用的冲突递增文件路径。"""
    parent = _workspace_relative_path(parent_path)
    await _require_workspace_directory(uid, parent)
    clean_name = _validate_child_name(filename, field_name="文件名")
    stem = PurePosixPath(clean_name).stem
    suffix = PurePosixPath(clean_name).suffix
    for index in range(1000):
        candidate_name = clean_name if index == 0 else f"{stem} ({index}){suffix}"
        relative = _join_relative(parent, candidate_name)
        if await _workspace_path_kind(uid, relative) is None:
            return _display_path(relative)
    raise RuntimeError(f"Unable to find available filename for {filename} after 1000 attempts.")


async def download_workspace_file(
    *, path: str, current_user: User, thread_titles: dict[str, str] | None = None
) -> StreamingResponse:
    """流式下载 workspace 或历史对话映射文件。"""
    uid = str(current_user.uid)
    await ensure_workspace_defaults(uid)
    key, display_path = await _resolve_readable_file_key(uid, path, thread_titles)
    store = get_file_store()
    try:
        stat = await store.stat(key)
    except FileStoreError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    filename = PurePosixPath(display_path).name or "download"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(
        store.stream(key), media_type=stat.content_type or detect_media_type(filename), headers=headers
    )


@contextlib.asynccontextmanager
async def materialize_workspace_file(
    *, path: str, current_user: User, thread_titles: dict[str, str] | None = None
) -> AsyncIterator[Path]:
    """将 FileStore 对象临时落盘，并在使用后删除。"""
    stored = await read_workspace_file_object(path=path, current_user=current_user, thread_titles=thread_titles)
    suffix = PurePosixPath(path).suffix
    with tempfile.TemporaryDirectory(prefix="yuxi-workspace-") as temp_dir:
        target = Path(temp_dir) / f"source{suffix}"
        target.write_bytes(stored.data)
        yield target


async def ensure_workspace_defaults(uid: str) -> None:
    """初始化 workspace 默认上下文文件，不覆盖已有对象。"""
    store = get_file_store()
    missing = [
        (filename, content)
        for filename, content in WORKSPACE_AGENT_CONTEXT_FILES.items()
        if await _workspace_path_kind(uid, f"{WORKSPACE_AGENTS_DIR_NAME}/{filename}") is None
    ]
    if not missing:
        return
    async with sandbox_file_operation_lock(uid=uid):
        for filename, default_content in missing:
            relative = f"{WORKSPACE_AGENTS_DIR_NAME}/{filename}"
            if await _workspace_path_kind(uid, relative) is not None:
                continue
            await store.put(
                user_workspace_key(uid, relative), default_content.encode("utf-8"), content_type="text/markdown"
            )


async def list_workspace_index_entries(uid: str) -> list[tuple[str, str]]:
    """返回 mention 搜索需要的 workspace 文件与目录索引。"""
    await ensure_workspace_defaults(uid)
    entries = await _list_workspace_directory(uid, "", recursive=True, files_only=False)
    return [(entry["name"], entry["path"].lstrip("/")) for entry in entries]


async def build_owned_thread_titles(db: AsyncSession, uid: str) -> dict[str, str]:
    """查询用户全部 active 对话，返回网页历史文件映射使用的标题。"""
    repo = ConversationRepository(db)
    conversations = await repo.list_active_conversations_for_user(str(uid))
    thread_titles = {}
    for conversation in conversations:
        try:
            thread_id = validate_thread_id(conversation.thread_id)
        except ValueError:
            logger.warning(f"跳过无法映射到文件系统的历史对话 thread_id: {conversation.thread_id}")
            continue
        created_date = conversation.created_at.strftime("%Y-%m-%d")
        title = (conversation.title or "").strip() or "未命名对话"
        thread_titles[thread_id] = f"{created_date}-{title}"
    return thread_titles


def is_workspace_chat_path(path: str | None) -> bool:
    """判断网页工作区路径是否属于历史对话虚拟命名空间。"""
    return _chat_path_parts(path) is not None


def workspace_path_uses_chat_mapping(path: str | None) -> bool:
    """判断工作区列表或文件请求是否需要加载用户对话白名单。"""
    normalized = _normalize_workspace_path(path).as_posix().rstrip("/") or "/"
    return normalized == f"/{WORKSPACE_AGENTS_DIR_NAME}" or is_workspace_chat_path(path)


def _normalize_workspace_path(path: str | None) -> PurePosixPath:
    raw_path = (path or "/").strip() or "/"
    if "\\" in raw_path:
        raise HTTPException(status_code=403, detail="Access denied")
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"
    parts = raw_path.split("/")
    if any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=403, detail="Access denied")
    return PurePosixPath(raw_path)


def _workspace_relative_path(path: str | None) -> str:
    return _normalize_workspace_path(path).as_posix().lstrip("/")


def _display_path(relative: str) -> str:
    return f"/{relative}" if relative else "/"


def _join_relative(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _physical_chats_path() -> str:
    return f"{WORKSPACE_AGENTS_DIR_NAME}/{WORKSPACE_CHATS_DIR_NAME}"


async def _workspace_path_exists(uid: str, relative: str) -> bool:
    return await _workspace_path_kind(uid, relative) is not None


async def _workspace_path_kind(uid: str, relative: str) -> str | None:
    if not relative:
        return "directory"
    store = get_file_store()
    key = user_workspace_key(uid, relative)
    try:
        await store.stat(key)
        return "file"
    except FileStoreError:
        return "directory" if await store.list(f"{key}/") else None


async def _require_workspace_directory(uid: str, relative: str) -> None:
    kind = await _workspace_path_kind(uid, relative)
    if kind is None:
        raise HTTPException(status_code=404, detail="目标目录不存在")
    if kind != "directory":
        raise HTTPException(status_code=400, detail="目标路径不是目录")


def _validate_child_name(name: str, *, field_name: str) -> str:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail=f"{field_name} 不能为空")
    if clean_name in {".", ".."} or "/" in clean_name or "\\" in clean_name:
        raise HTTPException(status_code=422, detail=f"{field_name} 不能包含路径分隔符")
    return clean_name


def _workspace_file_entry(relative: str, stat: ObjectStat) -> dict:
    path = _display_path(relative)
    return {
        "path": path,
        "virtual_path": f"{VIRTUAL_PATH_WORKSPACE}{path}",
        "name": PurePosixPath(relative).name,
        "is_dir": False,
        "size": stat.size,
        "modified_at": utc_isoformat(stat.modified),
    }


def _workspace_directory_entry(relative: str, stat: ObjectStat | None = None) -> dict:
    path = f"{_display_path(relative).rstrip('/')}/" if relative else "/"
    return {
        "path": path,
        "virtual_path": VIRTUAL_PATH_WORKSPACE if path == "/" else f"{VIRTUAL_PATH_WORKSPACE}{path}",
        "name": PurePosixPath(relative).name if relative else "工作区",
        "is_dir": True,
        "size": 0,
        "modified_at": utc_isoformat(stat.modified) if stat else "",
    }


async def _list_workspace_directory(uid: str, relative: str, *, recursive: bool, files_only: bool) -> list[dict]:
    prefix_key = (
        user_workspace_key(uid, relative)
        if relative
        else user_workspace_key(uid, KEEP_MARKER).rsplit("/", 1)[0]
    )
    prefix = f"{prefix_key}/"
    objects = await get_file_store().list(prefix)
    entries: dict[str, dict] = {}
    for item in objects:
        child_relative = item.key[len(prefix) :]
        if not child_relative:
            continue
        parts = child_relative.split("/")
        full_parts = ([*PurePosixPath(relative).parts] if relative else [])

        if recursive:
            for index in range(1, len(parts)):
                directory = PurePosixPath(*full_parts, *parts[:index]).as_posix()
                if parts[index - 1] != KEEP_MARKER and not files_only:
                    entries.setdefault(directory, _workspace_directory_entry(directory, item))
        elif len(parts) > 1:
            directory = PurePosixPath(*full_parts, parts[0]).as_posix()
            if parts[0] != KEEP_MARKER and not files_only:
                entries.setdefault(directory, _workspace_directory_entry(directory, item))
            continue

        if parts[-1] == KEEP_MARKER:
            marker_dir = PurePosixPath(*full_parts, *parts[:-1]).as_posix()
            if marker_dir and marker_dir != "." and not files_only:
                entries.setdefault(marker_dir, _workspace_directory_entry(marker_dir, item))
            continue
        file_relative = PurePosixPath(*full_parts, *parts).as_posix()
        entries[file_relative] = _workspace_file_entry(file_relative, item)
    return _sort_entries(list(entries.values()))


def _chat_path_parts(path: str | None) -> tuple[str, ...] | None:
    parts = tuple(part for part in _normalize_workspace_path(path).parts if part not in {"/", ""})
    prefix = (WORKSPACE_AGENTS_DIR_NAME, WORKSPACE_CHATS_DIR_NAME)
    return parts[2:] if parts[:2] == prefix else None


def _chat_namespace_key(thread_id: str, namespace: str, relative: str = "") -> str:
    if namespace == UPLOADS_DIR_NAME:
        return thread_upload_key(thread_id, relative or KEEP_MARKER).removesuffix(f"/{KEEP_MARKER}")
    if namespace == OUTPUTS_DIR_NAME:
        return thread_output_key(thread_id, relative or KEEP_MARKER).removesuffix(f"/{KEEP_MARKER}")
    raise HTTPException(status_code=404, detail="历史对话目录不存在")


async def _resolve_readable_file_key(
    uid: str, path: str, thread_titles: dict[str, str] | None
) -> tuple[str, str]:
    parts = _chat_path_parts(path)
    if parts is None:
        relative = _workspace_relative_path(path)
        if await _workspace_path_kind(uid, relative) != "file":
            kind = await _workspace_path_kind(uid, relative)
            if kind == "directory":
                raise HTTPException(status_code=400, detail=f"当前路径不是文件: {path}")
            raise HTTPException(status_code=404, detail=f"工作区文件不存在: {path}")
        return user_workspace_key(uid, relative), _display_path(relative)

    if await _workspace_path_exists(uid, _physical_chats_path()):
        raise HTTPException(status_code=409, detail="工作区 agents/chats 已被现有文件或目录占用")
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail=f"当前路径不是文件: {path}")
    thread_id, namespace, *relative_parts = parts
    if thread_id not in (thread_titles or {}):
        raise HTTPException(status_code=403, detail="Access denied")
    if relative_parts[0] in _CHAT_INTERMEDIATE_DIR_NAMES:
        raise HTTPException(status_code=404, detail="历史对话文件不存在")
    relative = "/".join(relative_parts)
    key = f"{_chat_namespace_key(thread_id, namespace)}/{relative}"
    try:
        await get_file_store().stat(key)
    except FileStoreError as exc:
        if await get_file_store().list(f"{key}/"):
            raise HTTPException(status_code=400, detail=f"当前路径不是文件: {path}") from exc
        raise HTTPException(status_code=404, detail=f"工作区文件不存在: {path}") from exc
    return key, _normalize_workspace_path(path).as_posix()


async def _visible_chat_objects(thread_id: str, namespace: str) -> list[ObjectStat]:
    prefix = f"{_chat_namespace_key(thread_id, namespace)}/"
    objects = await get_file_store().list(prefix)
    visible = []
    for item in objects:
        relative = item.key[len(prefix) :]
        if not relative or relative == KEEP_MARKER:
            continue
        first = relative.split("/", 1)[0]
        if first in _CHAT_INTERMEDIATE_DIR_NAMES:
            continue
        visible.append(item)
    return visible


async def _list_chat_directory(
    path: str,
    *,
    thread_titles: dict[str, str],
    recursive: bool,
    files_only: bool,
) -> list[dict]:
    parts = _chat_path_parts(path)
    if parts is None:
        return []
    if not parts:
        entries = []
        for thread_id, title in thread_titles.items():
            has_visible = bool(
                await _visible_chat_objects(thread_id, UPLOADS_DIR_NAME)
                or await _visible_chat_objects(thread_id, OUTPUTS_DIR_NAME)
            )
            if not has_visible:
                continue
            thread_path = f"/agents/chats/{thread_id}"
            if not files_only:
                entries.append(_virtual_entry(thread_path, name=thread_id, title=title, is_dir=True))
            if recursive:
                entries.extend(
                    await _list_chat_directory(
                        thread_path, thread_titles=thread_titles, recursive=True, files_only=files_only
                    )
                )
        return _sort_chat_entries(entries)

    thread_id = parts[0]
    if thread_id not in thread_titles:
        raise HTTPException(status_code=403, detail="Access denied")
    if len(parts) == 1:
        entries = []
        for namespace in (UPLOADS_DIR_NAME, OUTPUTS_DIR_NAME):
            objects = await _visible_chat_objects(thread_id, namespace)
            if not objects:
                continue
            namespace_path = f"/agents/chats/{thread_id}/{namespace}"
            if not files_only:
                entries.append(_virtual_entry(namespace_path, name=namespace, is_dir=True, source=objects[0]))
            if recursive:
                entries.extend(
                    await _list_chat_directory(
                        namespace_path, thread_titles=thread_titles, recursive=True, files_only=files_only
                    )
                )
        return _sort_entries(entries)

    namespace = parts[1]
    relative_root = "/".join(parts[2:])
    if relative_root and relative_root.split("/", 1)[0] in _CHAT_INTERMEDIATE_DIR_NAMES:
        raise HTTPException(status_code=404, detail="历史对话文件不存在")
    base_key = _chat_namespace_key(thread_id, namespace)
    prefix = f"{base_key}/{relative_root}/" if relative_root else f"{base_key}/"
    objects = await get_file_store().list(prefix)
    if not objects:
        return []

    display_root = f"/agents/chats/{thread_id}/{namespace}"
    if relative_root:
        display_root = f"{display_root}/{relative_root}"
    entries: dict[str, dict] = {}
    for item in objects:
        relative = item.key[len(prefix) :]
        if not relative:
            continue
        parts_tail = relative.split("/")
        if parts_tail[0] in _CHAT_INTERMEDIATE_DIR_NAMES:
            continue
        if recursive:
            for index in range(1, len(parts_tail)):
                directory_path = f"{display_root}/{'/'.join(parts_tail[:index])}"
                if not files_only:
                    entries.setdefault(
                        directory_path,
                        _virtual_entry(directory_path, name=parts_tail[index - 1], is_dir=True, source=item),
                    )
        elif len(parts_tail) > 1:
            directory_path = f"{display_root}/{parts_tail[0]}"
            if not files_only:
                entries.setdefault(
                    directory_path, _virtual_entry(directory_path, name=parts_tail[0], is_dir=True, source=item)
                )
            continue
        if parts_tail[-1] == KEEP_MARKER:
            marker_relative = "/".join(parts_tail[:-1])
            if marker_relative and not files_only:
                marker_path = f"{display_root}/{marker_relative}"
                entries.setdefault(
                    marker_path, _virtual_entry(marker_path, name=parts_tail[-2], is_dir=True, source=item)
                )
            continue
        file_path = f"{display_root}/{relative}"
        entries[file_path] = _virtual_entry(file_path, name=parts_tail[-1], is_dir=False, source=item)
    return _sort_entries(list(entries.values()))


def _virtual_entry(
    path: str, *, name: str, is_dir: bool, title: str | None = None, source: ObjectStat | None = None
) -> dict:
    display_path = f"{path.rstrip('/')}/" if is_dir else path
    entry = {
        "path": display_path,
        "virtual_path": f"{VIRTUAL_PATH_WORKSPACE}{display_path}",
        "name": name,
        "is_dir": is_dir,
        "size": 0 if is_dir or source is None else source.size,
        "modified_at": utc_isoformat(source.modified) if source else "",
        "readonly": True,
    }
    if title:
        entry["title"] = title
    return entry


def _reject_chat_write(path: str) -> None:
    if _chat_path_parts(path) is not None:
        raise HTTPException(status_code=403, detail=_CHAT_READONLY_MESSAGE)


def _sort_entries(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").lower()))


def _sort_chat_entries(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda item: str(item.get("title") or item.get("name") or "").lower(), reverse=True)


def _preview_binary_response(*, filename: str, content: bytes, media_type: str, preview_type: str) -> StreamingResponse:
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
        "X-Yuxi-Preview-Type": preview_type,
        "X-Yuxi-Preview-Filename": quote(filename),
    }
    return StreamingResponse(io.BytesIO(content), media_type=media_type, headers=headers)
