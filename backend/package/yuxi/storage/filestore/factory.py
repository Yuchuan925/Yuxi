from __future__ import annotations

import os

from .base import FileStore
from .local import LocalFileStore
from .models import FileStoreError
from .s3 import S3FileStore

_file_store: FileStore | None = None


def get_file_store() -> FileStore:
    """惰性创建并返回进程内共享的文件存储实例。"""
    global _file_store
    if _file_store is not None:
        return _file_store

    backend = (os.getenv("FILESTORE_BACKEND") or "s3").strip().lower()
    if backend == "local":
        running_in_docker = os.getenv("RUNNING_IN_DOCKER", "").strip().lower() in {"1", "true", "yes"}
        allow_local = os.getenv("FILESTORE_ALLOW_LOCAL", "").strip().lower() in {"1", "true", "yes"}
        if running_in_docker and not allow_local:
            raise FileStoreError(
                "标准 Docker API/worker 拆分拓扑不支持 local FileStore；"
                "单进程本地测试需显式设置 FILESTORE_ALLOW_LOCAL=true"
            )
        store: FileStore = LocalFileStore(os.getenv("FILESTORE_LOCAL_ROOT") or "saves/filestore")
    elif backend == "s3":
        store = S3FileStore(
            bucket=_env("FILESTORE_S3_BUCKET", "MINIO_BUCKET") or "yuxi-filestore",
            endpoint_url=_env("FILESTORE_S3_ENDPOINT", "MINIO_URI"),
            access_key_id=_env("FILESTORE_S3_ACCESS_KEY", "MINIO_ACCESS_KEY"),
            secret_access_key=_env("FILESTORE_S3_SECRET_KEY", "MINIO_SECRET_KEY"),
            region_name=_env("FILESTORE_S3_REGION", "MINIO_REGION") or "us-east-1",
        )
    else:
        raise FileStoreError(f"不支持的文件存储后端: {backend}")

    _file_store = store
    return store


def reset_file_store() -> None:
    """清空进程内文件存储单例，仅供测试隔离使用。"""
    global _file_store
    _file_store = None


def _env(primary: str, fallback: str) -> str | None:
    """读取主配置，并在空值时回退兼容的 MinIO 配置。"""
    return (os.getenv(primary) or os.getenv(fallback) or "").strip() or None
