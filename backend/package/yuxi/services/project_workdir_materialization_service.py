"""把旧线程文件一次性物化为实时 Project Workdir。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select, text

from yuxi.agents.backends.sandbox.paths import project_workdir_host_dir, project_workdir_virtual_dir
from yuxi.config import get_save_dir
from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.project_workdir_repository import (
    FileStorageMaterializationRepository,
    ProjectWorkdirRepository,
)
from yuxi.services.attachment_service import (
    MAX_ATTACHMENT_SIZE_BYTES,
    _make_attachment_path,
    _safe_file_name,
)
from yuxi.storage.minio import StorageError, get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, Conversation, Message, ToolCall
from yuxi.utils.datetime_utils import utc_now_naive

MATERIALIZATION_LOCK_KEY = "yuxi:project-workdir-v1"
MAX_MATERIALIZED_FILES_PER_WORKDIR = 1000
MAX_MATERIALIZED_DIRECTORIES_PER_WORKDIR = 2000
MAX_MATERIALIZED_BYTES_PER_WORKDIR = 100 * 1024 * 1024
MAX_MATERIALIZED_DIRECTORY_DEPTH = 64
_LEGACY_USER_DATA_ROOT = "/home/gem/user-data"
_LEGACY_OUTPUT_TABLE = "thread_output_revisions"
_LEGACY_OUTPUT_POINTER = "current_output_revision_id"
_LEGACY_ATTACHMENT_STORAGE_FIELDS = (
    "bucket_name",
    "original_object_name",
    "markdown_object_name",
    "minio_url",
)


class FileStorageNotReadyError(RuntimeError):
    """实时 Workdir 主链路尚未全局激活。"""


@dataclass(frozen=True, slots=True)
class LegacyFileSource:
    """一份经过完整性采集的旧文件来源。"""

    target_path: str
    size: int
    sha256: str
    bucket_name: str | None = None
    object_name: str | None = None
    host_root: Path | None = None
    host_parts: tuple[str, ...] = ()
    inline_content: bytes | None = None
    is_directory: bool = False

    def fingerprint_payload(self) -> str:
        entry_type = "directory" if self.is_directory else "file"
        return f"{entry_type}\0{self.target_path}\0{self.size}\0{self.sha256}"


@dataclass(frozen=True, slots=True)
class WorkdirInventory:
    """一个 Workdir 在维护 fence 下的最终旧文件清单。"""

    workdir_id: str
    uid: str
    sources: tuple[LegacyFileSource, ...]
    fingerprint: str


def _target_path_from_legacy_virtual_path(path: str) -> str:
    normalized = str(PurePosixPath(str(path or "")))
    prefix = f"{_LEGACY_USER_DATA_ROOT}/"
    if not normalized.startswith(prefix):
        raise ValueError(f"旧文件路径不属于 user-data: {path}")
    relative = normalized[len(prefix) :]
    if not relative or ".." in PurePosixPath(relative).parts:
        raise ValueError(f"旧文件路径无效: {path}")
    return relative


async def _legacy_output_schema_state(db) -> tuple[bool, bool]:
    """检测升级前 outputs pointer/table 是否仍存在。"""
    row = (
        await db.execute(
            text(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM pg_attribute
                        WHERE attrelid = to_regclass('conversations')
                          AND attname = :pointer
                          AND NOT attisdropped
                    ) AS has_pointer,
                    to_regclass(:table_name) IS NOT NULL AS has_revision_table
                """
            ),
            {"pointer": _LEGACY_OUTPUT_POINTER, "table_name": _LEGACY_OUTPUT_TABLE},
        )
    ).one()
    return bool(row.has_pointer), bool(row.has_revision_table)


def _validate_legacy_output_revision_scope(conversation: Conversation, row) -> None:
    """校验旧 revision 行仍属于当前 Conversation 与用户。"""
    if row.thread_id != conversation.thread_id or str(row.uid) != str(conversation.uid):
        raise ValueError(f"Conversation {conversation.thread_id} 的 current outputs revision 作用域无效")


