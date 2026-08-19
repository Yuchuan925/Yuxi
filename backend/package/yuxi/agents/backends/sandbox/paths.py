from __future__ import annotations

import errno
import hashlib
import os
import re
import uuid
from pathlib import Path, PurePosixPath

from yuxi.config import get_user_data_dir
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import (
    VIRTUAL_PATH_PREFIX,
    WORKSPACE_AGENT_CONTEXT_FILES,
    WORKSPACE_AGENTS_DIR_NAME,
    WORKSPACE_DIR_NAME,
    ensure_within_root,
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
WORKDIR_PROJECTS_DIR_NAME = "projects"
RESERVED_WORKDIR_ROOTS = frozenset({WORKSPACE_AGENTS_DIR_NAME})


def validate_thread_id(thread_id: str) -> str:
    value = str(thread_id or "").strip()
    if not value:
        raise ValueError("thread_id is required")
    if not _SAFE_ID_RE.match(value):
        raise ValueError("thread_id contains invalid characters")
    return value


def normalize_workdir_path(workdir_path: str) -> str:
    """校验并规范化 UserWorkspace 相对 Workdir 路径。"""
    raw = str(workdir_path or "").strip()
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or "\\" in raw or "://" in raw:
        raise ValueError("workdir_path must be a relative POSIX path")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ValueError("workdir_path contains invalid path components")
    if not pure.parts or pure.parts[0] in RESERVED_WORKDIR_ROOTS:
        raise ValueError("workdir_path uses a reserved workspace directory")
    return pure.as_posix()


def workdir_virtual_dir(workdir_path: str) -> str:
    """返回 Workdir 在 Sandbox 内的绝对路径。"""
    return f"{VIRTUAL_PATH_PREFIX.rstrip('/')}/{normalize_workdir_path(workdir_path)}"


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


def user_workdir_host_dir(uid: str, workdir_path: str) -> Path:
    """解析当前用户 Workdir，拒绝任意 symlink 路径组件。"""
    normalized = normalize_workdir_path(workdir_path)
    workspace = user_workspace_dir(uid)
    target = workspace.joinpath(*PurePosixPath(normalized).parts)
    _open_workspace_directory(uid, PurePosixPath(normalized).parts)
    if not target.is_dir():
        raise ValueError("workdir_path does not reference an existing directory")
    return target


def create_default_user_workdir(uid: str) -> tuple[str, Path]:
    """在 UserWorkspace/projects 中创建唯一默认 Workdir。"""
    ensure_user_workspace(uid)
    workspace_fd = _open_user_workspace_fd(uid)
    projects_fd = None
    try:
        try:
            os.mkdir(WORKDIR_PROJECTS_DIR_NAME, 0o777, dir_fd=workspace_fd)
        except FileExistsError:
            pass
        projects_fd = os.open(
            WORKDIR_PROJECTS_DIR_NAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=workspace_fd,
        )
        while True:
            directory_name = str(uuid.uuid4())
            try:
                os.mkdir(directory_name, 0o777, dir_fd=projects_fd)
                break
            except FileExistsError:
                continue
    finally:
        if projects_fd is not None:
            os.close(projects_fd)
        os.close(workspace_fd)
    relative_path = f"{WORKDIR_PROJECTS_DIR_NAME}/{directory_name}"
    return relative_path, user_workspace_dir(uid) / WORKDIR_PROJECTS_DIR_NAME / directory_name


def allocate_default_user_workdir_path() -> str:
    """分配默认 Workdir 相对路径，不在数据库事务提交前创建目录。"""
    return f"{WORKDIR_PROJECTS_DIR_NAME}/{uuid.uuid4()}"


def ensure_bound_user_workdir(uid: str, workdir_path: str) -> Path:
    """确保已由 Conversation 绑定的默认 Workdir 存在；显式路径只校验。"""
    try:
        return user_workdir_host_dir(uid, workdir_path)
    except FileNotFoundError:
        pass
    normalized = normalize_workdir_path(workdir_path)
    parts = PurePosixPath(normalized).parts
    if len(parts) != 2 or parts[0] != WORKDIR_PROJECTS_DIR_NAME:
        raise FileNotFoundError("explicit workdir_path does not exist")
    try:
        uuid.UUID(parts[1])
    except ValueError as exc:
        raise FileNotFoundError("migrated Workdir directory does not exist") from exc
    ensure_user_workspace(uid)
    workspace_fd = _open_user_workspace_fd(uid)
    projects_fd = None
    try:
        try:
            os.mkdir(WORKDIR_PROJECTS_DIR_NAME, 0o777, dir_fd=workspace_fd)
        except FileExistsError:
            pass
        projects_fd = os.open(
            WORKDIR_PROJECTS_DIR_NAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=workspace_fd,
        )
        try:
            os.mkdir(parts[1], 0o777, dir_fd=projects_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(parts[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=projects_fd)
        os.close(directory_fd)
    finally:
        if projects_fd is not None:
            os.close(projects_fd)
        os.close(workspace_fd)
    return user_workspace_dir(uid) / WORKDIR_PROJECTS_DIR_NAME / parts[1]


def _open_workspace_directory(uid: str, parts: tuple[str, ...]) -> None:
    """逐层以 O_NOFOLLOW 打开 UserWorkspace 目录。"""
    directory_fd = _open_user_workspace_fd(uid)
    try:
        for part in parts:
            try:
                child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("UserWorkspace 路径包含符号链接或非目录组件") from exc
                raise
            os.close(directory_fd)
            directory_fd = child_fd
    finally:
        os.close(directory_fd)


def _open_user_workspace_fd(uid: str, *, create: bool = False) -> int:
    """从配置根逐层打开 uid 的 Workspace，拒绝中间 symlink。"""
    root = get_user_data_dir()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ("shared", workspace_uid_dirname(uid), WORKSPACE_DIR_NAME):
            if create:
                try:
                    os.mkdir(part, 0o777, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("UserWorkspace 路径包含符号链接或非目录组件") from exc
                raise
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _ensure_workspace_default_files_fd(workspace_fd: int) -> None:
    """通过已校验的 Workspace fd 初始化 Agent 上下文文件。"""
    try:
        os.mkdir(WORKSPACE_AGENTS_DIR_NAME, 0o777, dir_fd=workspace_fd)
    except FileExistsError:
        pass
    try:
        agents_fd = os.open(
            WORKSPACE_AGENTS_DIR_NAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=workspace_fd,
        )
    except OSError as exc:
        logger.warning(f"工作区默认 Agents 目录初始化失败: {exc}")
        return
    try:
        for filename, default_content in WORKSPACE_AGENT_CONTEXT_FILES.items():
            try:
                file_fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o666,
                    dir_fd=agents_fd,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                logger.warning(f"工作区默认 {filename} 初始化失败: {exc}")
                continue
            try:
                content = default_content.encode("utf-8")
                offset = 0
                while offset < len(content):
                    offset += os.write(file_fd, content[offset:])
            finally:
                os.close(file_fd)
    finally:
        os.close(agents_fd)


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
    workspace_fd = _open_user_workspace_fd(uid, create=True)
    try:
        _ensure_workspace_default_files_fd(workspace_fd)
    finally:
        os.close(workspace_fd)
