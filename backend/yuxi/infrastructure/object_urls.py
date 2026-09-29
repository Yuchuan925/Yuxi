"""迁移后的职责模块。"""

from yuxi.infrastructure.observability.logging import logger


def is_minio_url(file_path: str) -> bool:
    """检测是否是本系统生成的 MinIO 存储 URL。"""
    from urllib.parse import urlparse

    parsed_url = urlparse(file_path)
    if parsed_url.scheme == "minio":
        return bool(parsed_url.netloc and parsed_url.path.lstrip("/"))

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return False

    path_parts = parsed_url.path.lstrip("/").split("/", 1)
    if len(path_parts) != 2:
        return False

    from yuxi.infrastructure.minio.client import MinIOClient

    known_buckets = set(MinIOClient.KB_BUCKETS.values()) | MinIOClient.PUBLIC_READ_BUCKETS
    return path_parts[0] in known_buckets


def parse_minio_url(file_path: str) -> tuple[str, str]:
    """
    解析MinIO URL，提取bucket名称和对象名称

    支持标准 HTTP/HTTPS URL 格式：
    - http(s)://host/bucket-name/path/to/object

    Args:
        file_path: MinIO文件URL (http:// 或 https://)

    Returns:
        tuple[str, str]: (bucket_name, object_name)

    Raises:
        ValueError: 如果无法解析URL
    """
    try:
        from urllib.parse import unquote, urlparse

        # 解析URL
        parsed_url = urlparse(file_path)

        # 对于 minio:// 协议，bucket名称在netloc中
        if parsed_url.scheme == "minio":
            bucket_name = parsed_url.netloc
            object_name = unquote(parsed_url.path.lstrip("/"))
        else:
            # 对于 http/https 协议，bucket名称在path的第一部分
            object_name = parsed_url.path.lstrip("/")
            path_parts = object_name.split("/", 1)
            if len(path_parts) > 1:
                bucket_name = path_parts[0]
                object_name = unquote(path_parts[1])
            else:
                raise ValueError(f"无法解析MinIO URL中的bucket名称: {file_path}")

        logger.debug(f"Parsed MinIO URL: bucket_name={bucket_name}, object_name={object_name}")
        return bucket_name, object_name

    except Exception as e:
        logger.error(f"Failed to parse MinIO URL {file_path}: {e}")
        raise ValueError(f"无法解析MinIO URL: {file_path}")