async def _legacy_current_output_files(db, conversation: Conversation) -> tuple[str, list[dict]] | None:
    """只为一次性升级读取旧 current revision；新 schema 返回 None。"""
    has_pointer, has_revision_table = await _legacy_output_schema_state(db)
    if not has_pointer:
        return None
    revision_id = await db.scalar(
        text("SELECT current_output_revision_id FROM conversations WHERE id = :conversation_id"),
        {"conversation_id": conversation.id},
    )
    if revision_id is None:
        return None
    if not has_revision_table:
        raise ValueError(f"Conversation {conversation.thread_id} 的 current outputs revision 表不存在")
    row = (
        await db.execute(
            text(
                """
                SELECT thread_id, uid, files
                FROM thread_output_revisions
                WHERE id = :revision_id
                  AND conversation_id = :conversation_id
                  AND status = 'published'
                """
            ),
            {"revision_id": revision_id, "conversation_id": conversation.id},
        )
    ).one_or_none()
    if row is None:
        raise ValueError(f"Conversation {conversation.thread_id} 的 current outputs revision 不可用")
    _validate_legacy_output_revision_scope(conversation, row)
    files = row.files
    if not isinstance(files, list):
        raise ValueError(f"Conversation {conversation.thread_id} 的 current outputs manifest 无效")
    revision_id = str(revision_id)
    if not revision_id or any(marker in revision_id for marker in ("/", "\\")):
        raise ValueError(f"Conversation {conversation.thread_id} 的 current outputs revision identity 无效")
    return revision_id, files


def _read_host_file(root: Path, parts: tuple[str, ...]) -> bytes:
    """从旧目录以 root-to-leaf no-follow 方式读取普通文件。"""
    if not parts:
        raise ValueError("旧文件相对路径不能为空")
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_fds.append(directory_fd)
        for part in parts[:-1]:
            directory_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            directory_fds.append(directory_fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("旧文件来源不是普通文件")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(file_fd, 1024 * 1024):
            size += len(chunk)
            if size > MAX_MATERIALIZED_BYTES_PER_WORKDIR:
                raise ValueError("单个旧文件超过 Workdir 物化上限")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _scan_host_tree(root: Path, namespace: str) -> list[LegacyFileSource]:
    """不跟随链接地枚举一个旧 uploads/outputs 目录。"""
    if not root.exists():
        return []
    sources: list[LegacyFileSource] = []
    pending: list[tuple[str, ...]] = [()]
    directory_count = 0
    while pending:
        prefix = pending.pop()
        if len(prefix) > MAX_MATERIALIZED_DIRECTORY_DEPTH:
            raise ValueError("旧文件目录层级超过 Workdir 物化上限")
        directory_fds: list[int] = []
        try:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            directory_fds.append(directory_fd)
            for part in prefix:
                directory_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                directory_fds.append(directory_fd)
            entries = []
            for name in sorted(os.listdir(directory_fd)):
                entries.append((name, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)))
        finally:
            for opened_fd in reversed(directory_fds):
                os.close(opened_fd)

        for name, item_stat in entries:
            parts = (*prefix, name)
            target_path = "/".join(part for part in (namespace, *parts) if part)
            if stat.S_ISLNK(item_stat.st_mode):
                raise ValueError(f"旧文件目录包含符号链接: {target_path}")
            if stat.S_ISDIR(item_stat.st_mode):
                directory_count += 1
                if directory_count > MAX_MATERIALIZED_DIRECTORIES_PER_WORKDIR:
                    raise ValueError("旧文件目录数量超过 Workdir 物化上限")
                sources.append(
                    LegacyFileSource(
                        target_path=target_path,
                        size=0,
                        sha256="",
                        is_directory=True,
                    )
                )
                pending.append(parts)
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                raise ValueError(f"旧文件目录包含特殊文件: {target_path}")
            content = _read_host_file(root, parts)
            sources.append(
                LegacyFileSource(
                    target_path=target_path,
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    host_root=root,
                    host_parts=parts,
                )
            )
    return sources


async def _object_source(
    *,
    target_path: str,
    bucket_name: str,
    object_name: str,
) -> LegacyFileSource:
    content = await _download_object_content(bucket_name, object_name)
    return LegacyFileSource(
        target_path=target_path,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        bucket_name=bucket_name,
        object_name=object_name,
    )


