"""
MinIO 存储模块
简化的对象存储功能
"""

# 导出核心功能
from yuxi.infrastructure.minio.client import (
    MinIOClient,
    ObjectSizeLimitError,
    StorageError,
    UploadResult,
    aupload_file_to_minio,
    get_minio_client,
)
from yuxi.infrastructure.minio.utils import generate_unique_filename, get_file_size, upload_image_to_minio

# 导出常用函数
__all__ = [
    # 核心功能
    "MinIOClient",
    "get_minio_client",
    "aupload_file_to_minio",
    # 异常类
    "ObjectSizeLimitError",
    "StorageError",
    "UploadResult",
    # 工具函数
    "get_file_size",
    "generate_unique_filename",
    "upload_image_to_minio",
]
