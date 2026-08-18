"""受限文件 scope 的对象描述符与 sandbox hydrate 边界。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

from yuxi.storage.minio.client import StorageError
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import OUTPUTS_DIR_NAME, UPLOADS_DIR_NAME, VIRTUAL_PATH_PREFIX

SUPPORTED_SCOPES = frozenset({UPLOADS_DIR_NAME, OUTPUTS_DIR_NAME})


def scoped_virtual_root(scope: str) -> str:
    """返回受支持 scope 的 sandbox 虚拟根。"""
    if scope not in SUPPORTED_SCOPES:
        raise ValueError(f"unsupported file scope: {scope}")
    return f"/{VIRTUAL_PATH_PREFIX.strip('/')}/{scope}"


def validate_scoped_virtual_path(scope: str, path: str) -> str:
    """规范化并限制用户文件路径到 owning scope。"""
    raw = str(path or "").strip()
    if "\\" in raw:
        raise ValueError("file path must use POSIX separators")
    pure = PurePosixPath(raw)
    if not pure.is_absolute() or ".." in pure.parts:
        raise ValueError("file path must be an absolute scoped path")
    normalized = str(pure)
    root = scoped_virtual_root(scope)
    if normalized == root or not normalized.startswith(f"{root}/"):
        raise ValueError(f"file path must be under {root}")
    return normalized


def scoped_relative_path(scope: str, path: str) -> str:
    """返回对象键使用的规范 scope 相对路径。"""
    normalized = validate_scoped_virtual_path(scope, path)
    return normalized[len(scoped_virtual_root(scope)) + 1 :]


async def await_blocking_file_call(function, *args, **kwargs):
    """取消时等待已启动的阻塞文件操作到达终点。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Cancelled scoped file operation also failed: {exc}")
        raise


async def replace_scope_with_objects(
    *,
    backend,
    scope: str,
    files: list[dict],
    minio_client,
    max_files: int,
    max_bytes: int,
    max_file_bytes: int | None = None,
    require_integrity: bool = True,
) -> None:
    """按持久快照逐文件重建 sandbox scope，失败时清空半成品。"""
    if len(files) > max_files:
        raise ValueError(f"scoped snapshot has more than {max_files} files")
    descriptors: list[tuple[str, str, str, int | None, str | None]] = []
    declared_total_size = 0
    for item in files:
        path = validate_scoped_virtual_path(scope, str(item.get("path") or ""))
        bucket_name = str(item.get("bucket_name") or "")
        object_name = str(item.get("object_name") or "")
        raw_size = item.get("size")
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else None
        digest = item.get("sha256") if isinstance(item.get("sha256"), str) else None
        if (
            not bucket_name
            or not object_name
            or (size is not None and size < 0)
            or (digest is not None and len(digest) != 64)
            or (require_integrity and (size is None or digest is None))
        ):
            raise ValueError("invalid scoped file descriptor")
        declared_total_size += size or 0
        if declared_total_size > max_bytes:
            raise ValueError(f"scoped snapshot exceeds {max_bytes} bytes")
        descriptors.append((path, bucket_name, object_name, size, digest))

    try:
        await await_blocking_file_call(backend.clear_scope_files, scope)
        hydrated_total_size = 0
        for path, bucket_name, object_name, size, digest in descriptors:
            temp_path = ""
            try:
                with tempfile.NamedTemporaryFile(prefix="yuxi-scope-", delete=False) as temp_file:
                    temp_path = temp_file.name
                response = await minio_client.adownload_response(bucket_name, object_name)
                actual_size = 0
                hasher = hashlib.sha256()
                try:
                    with open(temp_path, "wb") as target:
                        while chunk := await asyncio.to_thread(response.read, 1024 * 1024):
                            actual_size += len(chunk)
                            if size is not None and actual_size > size:
                                raise ValueError(f"scoped object exceeds declared size: {object_name}")
                            if max_file_bytes is not None and actual_size > max_file_bytes:
                                raise ValueError(f"scoped object exceeds file limit: {object_name}")
                            if actual_size > max_bytes - hydrated_total_size:
                                raise ValueError(f"scoped snapshot exceeds {max_bytes} bytes")
                            hasher.update(chunk)
                            target.write(chunk)
                finally:
                    response.close()
                    response.release_conn()
                if (size is not None and actual_size != size) or (digest is not None and hasher.hexdigest() != digest):
                    raise ValueError(f"scoped object checksum mismatch: {object_name}")
                await await_blocking_file_call(backend.upload_scope_file_from_path, scope, path, temp_path)
                hydrated_total_size += actual_size
            except StorageError as exc:
                raise FileNotFoundError(f"scoped object not found: {object_name}") from exc
            finally:
                if temp_path:
                    with suppress(FileNotFoundError):
                        await asyncio.to_thread(os.unlink, temp_path)
    except (Exception, asyncio.CancelledError):
        try:
            await await_blocking_file_call(backend.clear_scope_files, scope)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Failed to clear sandbox {scope} after hydrate error: {exc}")
        raise


def _replace_scope_with_local_tree_blocking(
    backend,
    scope: str,
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    """通过目录 fd 安全遍历 legacy 本地树并替换 sandbox scope。"""
    backend.clear_scope_files(scope)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return

    file_count = 0
    total_bytes = 0

    def copy_directory(directory_fd: int, relative_parts: tuple[str, ...]) -> None:
        nonlocal file_count, total_bytes
        for name in sorted(os.listdir(directory_fd)):
            item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(item_stat.st_mode):
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    copy_directory(child_fd, (*relative_parts, name))
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                raise ValueError(f"legacy {scope} contains a non-regular entry: {'/'.join((*relative_parts, name))}")

            file_count += 1
            if file_count > max_files:
                raise ValueError(f"legacy {scope} file count exceeds {max_files}")
            virtual_path = f"{scoped_virtual_root(scope)}/{'/'.join((*relative_parts, name))}"
            validate_scoped_virtual_path(scope, virtual_path)
            source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            temp_path = ""
            copied_bytes = 0
            try:
                opened_stat = os.fstat(source_fd)
                if not stat.S_ISREG(opened_stat.st_mode):
                    raise ValueError(f"legacy {scope} entry changed type during snapshot: {virtual_path}")
                with tempfile.NamedTemporaryFile(prefix="yuxi-legacy-scope-", delete=False) as target:
                    temp_path = target.name
                    while chunk := os.read(source_fd, 1024 * 1024):
                        copied_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            raise ValueError(f"legacy {scope} total size exceeds {max_bytes} bytes")
                        target.write(chunk)
                if copied_bytes != opened_stat.st_size:
                    raise ValueError(f"legacy {scope} entry changed during snapshot: {virtual_path}")
                backend.upload_scope_file_from_path(scope, virtual_path, temp_path)
            finally:
                os.close(source_fd)
                if temp_path:
                    with suppress(FileNotFoundError):
                        os.unlink(temp_path)

    try:
        copy_directory(root_fd, ())
    except Exception:
        backend.clear_scope_files(scope)
        raise
    finally:
        os.close(root_fd)


async def replace_scope_with_local_tree(
    *,
    backend,
    scope: str,
    root: Path,
    max_files: int,
    max_bytes: int,
) -> None:
    """从待迁移本地树完整恢复 sandbox scope，取消时等待清理终点。"""
    await await_blocking_file_call(
        _replace_scope_with_local_tree_blocking,
        backend,
        scope,
        root,
        max_files=max_files,
        max_bytes=max_bytes,
    )