async def _output_sources(
    conversation: Conversation,
    revision_id: str,
    descriptors: list[dict],
) -> list[LegacyFileSource]:
    """校验旧 outputs manifest 与线程、对象和路径的完整绑定。"""
    minio = get_minio_client()
    expected_bucket = minio.KB_BUCKETS["documents"]
    path_prefix = f"{_LEGACY_USER_DATA_ROOT}/outputs/"
    object_prefix = f"threads/{conversation.thread_id}/outputs/revisions/{revision_id}/"
    sources: list[LegacyFileSource] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 无效")
        bucket_name = descriptor.get("bucket_name")
        object_name = descriptor.get("object_name")
        path = descriptor.get("path")
        if bucket_name != expected_bucket or not isinstance(object_name, str) or not isinstance(path, str):
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 作用域无效")
        target_path = _target_path_from_legacy_virtual_path(path)
        if not path.startswith(path_prefix) or not target_path.startswith("outputs/"):
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 路径无效")
        relative_path = target_path.removeprefix("outputs/")
        if not relative_path or object_name != f"{object_prefix}{relative_path}":
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 对象作用域无效")
        size = descriptor.get("size")
        sha256 = descriptor.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_MATERIALIZED_BYTES_PER_WORKDIR
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 完整性无效")
        try:
            bytes.fromhex(sha256)
        except ValueError as exc:
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 完整性无效") from exc
        source = await _object_source(
            target_path=target_path,
            bucket_name=expected_bucket,
            object_name=object_name,
        )
        if source.size != size or source.sha256 != sha256:
            raise ValueError(f"Conversation {conversation.thread_id} 的 outputs descriptor 内容不一致")
        sources.append(source)
    return sources


async def _download_object_content(bucket_name: str, object_name: str) -> bytes:
    """流式读取旧对象，并在进入 worker 内存上限前终止异常对象。"""
    try:
        response = await get_minio_client().adownload_response(bucket_name, object_name)
    except StorageError as exc:
        raise FileNotFoundError(f"旧文件对象不存在: {bucket_name}/{object_name}") from exc
    chunks: list[bytes] = []
    size = 0
    try:
        while chunk := await asyncio.to_thread(response.read, 1024 * 1024):
            size += len(chunk)
            if size > MAX_MATERIALIZED_BYTES_PER_WORKDIR:
                raise ValueError(f"旧文件对象超过 Workdir 物化上限: {bucket_name}/{object_name}")
            chunks.append(chunk)
    finally:
        try:
            response.close()
        finally:
            response.release_conn()
    return b"".join(chunks)


