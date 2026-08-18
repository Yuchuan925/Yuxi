"""线程 outputs 快照的 hydrate、staging 与条件发布。"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend
from yuxi.repositories.thread_output_repository import ThreadOutputRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.storage.postgres.models_business import Conversation, ThreadOutputRevision
from yuxi.services.scoped_file_store import (
    await_blocking_file_call,
    replace_scope_with_local_tree,
    replace_scope_with_objects,
    scoped_relative_path,
)
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import OUTPUTS_DIR_NAME

MAX_OUTPUT_SNAPSHOT_FILES = 1000
MAX_OUTPUT_SNAPSHOT_BYTES = 100 * 1024 * 1024


def _output_object_name(thread_id: str, revision_id: str, relative_path: str) -> str:
    return f"threads/{thread_id}/outputs/revisions/{revision_id}/{relative_path}"


async def get_current_output_snapshot(*, conversation, db) -> tuple[str | None, list[dict]]:
    """读取线程当前已发布 outputs 快照。"""
    revision = await ThreadOutputRepository(db).get_current(conversation)
    if revision is None:
        if getattr(conversation, "current_output_revision_id", None):
            raise ValueError("outputs current revision 不可用")
        return None, []
    return revision.id, list(revision.files or [])


async def get_output_snapshot(*, conversation, revision_id: str, db) -> tuple[str, list[dict]]:
    """按线程作用域读取已发布或私有 checkpoint 快照。"""
    revision = await ThreadOutputRepository(db).get_snapshot(conversation, revision_id)
    if revision is None:
        raise ValueError("outputs revision 不可用于当前线程")
    return revision.id, list(revision.files or [])


async def get_user_output_snapshot(*, thread_id: str, uid: str, db) -> tuple[object, str | None, list[dict]]:
    """按线程授权读取当前 outputs 快照。"""
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
    if conversation is None or conversation.uid != str(uid) or conversation.status == "deleted":
        raise ValueError("对话线程不存在")
    revision_id, files = await get_current_output_snapshot(conversation=conversation, db=db)
    return conversation, revision_id, files


def find_output_descriptor(files: list[dict], path: str) -> dict | None:
    """按规范虚拟路径精确查找当前输出文件。"""
    relative_path = scoped_relative_path(OUTPUTS_DIR_NAME, path)
    normalized_path = f"/home/gem/user-data/outputs/{relative_path}"
    return next((item for item in files if item.get("path") == normalized_path), None)


def list_output_entries(
    files: list[dict],
    directory_path: str,
    *,
    modified_at: str = "",
    recursive: bool = False,
) -> list[dict]:
    """从平坦快照构造单层或递归目录列表。"""
    root = "/home/gem/user-data/outputs"
    normalized_directory = directory_path.rstrip("/")
    if normalized_directory != root and not normalized_directory.startswith(f"{root}/"):
        raise ValueError("outputs directory path is invalid")
    prefix = f"{normalized_directory}/"
    entries: dict[str, dict] = {}
    for item in files:
        path = str(item.get("path") or "")
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix) :]
        if not remainder:
            continue
        if recursive:
            parts = remainder.split("/")
            for index, directory_name in enumerate(parts[:-1]):
                directory_path_value = f"{prefix}{'/'.join(parts[: index + 1])}/"
                entries[directory_path_value] = {
                    "path": directory_path_value,
                    "name": directory_name,
                    "is_dir": True,
                    "size": 0,
                    "modified_at": modified_at,
                }
            entries[path] = {
                "path": path,
                "name": parts[-1],
                "is_dir": False,
                "size": int(item.get("size") or 0),
                "modified_at": modified_at,
            }
            continue
        name, separator, _rest = remainder.partition("/")
        child_path = f"{prefix}{name}"
        if separator:
            entries[name] = {
                "path": f"{child_path}/",
                "name": name,
                "is_dir": True,
                "size": 0,
                "modified_at": modified_at,
            }
        elif name not in entries:
            entries[name] = {
                "path": child_path,
                "name": name,
                "is_dir": False,
                "size": int(item.get("size") or 0),
                "modified_at": modified_at,
            }
    if recursive:
        return sorted(entries.values(), key=lambda item: str(item["path"]).lower())
    return sorted(entries.values(), key=lambda item: (not item["is_dir"], item["name"].lower()))


async def publish_output_manifest(
    *,
    db,
    conversation,
    base_revision_id: str | None,
    files: list[dict],
    run_id: str | None = None,
) -> str:
    """发布仅改变命名空间的 revision，复用已有不可变对象。"""
    revision_id = uuid.uuid4().hex
    repository = ThreadOutputRepository(db)
    await repository.create_staging(
        revision_id=revision_id,
        conversation=conversation,
        run_id=run_id,
        base_revision_id=base_revision_id,
    )
    await repository.set_files(revision_id, files)
    await repository.publish(revision_id)
    await db.commit()
    return revision_id


async def hydrate_thread_outputs_to_sandbox(
    *,
    runtime_thread_id: str,
    file_thread_id: str,
    uid: str,
    files: list[dict],
    sandbox_instance_id: str | None = None,
    create_if_missing: bool = True,
) -> None:
    """从当前已发布 revision 完整替换 sandbox outputs。"""
    backend = ProvisionerSandboxBackend(
        thread_id=runtime_thread_id,
        uid=uid,
        sandbox_instance_id=sandbox_instance_id,
        create_if_missing=create_if_missing,
    )
    await replace_scope_with_objects(
        backend=backend,
        scope=OUTPUTS_DIR_NAME,
        files=files,
        minio_client=get_minio_client(),
        max_files=MAX_OUTPUT_SNAPSHOT_FILES,
        max_bytes=MAX_OUTPUT_SNAPSHOT_BYTES,
    )


async def hydrate_legacy_thread_outputs_to_sandbox(
    *,
    runtime_thread_id: str,
    file_thread_id: str,
    uid: str,
    legacy_root: Path,
    sandbox_instance_id: str | None = None,
    create_if_missing: bool = True,
) -> None:
    """在首个持久 revision 前恢复待迁移的本地 outputs。"""
    backend = ProvisionerSandboxBackend(
        thread_id=runtime_thread_id,
        uid=uid,
        sandbox_instance_id=sandbox_instance_id,
        create_if_missing=create_if_missing,
    )
    await replace_scope_with_local_tree(
        backend=backend,
        scope=OUTPUTS_DIR_NAME,
        root=legacy_root,
        max_files=MAX_OUTPUT_SNAPSHOT_FILES,
        max_bytes=MAX_OUTPUT_SNAPSHOT_BYTES,
    )


async def stage_thread_outputs(
    *,
    runtime_thread_id: str,
    file_thread_id: str,
    uid: str,
    conversation_id: int,
    run_id: str | None,
    base_revision_id: str | None,
    sandbox_instance_id: str | None = None,
) -> str:
    """先记录 durable intent，再逐文件上传不可变 outputs 对象。"""
    revision_id = uuid.uuid4().hex
    async with pg_manager.get_async_session_context() as db:
        conversation = await db.get(Conversation, conversation_id)
        if conversation is None or conversation.uid != uid or conversation.thread_id != file_thread_id:
            raise ValueError("outputs publication scope 不存在")
        await ThreadOutputRepository(db).create_staging(
            revision_id=revision_id,
            conversation=conversation,
            run_id=run_id,
            base_revision_id=base_revision_id,
        )
        await db.commit()

    backend = ProvisionerSandboxBackend(
        thread_id=runtime_thread_id,
        uid=uid,
        create_if_missing=False,
        sandbox_instance_id=sandbox_instance_id,
    )
    minio_client = get_minio_client()
    files: list[dict] = []
    try:
        paths = await await_blocking_file_call(backend.list_output_files)
        if len(paths) > MAX_OUTPUT_SNAPSHOT_FILES:
            raise ValueError(f"outputs 文件数超过限制（最多 {MAX_OUTPUT_SNAPSHOT_FILES} 个）")
        total_size = 0
        for path in paths:
            relative_path = scoped_relative_path(OUTPUTS_DIR_NAME, path)
            object_name = _output_object_name(file_thread_id, revision_id, relative_path)
            temp_path = ""
            try:
                remaining_bytes = MAX_OUTPUT_SNAPSHOT_BYTES - total_size
                if remaining_bytes < 0:
                    raise ValueError(f"outputs 总大小超过限制（最多 {MAX_OUTPUT_SNAPSHOT_BYTES} bytes）")
                with tempfile.NamedTemporaryFile(prefix="yuxi-output-", delete=False) as temp_file:
                    temp_path = temp_file.name
                size = await await_blocking_file_call(
                    backend.download_output_file_to_path,
                    path,
                    temp_path,
                    remaining_bytes,
                )
                total_size += size
                hasher = hashlib.sha256()
                verified_size = 0
                with open(temp_path, "rb") as source:
                    while chunk := source.read(1024 * 1024):
                        verified_size += len(chunk)
                        hasher.update(chunk)
                if verified_size != size:
                    raise ValueError(f"output file changed during snapshot: {path}")
                await await_blocking_file_call(
                    minio_client.upload_file_from_path_streaming,
                    minio_client.KB_BUCKETS["documents"],
                    object_name,
                    temp_path,
                    mimetypes.guess_type(PurePosixPath(path).name)[0],
                )
                files.append(
                    {
                        "path": path,
                        "bucket_name": minio_client.KB_BUCKETS["documents"],
                        "object_name": object_name,
                        "size": size,
                        "sha256": hasher.hexdigest(),
                        "content_type": mimetypes.guess_type(PurePosixPath(path).name)[0],
                    }
                )
            finally:
                if temp_path:
                    with suppress(FileNotFoundError):
                        await asyncio.to_thread(os.unlink, temp_path)

        async with pg_manager.get_async_session_context() as db:
            await ThreadOutputRepository(db).set_files(revision_id, files)
            await db.commit()
        return revision_id
    except (Exception, asyncio.CancelledError) as exc:
        await mark_output_revision_status(revision_id, "failed", str(exc))
        raise


async def publish_staged_outputs(*, db, revision_id: str) -> None:
    """在调用方 owning transaction 中条件发布 staged revision。"""
    await ThreadOutputRepository(db).publish(revision_id)


async def mark_output_revision_status(revision_id: str, status: str, error_message: str | None) -> None:
    """独立事务记录发布失败或确认不明。"""
    try:
        async with pg_manager.get_async_session_context() as db:
            await ThreadOutputRepository(db).mark_status(revision_id, status, error_message)
            await db.commit()
    except Exception as exc:
        # staging intent 已提交；状态修正失败时仍保留可审计的未终结事实。
        logger.error("Failed to mark output revision %s as %s: %s", revision_id, status, exc)


async def discard_unreferenced_output_checkpoint(revision_id: str) -> None:
    """删除未被 Run 引用的私有 checkpoint 及其独占对象。"""
    async with pg_manager.get_async_session_context() as db:
        revision = await db.get(ThreadOutputRevision, revision_id)
        if revision is None:
            return
        if revision.status != "checkpoint":
            raise ValueError("只能丢弃私有 outputs checkpoint")
        referenced = await db.scalar(
            select(ThreadOutputRevision.id).where(ThreadOutputRevision.base_revision_id == revision_id).limit(1)
        )
        if referenced:
            raise ValueError("outputs checkpoint 已被 revision 引用")
        files = list(revision.files or [])

    minio_client = get_minio_client()
    for item in files:
        await minio_client.adelete_file(str(item["bucket_name"]), str(item["object_name"]))

    async with pg_manager.get_async_session_context() as db:
        revision = await db.get(ThreadOutputRevision, revision_id, with_for_update=True)
        if revision is None:
            return
        referenced = await db.scalar(
            select(ThreadOutputRevision.id).where(ThreadOutputRevision.base_revision_id == revision_id).limit(1)
        )
        if referenced:
            raise ValueError("outputs checkpoint 在清理期间被 revision 引用")
        await db.delete(revision)
        await db.commit()
