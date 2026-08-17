import os
import tempfile
from pathlib import Path


def get_save_dir() -> Path:
    """读取当前进程的保存目录。"""
    return Path(os.getenv("SAVE_DIR", "saves"))


def get_runtime_dir() -> Path:
    """读取可丢弃日志与缓存使用的当前进程运行目录。"""
    configured = os.getenv("YUXI_RUNTIME_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / f"yuxi-runtime-{os.getpid()}"


def __getattr__(name: str):
    """按需加载用户配置，避免轻量路径配置触发业务模型导入。"""
    if name in {"UserConfig", "UserConfigSchema"}:
        from .user import UserConfig, UserConfigSchema

        return {"UserConfig": UserConfig, "UserConfigSchema": UserConfigSchema}[name]
    raise AttributeError(name)


__all__ = ["UserConfig", "UserConfigSchema", "get_runtime_dir", "get_save_dir"]