async def _attachment_sources(conversation: Conversation, attachments: list[dict]) -> list[LegacyFileSource]:
    """把旧附件记录收敛为物化来源，不暴露运行时 hydrate 接口。"""
    expected_bucket = get_minio_client().KB_BUCKETS["documents"]
    legacy_root = get_save_dir() / "threads" / conversation.thread_id / "user-data" / "uploads"
    sources: list[LegacyFileSource] = []
    seen_paths: set[str] = set()
    for attachment in attachments:
        file_id = attachment.get("file_id")
        file_name = attachment.get("file_name")
        if (
            not isinstance(file_id, str)
            or not file_id
            or any(marker in file_id for marker in ("/", "\\"))
            or not isinstance(file_name, str)
        ):
            raise ValueError("旧附件记录作用域无效")
        file_size = attachment.get("file_size")
        if file_size is not None and (
            not isinstance(file_size, int) or file_size < 0 or file_size > MAX_ATTACHMENT_SIZE_BYTES
        ):
            raise ValueError("旧附件记录大小无效")

        safe_file_name = _safe_file_name(file_name)
        storage_name = f"{file_id}_{safe_file_name}"
        expected_original_path = f"{_LEGACY_USER_DATA_ROOT}/uploads/{storage_name}"
        direct_upload_path = f"{_LEGACY_USER_DATA_ROOT}/uploads/{safe_file_name}"
        recorded_path = attachment.get("path")
        original_path = attachment.get("original_path")
        if not isinstance(original_path, str) and isinstance(recorded_path, str):
            original_path = recorded_path

        bucket_name = attachment.get("bucket_name")
        original_object_name = attachment.get("original_object_name")
        expected_prefix = f"threads/{conversation.thread_id}/attachments/{file_id}"
        expected_original_object = f"{expected_prefix}/original/{safe_file_name}"
        expected_markdown_object = f"{expected_prefix}/parsed/{Path(safe_file_name).stem or 'attachment'}.md"
        if bucket_name is None and original_object_name is None:
            if original_path == direct_upload_path:
                storage_name = safe_file_name
            elif original_path != expected_original_path:
                raise ValueError("旧附件虚拟路径作用域无效")
            content = await asyncio.to_thread(_read_host_file, legacy_root, (Path(original_path).name,))
            if len(content) > MAX_ATTACHMENT_SIZE_BYTES:
                raise ValueError("旧附件超过大小限制")
            sources.append(
                LegacyFileSource(
                    target_path=_target_path_from_legacy_virtual_path(original_path),
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    host_root=legacy_root,
                    host_parts=(Path(original_path).name,),
                )
            )
        elif bucket_name == expected_bucket and original_object_name == expected_original_object:
            if original_path != expected_original_path:
                raise ValueError("旧附件虚拟路径作用域无效")
            sources.append(
                await _object_source(
                    target_path=_target_path_from_legacy_virtual_path(original_path),
                    bucket_name=expected_bucket,
                    object_name=expected_original_object,
                )
            )
        else:
            raise ValueError("旧附件对象作用域无效")

        markdown_object_name = attachment.get("markdown_object_name")
        if recorded_path not in (None, original_path):
            expected_markdown_path = (
                f"{_LEGACY_USER_DATA_ROOT}/uploads/attachments/{_make_attachment_path(storage_name)}"
            )
            if recorded_path != expected_markdown_path:
                raise ValueError("旧附件解析路径作用域无效")
            if markdown_object_name is not None:
                if bucket_name != expected_bucket or markdown_object_name != expected_markdown_object:
                    raise ValueError("旧附件解析对象作用域无效")
                sources.append(
                    await _object_source(
                        target_path=_target_path_from_legacy_virtual_path(recorded_path),
                        bucket_name=expected_bucket,
                        object_name=expected_markdown_object,
                    )
                )
            elif isinstance(attachment.get("markdown"), str):
                content = attachment["markdown"].encode("utf-8")
                sources.append(
                    LegacyFileSource(
                        target_path=_target_path_from_legacy_virtual_path(recorded_path),
                        size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        inline_content=content,
                    )
                )
            else:
                parts = ("attachments", Path(recorded_path).name)
                content = await asyncio.to_thread(_read_host_file, legacy_root, parts)
                if len(content) > MAX_ATTACHMENT_SIZE_BYTES:
                    raise ValueError("旧附件超过大小限制")
                sources.append(
                    LegacyFileSource(
                        target_path=_target_path_from_legacy_virtual_path(recorded_path),
                        size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        host_root=legacy_root,
                        host_parts=parts,
                    )
                )
        elif markdown_object_name is not None:
            raise ValueError("旧附件解析对象作用域无效")

    for source in sources:
        if source.target_path in seen_paths:
            raise ValueError("旧附件虚拟路径重复")
        seen_paths.add(source.target_path)
    return sources


async def _conversation_sources(db, conversation: Conversation) -> list[LegacyFileSource]:
    sources = [
        LegacyFileSource(target_path=namespace, size=0, sha256="", is_directory=True)
        for namespace in ("uploads", "outputs")
    ]
    sources.extend(
        await _attachment_sources(
            conversation,
            await ConversationRepository(db).get_attachments(conversation.id),
        )
    )
    legacy_root = get_save_dir() / "threads" / conversation.thread_id / "user-data"
    sources.extend(await asyncio.to_thread(_scan_host_tree, legacy_root / "uploads", "uploads"))

    current_revision = await _legacy_current_output_files(db, conversation)
    if current_revision is None:
        sources.extend(await asyncio.to_thread(_scan_host_tree, legacy_root / "outputs", "outputs"))
    else:
        revision_id, descriptors = current_revision
        sources.extend(await _output_sources(conversation, revision_id, descriptors))
    return sources


