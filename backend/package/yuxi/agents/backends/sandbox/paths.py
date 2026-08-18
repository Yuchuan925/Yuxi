from __future__ import annotations

import hashlib
import re
from pathlib import Path

from yuxi.config import get_projects_dir, get_user_data_dir
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import (
    WORKSPACE_AGENT_CONTEXT_FILES,
    WORKSPACE_AGENTS_DIR_NAME,
    WORKSPACE_DIR_NAME,
    ensure_within_root,
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PROJECTS_VIRTUAL_ROOT = "/home/gem/projects"


def validate_thread_id(thread_id: str) -> str:
    value = str(thread_id or "").strip()
    if not value:
        raise ValueError("thread_id is required")
    if not _SAFE_ID_RE.match(value):
        raise ValueError("thread_id contains invalid characters")
    return value


def validate_workdir_id(workdir_id: str) -> str:
    """校验 Project Workdir 的单路径段身份。"""
    value = str(workdir_id or "").strip()
    if not value:
        raise ValueError("workdir_id is required")
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError("workdir_id contains invalid characters")
    return value


def project_workdir_virtual_dir(workdir_id: str) -> str:
    """返回 Sandbox 内稳定的 Project Workdir 根。"""
    safe_workdir_id = validate_workdir_id(workdir_id)
    return f"{PROJECTS_VIRTUAL_ROOT}/project-{safe_workdir_id}"


def project_workdir_host_dir(workdir_id: str) -> Path:
    """返回 Compose 持久卷内的 Project Workdir 根。"""
    safe_workdir_id = validate_workdir_id(workdir_id)
    projects_root = get_projects_dir().resolve(strict=False)
    target = (projects_root / safe_workdir_id).resolve(strict=False)
    return ensure_within_root(target, projects_root, error_message="workdir path resolved outside projects root")


def workspace_uid_dirname(uid: str) -> str:
    """Return a path-safe, stable workspace directory name for a logical UID.

    Database and OIDC subject identifiers may contain characters such as ``:``
    that are valid identity data but unsafe in filesystem path components.
    Legacy simple UIDs retain their directory name; all other values use a
    namespaced SHA-256 digest at the filesystem boundary only.
    """
    value = str(uid or "").strip()
    if not value:
        raise ValueError("uid is required")
    if _SAFE_ID_RE.fullmatch(value):
        return value
    return f"uid-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def global_user_data_dir(uid: str) -> Path:
    """Return the shared host-side directory used for one user's workspace files."""
    safe_uid = workspace_uid_dirname(uid)
    return get_user_data_dir() / "shared" / safe_uid


def user_workspace_dir(uid: str) -> Path:
    """返回用户级实时 Workspace 根。"""
    return global_user_data_dir(uid) / WORKSPACE_DIR_NAME


def user_workspace_agent_context_file(uid: str, filename: str) -> Path:
    """返回用户级 Agent 上下文文件。"""
    return user_workspace_dir(uid) / WORKSPACE_AGENTS_DIR_NAME / filename


def _user_data_root_dir() -> Path:
    return get_user_data_dir().resolve(strict=False)


def _resolve_user_data_child_path(path: Path) -> Path:
    root = _user_data_root_dir()
    resolved = path.resolve(strict=False)
    return ensure_within_root(resolved, root, error_message="path resolved outside user data root")


def _chmod_writable(path: Path, *, dir: bool = False) -> None:
    safe_path = _resolve_user_data_child_path(path)
    mode = 0o777 if dir else 0o666
    try:
        safe_path.chmod(mode)
    except OSError:
        pass


def ensure_workspace_default_files(workspace_dir: Path) -> None:
    workspace_dir = _resolve_user_data_child_path(workspace_dir)
    agents_dir = workspace_dir / WORKSPACE_AGENTS_DIR_NAME

    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        _chmod_writable(agents_dir, dir=True)
    except FileExistsError:
        logger.warning("工作区默认 Agents 目录创建失败：路径已被文件占用")
        return
    except OSError as exc:
        logger.warning(f"工作区默认 Agents 目录初始化失败: {exc}")
        return

    for filename, default_content in WORKSPACE_AGENT_CONTEXT_FILES.items():
        context_file = agents_dir / filename
        try:
            with context_file.open("x", encoding="utf-8") as buffer:
                buffer.write(default_content)
            _chmod_writable(context_file)
        except FileExistsError:
            if context_file.is_dir():
                logger.warning(f"工作区默认 {filename} 创建失败：路径已被目录占用")
        except OSError as exc:
            logger.warning(f"工作区默认 {filename} 初始化失败: {exc}")


def ensure_user_workspace(uid: str) -> None:
    """创建用户级 Workspace 与默认 Agent 上下文文件。"""
    _resolve_user_data_child_path(global_user_data_dir(uid)).mkdir(parents=True, exist_ok=True)
    workspace_dir = _resolve_user_data_child_path(user_workspace_dir(uid))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    ensure_workspace_default_files(workspace_dir)
