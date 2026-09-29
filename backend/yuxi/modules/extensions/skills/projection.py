"""已授权 Skill 来源的路径校验与投影物化。"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shutil
import stat
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from yuxi.infrastructure.runtime_settings import get_skill_projection_dir
from yuxi.infrastructure.observability.logging import logger
from yuxi.infrastructure.filesystem import open_directory_fd, open_regular_file_fd


SKILL_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


SKILL_NAME_PATTERN = SKILL_SLUG_PATTERN


_USER_SKILLS_LOCK = threading.Lock()


_USER_SKILLS_LOCKS: dict[str, threading.Lock] = {}


def _get_user_skills_lock(uid: str) -> threading.Lock:
    with _USER_SKILLS_LOCK:
        lock = _USER_SKILLS_LOCKS.get(uid)
        if lock is None:
            lock = threading.Lock()
            _USER_SKILLS_LOCKS[uid] = lock
        return lock


@contextmanager
def _user_skills_file_lock(uid: str):
    """在共享投影卷上串行化同一用户的目录替换。"""
    from yuxi.modules.workspace.paths import workspace_uid_dirname

    lock_dir = get_skill_projection_dir() / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{workspace_uid_dirname(uid)}.lock"
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_valid_skill_slug(slug: str) -> bool:
    if not isinstance(slug, str):
        return False
    return bool(SKILL_SLUG_PATTERN.match(slug.strip()))


def get_user_skills_root_dir(uid: str) -> Path:
    """返回当前用户获授权的共享 Skill 只读投影根目录。"""
    from yuxi.modules.workspace.paths import workspace_uid_dirname

    safe_uid = workspace_uid_dirname(uid)
    root = get_skill_projection_dir() / safe_uid
    root.mkdir(parents=True, exist_ok=True)
    return root


async def sync_user_accessible_skills_async(
    uid: str,
    source_dirs: dict[str, str | Path],
) -> Path:
    """在线程池同步用户获授权的共享 Skill 投影，避免阻塞 Agent 事件循环。"""
    return await asyncio.to_thread(
        sync_user_accessible_skills,
        uid,
        source_dirs,
    )


def remove_skill_from_user_projection(uid: str, slug: str) -> None:
    """从一个已物化 uid 投影移除 Skill，授权变更时保持 fail-closed。"""
    if not is_valid_skill_slug(slug):
        raise ValueError("无效 skill slug")
    with _get_user_skills_lock(uid), _user_skills_file_lock(uid):
        _remove_skill_projection_entry(get_user_skills_root_dir(uid) / slug)


def sync_user_accessible_skills(
    uid: str,
    source_dirs: dict[str, str | Path],
) -> Path:
    """将用户有权访问的共享 Skill 来源同步到统一只读目录。"""
    user_skills_root = get_user_skills_root_dir(uid)
    normalized_sources = {
        slug: Path(os.path.abspath(os.fspath(path)))
        for slug, path in source_dirs.items()
        if is_valid_skill_slug(slug) and isinstance(path, (str, Path))
    }
    accessible_slugs = set(normalized_sources)
    with _get_user_skills_lock(uid), _user_skills_file_lock(uid):
        for entry in user_skills_root.iterdir():
            if entry.name in accessible_slugs:
                continue
            _remove_skill_projection_entry(entry)

        for slug, source_dir in normalized_sources.items():
            target_dir = user_skills_root / slug
            temp_target = user_skills_root / f".{slug}.tmp-{uuid.uuid4().hex[:8]}"
            try:
                if skill_dirs_equal(source_dir, target_dir):
                    continue
                copy_skill_tree_no_symlinks(source_dir, temp_target)
                _remove_skill_projection_entry(target_dir)
                temp_target.rename(target_dir)
            except FileNotFoundError:
                logger.warning(f"跳过不存在的 Skill 来源: slug={slug}")
                _remove_skill_projection_entry(target_dir)
            except (OSError, ValueError):
                _remove_skill_projection_entry(target_dir)
                raise
            finally:
                if temp_target.exists():
                    shutil.rmtree(temp_target, ignore_errors=True)

    return user_skills_root


def _remove_skill_projection_entry(path: Path) -> None:
    """删除一个投影条目，不跟随可能存在的符号链接。"""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def build_builtin_skill_dir_path(slug: str) -> str:
    return (Path("shared") / slug).as_posix()


def dir_contains_symlink(path: Path) -> bool:
    """检查目录内是否包含任意符号链接子路径。"""
    return any(child.is_symlink() for child in path.rglob("*"))


def copy_skill_tree_no_symlinks(source_dir: Path, target_dir: Path) -> None:
    """复制不含符号链接的 Skill 目录到 staging。"""
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)
    if dir_contains_symlink(source_dir):
        raise ValueError(f"Skill 来源只允许普通文件和目录: {source_dir}")
    try:
        shutil.copytree(source_dir, target_dir, symlinks=False)
    except BaseException:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def skill_dirs_equal(dir1: Path, dir2: Path) -> bool:
    """按 no-follow 字节与执行位比较来源和投影，非法来源显式失败。"""
    source_hash = _compute_projection_hash(dir1)
    try:
        return source_hash == _compute_projection_hash(dir2)
    except OSError:
        # 缺失或被替换为链接的投影必须重建，不能沿用相同字节的链接。
        return False


def _compute_projection_hash(path: Path) -> bytes:
    """通过目录 fd 读取投影比较摘要，拒绝链接和特殊文件。"""
    hasher = hashlib.sha256()

    def visit(directory_fd: int) -> None:
        """在已打开的目录内递归比较所需的类型、执行位和字节。"""
        for name in sorted(os.listdir(directory_fd)):
            hasher.update(os.fsencode(name) + b"\0")
            mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child_fd = open_directory_fd(directory_fd, (name,))
                try:
                    hasher.update(b"directory\0")
                    visit(child_fd)
                    hasher.update(b"end-directory\0")
                finally:
                    os.close(child_fd)
            else:
                with open_regular_file_fd(directory_fd, (name,)) as (file_fd, file_stat):
                    hasher.update(b"file\0" + bytes([stat.S_IMODE(file_stat.st_mode) & 0o111]))
                    content_hash = hashlib.sha256()
                    while chunk := os.read(file_fd, 1024 * 1024):
                        content_hash.update(chunk)
                    hasher.update(content_hash.digest())

    absolute = Path(os.path.abspath(path))
    directory_fd = open_directory_fd(Path(absolute.anchor), absolute.parts[1:])
    try:
        visit(directory_fd)
    finally:
        os.close(directory_fd)
    return hasher.digest()


def compute_dir_hash(source_dir: Path) -> str:
    hasher = hashlib.sha256()
    entries = sorted(source_dir.rglob("*"), key=lambda path: path.relative_to(source_dir).as_posix())
    for entry in entries:
        relative_path = entry.relative_to(source_dir).as_posix()
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        if entry.is_dir():
            hasher.update(b"directory\0")
            continue
        if not entry.is_file():
            hasher.update(b"other\0")
            continue
        hasher.update(b"file\0")
        hasher.update(bytes([stat.S_IMODE(entry.stat().st_mode) & 0o111]))
        with entry.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                hasher.update(chunk)
        hasher.update(b"\0")
    return hasher.hexdigest()


def replace_skill_target(
    target_dir: Path,
    source_dir: Path,
    *,
    validate: Callable[[Path], None] | None = None,
) -> None:
    """将 source_dir 原子地复制为 target_dir：先复制到临时目录，可选校验后再替换。"""
    temp_target = target_dir.with_name(f".{target_dir.name}.tmp-{uuid.uuid4().hex[:8]}")
    trash_dir: Path | None = None
    if temp_target.exists():
        shutil.rmtree(temp_target, ignore_errors=True)

    copy_skill_tree_no_symlinks(source_dir, temp_target)
    try:
        if validate is not None:
            validate(temp_target)
        if target_dir.exists():
            trash_dir = target_dir.with_name(f".{target_dir.name}.bak-{uuid.uuid4().hex[:8]}")
            target_dir.rename(trash_dir)
        temp_target.rename(target_dir)
    except Exception:
        shutil.rmtree(temp_target, ignore_errors=True)
        if trash_dir and trash_dir.exists() and not target_dir.exists():
            trash_dir.rename(target_dir)
        raise

    if trash_dir and trash_dir.exists():
        shutil.rmtree(trash_dir, ignore_errors=True)