def _deduplicate_sources(sources: list[LegacyFileSource]) -> tuple[LegacyFileSource, ...]:
    namespace_roots = {
        PurePosixPath(source.target_path).parts[0]
        for source in sources
        if PurePosixPath(source.target_path).parts
        and PurePosixPath(source.target_path).parts[0] in {"uploads", "outputs"}
    }
    sources = [
        *(LegacyFileSource(target_path=root, size=0, sha256="", is_directory=True) for root in namespace_roots),
        *sources,
    ]
    by_path: dict[str, LegacyFileSource] = {}
    for source in sources:
        previous = by_path.get(source.target_path)
        if previous and (
            previous.is_directory != source.is_directory
            or previous.size != source.size
            or previous.sha256 != source.sha256
        ):
            raise ValueError(f"旧文件在同一 Workdir 路径发生内容冲突: {source.target_path}")
        by_path[source.target_path] = previous or source
    ordered = tuple(by_path[path] for path in sorted(by_path))
    file_sources = [source for source in ordered if not source.is_directory]
    directory_sources = [source for source in ordered if source.is_directory]
    total_size = sum(source.size for source in file_sources)
    if len(file_sources) > MAX_MATERIALIZED_FILES_PER_WORKDIR:
        raise ValueError("Workdir 旧文件数量超过物化上限")
    if len(directory_sources) > MAX_MATERIALIZED_DIRECTORIES_PER_WORKDIR:
        raise ValueError("Workdir 旧目录数量超过物化上限")
    if total_size > MAX_MATERIALIZED_BYTES_PER_WORKDIR:
        raise ValueError("Workdir 旧文件总大小超过物化上限")
    return ordered


