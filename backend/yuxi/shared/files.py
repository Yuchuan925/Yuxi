"""业务层交付给 HTTP 层的临时文件结果。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PreparedFile:
    """描述已授权、需在响应完成后删除的临时文件。"""

    path: str
    media_type: str
    filename: str | None = None
    explicit_disposition: bool = False
