"""跨协议边界的文件输入与已授权文件结果。"""

from dataclasses import dataclass
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class FileInput:
    """借用已复位的文件流；调用方负责关闭，消费者不得保存或关闭它。"""

    filename: str
    source: BinaryIO


@dataclass(frozen=True, slots=True)
class PreparedFile:
    """描述已授权、需在响应完成后删除的临时文件。"""

    path: str
    media_type: str
    filename: str | None = None
    explicit_disposition: bool = False