def _inventory_fingerprint(sources: tuple[LegacyFileSource, ...]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.fingerprint_payload().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


async def collect_legacy_inventory(db) -> tuple[WorkdirInventory, ...]:
    """在 fence 下读取全部旧文件事实并按 Workdir 合并。"""
    result = await db.execute(select(Conversation).where(Conversation.status != "deleted").order_by(Conversation.id))
    conversations = list(result.scalars().all())
    workdirs = {workdir.id: workdir for workdir in await ProjectWorkdirRepository(db).list_all()}
    grouped: dict[str, list[LegacyFileSource]] = {workdir_id: [] for workdir_id in workdirs}
    for conversation in conversations:
        if not conversation.workdir_id or conversation.workdir_id not in workdirs:
            raise ValueError(f"Conversation {conversation.thread_id} 缺少有效 Workdir")
        grouped[conversation.workdir_id].extend(await _conversation_sources(db, conversation))

    inventories: list[WorkdirInventory] = []
    for workdir_id in sorted(workdirs):
        sources = _deduplicate_sources(grouped.get(workdir_id, []))
        inventories.append(
            WorkdirInventory(
                workdir_id=workdir_id,
                uid=str(workdirs[workdir_id].uid),
                sources=sources,
                fingerprint=_inventory_fingerprint(sources),
            )
        )
    return tuple(inventories)


def global_inventory_fingerprint(inventories: tuple[WorkdirInventory, ...]) -> str:
    digest = hashlib.sha256()
    for inventory in inventories:
        digest.update(f"{inventory.workdir_id}\0{inventory.uid}\0{inventory.fingerprint}\n".encode())
    return digest.hexdigest()


def _rewrite_legacy_project_path(path: object, workdir_path: str) -> object:
    """把旧 uploads/outputs 虚拟路径改写到当前 Project Workdir。"""
    if not isinstance(path, str):
        return path
    for namespace in ("uploads", "outputs"):
        legacy_root = f"{_LEGACY_USER_DATA_ROOT}/{namespace}"
        if path == legacy_root or path.startswith(f"{legacy_root}/"):
            return f"{workdir_path}{path[len(_LEGACY_USER_DATA_ROOT) :]}"
    return path


def _rewrite_attachment_record(thread_id: str, workdir_path: str, record: dict) -> dict:
    rewritten = dict(record)
    for field in ("path", "original_path", "file_path"):
        rewritten[field] = _rewrite_legacy_project_path(rewritten.get(field), workdir_path)
    if isinstance(rewritten.get("path"), str):
        rewritten["artifact_url"] = f"/api/chat/thread/{thread_id}/artifacts/{rewritten['path'].lstrip('/')}"
    if isinstance(rewritten.get("original_path"), str):
        rewritten["original_artifact_url"] = (
            f"/api/chat/thread/{thread_id}/artifacts/{rewritten['original_path'].lstrip('/')}"
        )
    return rewritten


async def _rewrite_persisted_project_paths(db, conversations: list[Conversation]) -> None:
    """在 activation 事务内改写仍被 shipping history/附件消费的旧虚拟路径。"""
    conversation_by_id = {conversation.id: conversation for conversation in conversations}
    for conversation in conversations:
        metadata = dict(conversation.extra_metadata or {})
        attachments = metadata.get("attachments")
        if isinstance(attachments, list):
            workdir_path = project_workdir_virtual_dir(conversation.workdir_id)
            metadata["attachments"] = [
                _rewrite_attachment_record(conversation.thread_id, workdir_path, item)
                if isinstance(item, dict)
                else item
                for item in attachments
            ]
            conversation.extra_metadata = metadata

    conversation_ids = list(conversation_by_id)
    if not conversation_ids:
        return
    result = await db.execute(
        select(ToolCall, Message.conversation_id)
        .join(Message, Message.id == ToolCall.message_id)
        .where(
            Message.conversation_id.in_(conversation_ids),
            ToolCall.tool_name == "present_artifacts",
        )
    )
    for tool_call, conversation_id in result.all():
        conversation = conversation_by_id.get(conversation_id)
        tool_input = dict(tool_call.tool_input or {})
        filepaths = tool_input.get("filepaths")
        if conversation is None or not isinstance(filepaths, list):
            continue
        workdir_path = project_workdir_virtual_dir(conversation.workdir_id)
        tool_input["filepaths"] = [_rewrite_legacy_project_path(path, workdir_path) for path in filepaths]
        tool_call.tool_input = tool_input
    await db.flush()


def _strip_legacy_attachment_storage(record: dict) -> dict:
    """移除已经由实时 Workdir 取代的对象存储定位字段。"""
    return {key: value for key, value in record.items() if key not in _LEGACY_ATTACHMENT_STORAGE_FIELDS}


def _is_legacy_file_storage_object(object_name: str) -> bool:
    """只识别 Stage 3/4 曾拥有的正式附件与 outputs 对象。"""
    if not object_name or str(PurePosixPath(object_name)) != object_name:
        return False
    parts = PurePosixPath(object_name).parts
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if len(parts) >= 6 and parts[0] == "threads" and parts[2] == "attachments":
        return parts[4] in {"original", "parsed"}
    return len(parts) >= 6 and parts[0] == "threads" and parts[2] == "outputs" and parts[3] == "revisions"


async def _cleanup_legacy_file_storage(db) -> None:
    """在物化激活后删除旧对象，并事务性移除旧 schema/元数据。"""
    has_pointer, has_revision_table = await _legacy_output_schema_state(db)
    conversations = list((await db.scalars(select(Conversation).order_by(Conversation.id))).all())
    has_legacy_attachment_metadata = False
    for conversation in conversations:
        attachments = (conversation.extra_metadata or {}).get("attachments")
        if not isinstance(attachments, list):
            continue
        has_legacy_attachment_metadata = has_legacy_attachment_metadata or any(
            isinstance(item, dict) and any(field in item for field in _LEGACY_ATTACHMENT_STORAGE_FIELDS)
            for item in attachments
        )
    if not has_pointer and not has_revision_table and not has_legacy_attachment_metadata:
        await db.rollback()
        return

    minio = get_minio_client()
    bucket_name = minio.KB_BUCKETS["documents"]
    object_names = await minio.alist_object_names(bucket_name, "threads/")
    for object_name in sorted(name for name in object_names if _is_legacy_file_storage_object(name)):
        await minio.adelete_file(bucket_name, object_name)

    await db.rollback()
    conversations = list((await db.scalars(select(Conversation).order_by(Conversation.id).with_for_update())).all())
    for conversation in conversations:
        metadata = dict(conversation.extra_metadata or {})
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list):
            continue
        rewritten = [_strip_legacy_attachment_storage(item) if isinstance(item, dict) else item for item in attachments]
        if rewritten != attachments:
            metadata["attachments"] = rewritten
            conversation.extra_metadata = metadata
    await db.flush()
    if has_revision_table:
        await db.execute(text("DROP TABLE thread_output_revisions"))
    if has_pointer:
        await db.execute(text("ALTER TABLE conversations DROP COLUMN current_output_revision_id"))
    await db.commit()


