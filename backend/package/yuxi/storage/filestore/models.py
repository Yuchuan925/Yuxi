from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class FileStoreError(Exception):
    """表示文件存储操作失败。"""


@dataclass(frozen=True, slots=True)
class ObjectStat:
    """描述对象存储中的文件元数据。"""

    key: str
    size: int
    modified: datetime
    content_type: str | None


@dataclass(frozen=True, slots=True)
class StoredObject:
    """表示从文件存储读取的完整对象。"""

    key: str
    data: bytes
    size: int
    modified: datetime
    content_type: str | None
