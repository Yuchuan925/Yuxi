from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from .models import FileStoreError

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def normalize_key(key: str, *, allow_empty: bool = False) -> str:
    """校验并规范对象 key，拒绝绝对路径和路径穿越。"""
    if not isinstance(key, str):
        raise FileStoreError("对象 key 必须是字符串")
    if "\\" in key:
        raise FileStoreError("对象 key 必须使用 POSIX 路径分隔符")
    if key.startswith("/"):
        raise FileStoreError("对象 key 不能是绝对路径")

    raw_parts = key.split("/")
    if not key and allow_empty:
        return ""
    if not key or any(part in {"", ".", ".."} for part in raw_parts):
        raise FileStoreError("对象 key 包含空段或路径穿越")

    return PurePosixPath(*raw_parts).as_posix()


def normalize_prefix(prefix: str) -> str:
    """规范对象前缀，允许空前缀和单个尾斜杠。"""
    if not prefix:
        return ""
    has_trailing_slash = prefix.endswith("/")
    normalized = normalize_key(prefix[:-1] if has_trailing_slash else prefix)
    return f"{normalized}/" if has_trailing_slash else normalized


def thread_upload_key(thread_id: str, path: str) -> str:
    """生成线程上传文件的逻辑 key。"""
    return _join_key("threads", thread_id, "uploads", path)


def thread_output_key(thread_id: str, path: str) -> str:
    """生成线程输出文件的逻辑 key。"""
    return _join_key("threads", thread_id, "outputs", path)


def user_workspace_key(uid: str, path: str) -> str:
    """生成用户工作区文件的逻辑 key。"""
    return _join_key("users", _safe_identifier(uid, field_name="uid"), "workspace", path)


def shared_skills_prefix() -> str:
    """返回共享 Skill 对象的统一目录前缀。"""
    return "skills/"


def shared_skill_key(slug: str, path: str) -> str:
    """生成共享 Skill 文件的逻辑 key。"""
    return _join_key("skills", _safe_identifier(slug, field_name="slug"), path)


def thread_skill_key(thread_id: str, path: str) -> str:
    """生成线程 Skill 文件的逻辑 key。"""
    return _join_key("threads", thread_id, "skills", path)


def _join_key(*parts: str) -> str:
    """拼接经过严格校验的 key 片段。"""
    return "/".join(normalize_key(part) for part in parts)


def _safe_identifier(value: str, *, field_name: str) -> str:
    """将外部标识转换为稳定且不会改变 key 层级的单段值。"""
    normalized = str(value or "").strip()
    if not normalized:
        raise FileStoreError(f"{field_name} 不能为空")
    if _SAFE_ID_RE.fullmatch(normalized):
        return normalized
    return f"id-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