async def _source_content(source: LegacyFileSource) -> bytes:
    if source.is_directory:
        raise ValueError(f"旧目录不能按文件读取: {source.target_path}")
    if source.inline_content is not None:
        content = source.inline_content
    elif source.bucket_name and source.object_name:
        content = await _download_object_content(source.bucket_name, source.object_name)
    elif source.host_root is not None and source.host_parts:
        content = await asyncio.to_thread(_read_host_file, source.host_root, source.host_parts)
    else:
        raise ValueError(f"旧文件来源不可读: {source.target_path}")
    if len(content) != source.size or hashlib.sha256(content).hexdigest() != source.sha256:
        raise ValueError(f"旧文件在 inventory 后发生变化: {source.target_path}")
    return content


def _write_staged_file(staging_root: Path, relative_path: str, content: bytes) -> None:
    parts = PurePosixPath(relative_path).parts
    if not parts or ".." in parts:
        raise ValueError(f"Workdir 目标路径无效: {relative_path}")
    target = staging_root.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _activate_staged_workdir(staging_root: Path, workdir_id: str) -> None:
    final_root = project_workdir_host_dir(workdir_id)
    final_root.parent.mkdir(parents=True, exist_ok=True)
    quarantine = final_root.parent / f".old-{workdir_id}-{uuid.uuid4().hex}"
    if final_root.exists():
        os.replace(final_root, quarantine)
    try:
        os.replace(staging_root, final_root)
    except Exception:
        if quarantine.exists() and not final_root.exists():
            os.replace(quarantine, final_root)
        raise
    if quarantine.exists():
        shutil.rmtree(quarantine)


async def materialize_inventory_epoch(
    *,
    epoch_id: str,
    inventories: tuple[WorkdirInventory, ...],
) -> None:
    """把一个完整 epoch 写入隔离目录，逐 Workdir 校验后原子替换 inactive 根。"""
    staging_epoch_root = get_save_dir() / "project-materialization" / epoch_id
    await asyncio.to_thread(shutil.rmtree, staging_epoch_root, True)
    staging_epoch_root.mkdir(parents=True, exist_ok=True)
    try:
        for inventory in inventories:
            staging_root = staging_epoch_root / inventory.workdir_id
            staging_root.mkdir(parents=True, exist_ok=False)
            for source in inventory.sources:
                if source.is_directory:
                    staging_root.joinpath(*PurePosixPath(source.target_path).parts).mkdir(parents=True, exist_ok=True)
                    continue
                content = await _source_content(source)
                await asyncio.to_thread(_write_staged_file, staging_root, source.target_path, content)
            verified = await asyncio.to_thread(_scan_host_tree, staging_root, "")
            normalized_verified = tuple(
                sorted(
                    (
                        LegacyFileSource(
                            target_path=item.target_path.lstrip("/"),
                            size=item.size,
                            sha256=item.sha256,
                            host_root=item.host_root,
                            host_parts=item.host_parts,
                            is_directory=item.is_directory,
                        )
                        for item in verified
                    ),
                    key=lambda item: item.target_path,
                )
            )
            if _inventory_fingerprint(normalized_verified) != inventory.fingerprint:
                raise ValueError(f"Workdir {inventory.workdir_id} staging 校验失败")
            await asyncio.to_thread(_activate_staged_workdir, staging_root, inventory.workdir_id)
    finally:
        await asyncio.to_thread(shutil.rmtree, staging_epoch_root, True)


async def _nonterminal_run_count(db) -> int:
    result = await db.execute(
        select(func.count()).select_from(AgentRun).where(AgentRun.status.notin_(TERMINAL_RUN_STATUSES))
    )
    return int(result.scalar_one())


@asynccontextmanager
async def _materialization_advisory_lock():
    """在一条专用 PostgreSQL 连接上持有完整物化周期的 session lock。"""

    engine = pg_manager.async_engine
    if engine is None:
        raise RuntimeError("PostgreSQL engine 未初始化")
    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(hashtext(:key))"), {"key": MATERIALIZATION_LOCK_KEY})
        try:
            yield
        finally:
            unlocked = await connection.scalar(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": MATERIALIZATION_LOCK_KEY},
            )
            if unlocked is not True:
                await connection.invalidate()
                raise RuntimeError("释放 Project Workdir 物化 advisory lock 失败")


