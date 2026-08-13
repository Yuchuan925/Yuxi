from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from .keys import normalize_key, normalize_prefix
from .models import FileStoreError, ObjectStat, StoredObject


class LocalFileStore:
    """在受控根目录内提供异步本地文件存储。"""

    def __init__(self, root: str | Path):
        """初始化本地存储根目录。"""
        self.root = Path(root).expanduser().resolve()
        self.metadata_root = self.root / ".filestore-metadata"
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_root.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectStat:
        """以临时文件加原子替换的方式写入完整对象。"""
        normalized = normalize_key(key)
        path = self._path(normalized)
        resolved_content_type = content_type or mimetypes.guess_type(normalized)[0] or "application/octet-stream"

        try:
            await asyncio.to_thread(self._atomic_write, path, data)
            await asyncio.to_thread(
                self._atomic_write,
                self._metadata_path(normalized),
                json.dumps({"content_type": resolved_content_type}).encode(),
            )
            return await self.stat(normalized)
        except OSError as exc:
            raise FileStoreError(f"写入本地对象失败: {normalized}") from exc

    async def read(self, key: str) -> StoredObject:
        """读取完整本地对象。"""
        normalized = normalize_key(key)
        try:
            data, stat = await asyncio.gather(
                asyncio.to_thread(self._path(normalized).read_bytes),
                self.stat(normalized),
            )
        except FileNotFoundError as exc:
            raise FileStoreError(f"对象不存在: {normalized}") from exc
        return StoredObject(
            key=stat.key,
            data=data,
            size=stat.size,
            modified=stat.modified,
            content_type=stat.content_type,
        )

    async def stream(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """按固定块大小流式读取本地对象。"""
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        normalized = normalize_key(key)
        try:
            file = await asyncio.to_thread(self._path(normalized).open, "rb")
        except FileNotFoundError as exc:
            raise FileStoreError(f"对象不存在: {normalized}") from exc

        try:
            while chunk := await asyncio.to_thread(file.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(file.close)

    async def stat(self, key: str) -> ObjectStat:
        """读取本地对象元数据。"""
        normalized = normalize_key(key)
        try:
            path = self._path(normalized)
            file_stat, is_file, content_type = await asyncio.gather(
                asyncio.to_thread(path.stat),
                asyncio.to_thread(path.is_file),
                asyncio.to_thread(self._read_content_type, normalized),
            )
        except FileNotFoundError as exc:
            raise FileStoreError(f"对象不存在: {normalized}") from exc
        if not is_file:
            raise FileStoreError(f"对象不存在: {normalized}")
        return ObjectStat(
            key=normalized,
            size=file_stat.st_size,
            modified=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
            content_type=content_type,
        )

    async def list(self, prefix: str = "") -> list[ObjectStat]:
        """按逻辑 key 前缀列出本地对象。"""
        normalized_prefix = normalize_prefix(prefix)
        return await asyncio.to_thread(self._list_sync, normalized_prefix)

    async def delete(self, key: str) -> None:
        """幂等删除本地对象及其元数据。"""
        normalized = normalize_key(key)
        await asyncio.to_thread(self._unlink_missing_ok, self._path(normalized))
        await asyncio.to_thread(self._unlink_missing_ok, self._metadata_path(normalized))

    async def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀下的全部本地对象。"""
        objects = await self.list(prefix)
        for item in objects:
            await self.delete(item.key)
        return len(objects)

    def _path(self, key: str) -> Path:
        """将逻辑 key 解析为根目录内的真实对象路径。"""
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path.is_relative_to(self.metadata_root):
            raise FileStoreError("对象 key 超出本地存储根目录")
        return path

    def _metadata_path(self, key: str) -> Path:
        """返回对象内容类型的本地元数据路径。"""
        return self.metadata_root / f"{key}.json"

    def _list_sync(self, prefix: str) -> list[ObjectStat]:
        """同步扫描根目录并生成指定前缀的对象列表。"""
        objects: list[ObjectStat] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_relative_to(self.metadata_root):
                continue
            key = path.relative_to(self.root).as_posix()
            if not key.startswith(prefix):
                continue
            file_stat = path.stat()
            objects.append(
                ObjectStat(
                    key=key,
                    size=file_stat.st_size,
                    modified=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
                    content_type=self._read_content_type(key),
                )
            )
        return sorted(objects, key=lambda item: item.key)

    def _read_content_type(self, key: str) -> str | None:
        """读取对象内容类型，不存在时按扩展名推断。"""
        try:
            metadata = json.loads(self._metadata_path(key).read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return mimetypes.guess_type(key)[0] or "application/octet-stream"
        return metadata.get("content_type")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """在目标目录内写临时文件并原子替换目标。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as temporary_file:
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _unlink_missing_ok(path: Path) -> None:
        """幂等删除单个本地文件。"""
        path.unlink(missing_ok=True)
