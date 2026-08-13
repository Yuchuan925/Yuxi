from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .models import ObjectStat, StoredObject


class FileStore(Protocol):
    """定义逻辑 key 驱动的异步文件存储能力。"""

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectStat:
        """写入完整字节对象并返回元数据。"""
        ...

    async def read(self, key: str) -> StoredObject:
        """读取完整对象。"""
        ...

    def stream(self, key: str, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """按块流式读取对象。"""
        ...

    async def stat(self, key: str) -> ObjectStat:
        """读取对象元数据。"""
        ...

    async def list(self, prefix: str = "") -> list[ObjectStat]:
        """列出指定逻辑 key 前缀下的对象。"""
        ...

    async def delete(self, key: str) -> None:
        """幂等删除单个对象。"""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀下的对象并返回删除数量。"""
        ...