async def ensure_project_workdir_materialized() -> None:
    """串行完成全量 inventory、物化、复核与一次性 activation。"""
    async with _materialization_advisory_lock():
        async with pg_manager.get_async_session_context() as db:
            try:
                control_repo = FileStorageMaterializationRepository(db)
                control = await control_repo.get(for_update=True)
                if control.phase == "active":
                    await _cleanup_legacy_file_storage(db)
                    return
                if await _nonterminal_run_count(db):
                    raise FileStorageNotReadyError("仍有非终态 AgentRun，不能采集最终旧文件 inventory")

                epoch_id = str(uuid.uuid4())
                await control_repo.set_phase(control, phase="fenced", epoch_id=epoch_id)
                await db.commit()

                inventories = await collect_legacy_inventory(db)
                fingerprint = global_inventory_fingerprint(inventories)
                control = await control_repo.get(for_update=True)
                await control_repo.set_phase(
                    control,
                    phase="preparing",
                    epoch_id=epoch_id,
                    inventory_fingerprint=fingerprint,
                )
                workdir_repo = ProjectWorkdirRepository(db)
                for workdir in await workdir_repo.list_all(for_update=True):
                    inventory = next(item for item in inventories if item.workdir_id == workdir.id)
                    await workdir_repo.set_materialization_result(
                        workdir,
                        status="importing",
                        epoch_id=epoch_id,
                        source_fingerprint=inventory.fingerprint,
                    )
                await db.commit()

                await materialize_inventory_epoch(epoch_id=epoch_id, inventories=inventories)

                workdirs = await workdir_repo.list_all(for_update=True)
                for workdir in workdirs:
                    inventory = next(item for item in inventories if item.workdir_id == workdir.id)
                    await workdir_repo.set_materialization_result(
                        workdir,
                        status="prepared",
                        epoch_id=epoch_id,
                        source_fingerprint=inventory.fingerprint,
                    )
                await db.commit()

                final_inventories = await collect_legacy_inventory(db)
                if global_inventory_fingerprint(final_inventories) != fingerprint:
                    raise ValueError("旧文件 inventory 在 activation 前发生变化")

                control = await control_repo.get(for_update=True)
                workdirs = await workdir_repo.list_all(for_update=True)
                if control.epoch_id != epoch_id or control.phase != "preparing":
                    raise RuntimeError("文件物化 epoch ownership 已变化")
                final_by_id = {item.workdir_id: item for item in final_inventories}
                for workdir in workdirs:
                    inventory = final_by_id.get(workdir.id)
                    if inventory is None or workdir.source_fingerprint != inventory.fingerprint:
                        raise ValueError(f"Workdir {workdir.id} 的最终 inventory 不一致")
                    await workdir_repo.set_materialization_result(
                        workdir,
                        status="ready",
                        epoch_id=epoch_id,
                        source_fingerprint=inventory.fingerprint,
                    )
                conversations = list(
                    (
                        await db.scalars(
                            select(Conversation)
                            .where(Conversation.status != "deleted")
                            .order_by(Conversation.id)
                            .with_for_update()
                        )
                    ).all()
                )
                await _rewrite_persisted_project_paths(db, conversations)
                await control_repo.set_phase(
                    control,
                    phase="active",
                    epoch_id=epoch_id,
                    inventory_fingerprint=fingerprint,
                    activated_at=utc_now_naive(),
                )
                await db.commit()
                await _cleanup_legacy_file_storage(db)
            except Exception as exc:
                await db.rollback()
                control = await FileStorageMaterializationRepository(db).get(for_update=True)
                if control.phase != "active":
                    await FileStorageMaterializationRepository(db).set_phase(
                        control,
                        phase="error",
                        error_message=str(exc),
                    )
                    await db.commit()
                raise


async def require_project_workdir_active(db):
    """文件 producer/consumer 的统一 activation gate。"""
    control = await FileStorageMaterializationRepository(db).get()
    if control.phase != "active":
        raise FileStorageNotReadyError("Project Workdir 文件主链路尚未激活")
    return control


async def project_workdir_materialization_status(db) -> dict:
    """返回不包含路径与对象信息的物化状态。"""
    control = await FileStorageMaterializationRepository(db).get()
    return control.to_dict()
