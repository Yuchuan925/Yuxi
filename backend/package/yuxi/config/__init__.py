import os
from pathlib import Path


def get_save_dir() -> Path:
    """读取当前进程的保存目录。"""
    return Path(os.getenv("SAVE_DIR", "saves"))


def __getattr__(name: str):
    """按需加载用户配置，避免轻量路径配置触发业务模型导入。"""
    if name in {"UserConfig", "UserConfigSchema"}:
        from .user import UserConfig, UserConfigSchema

        return {"UserConfig": UserConfig, "UserConfigSchema": UserConfigSchema}[name]
    raise AttributeError(name)


__all__ = ["UserConfig", "UserConfigSchema", "get_save_dir"]
