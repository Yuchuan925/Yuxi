"""当前用户 UserWorkspace 的 no-follow 文件访问边界。"""

from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path, PurePosixPath

from yuxi.agents.backends.sandbox.backend import FileTransferLimitError
from yuxi.agents.backends.sandbox.paths import user_workspace_dir, workspace_uid_dirname
from yuxi.config import get_skill_projection_dir
from yuxi.utils.paths import VIRTUAL_PATH_PREFIX, VIRTUAL_SKILLS_PATH, open_directory_fd


class WorkspaceFilesystem:
    """以 uid 为边界直接访问 UserWorkspace 与只读 Skill projection。"""

    def __init__(self, uid: str):
        self.uid = str(uid)
        self.workspace_root = user_workspace_dir(self.uid)
        self.skills_root = get_skill_projection_dir() / workspace_uid_dirname(self.uid)

    def ensure_available(self) -> None:
        """确认 UserWorkspace 根是未经过 symlink 的真实目录。"""
        directory_fd = os.open(self.workspace_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.close(directory_fd)

    def list_authorized_directory(self, path: str, *, root: str) -> list[dict]:
        """列出 Workdir 内的普通文件与真实目录。"""
        self._require_within(path, root)
        base, parts = self._resolve_virtual_path(path, writable=False)
        directory_fd = self._open_directory(base, parts)
        try:
            entries = []
            for name in sorted(os.listdir(directory_fd), key=str.lower):
                item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not (stat.S_ISDIR(item_stat.st_mode) or stat.S_ISREG(item_stat.st_mode)):
                    continue
                entries.append(
                    {
                        "name": name,
                        "is_dir": stat.S_ISDIR(item_stat.st_mode),
                        "size": 0 if stat.S_ISDIR(item_stat.st_mode) else item_stat.st_size,
                        "modified_at": item_stat.st_mtime,
                    }
                )
            return entries
        finally:
            os.close(directory_fd)

    def download_authorized_file_to_path(self, path: str, target_path: str, max_bytes: int) -> int:
        """把授权普通文件有界复制到服务临时文件。"""
        if max_bytes < 0:
            raise ValueError("file download limit must be non-negative")
        base, parts = self._resolve_virtual_path(path, writable=False)
        if not parts:
            raise IsADirectoryError(path)
        parent_fd = self._open_directory(base, parts[:-1])
        source_fd = target_fd = None
        try:
            try:
                source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PermissionError("symlink paths are not allowed") from exc
                raise
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise IsADirectoryError(path) if stat.S_ISDIR(source_stat.st_mode) else PermissionError(path)
            target_fd = os.open(target_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            total = 0
            while chunk := os.read(source_fd, 1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise FileTransferLimitError("file exceeds transfer limit")
                self._write_all(target_fd, chunk)
            return total
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if source_fd is not None:
                os.close(source_fd)
            os.close(parent_fd)

    def upload_authorized_file_from_path(self, path: str, source_path: str) -> None:
        """从受信任服务临时文件原子写入 UserWorkspace。"""
        base, parts = self._resolve_virtual_path(path, writable=True)
        if not parts:
            raise IsADirectoryError(path)
        parent_fd = self._open_directory(base, parts[:-1], create=True)
        source_fd = target_fd = None
        temp_name = f".yuxi-write-{uuid.uuid4().hex}"
        try:
            source_fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise ValueError("upload source is not a regular file")
            target_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o666,
                dir_fd=parent_fd,
            )
            while chunk := os.read(source_fd, 1024 * 1024):
                self._write_all(target_fd, chunk)
            os.close(target_fd)
            target_fd = None
            os.rename(temp_name, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if source_fd is not None:
                os.close(source_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def create_authorized_directory(self, parent_path: str, name: str, *, root: str) -> str:
        """在 Workdir 内创建一个单层目录。"""
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("directory name must be one path component")
        self._require_within(parent_path, root)
        base, parts = self._resolve_virtual_path(parent_path, writable=True)
        parent_fd = self._open_directory(base, parts)
        try:
            os.mkdir(name, 0o777, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return f"{parent_path.rstrip('/')}/{name}"

    def delete_authorized_path(self, path: str, *, root: str) -> None:
        """递归删除 Workdir 内的真实文件或目录，不允许删除根。"""
        self._require_within(path, root, allow_root=False)
        base, parts = self._resolve_virtual_path(path, writable=True)
        parent_fd = self._open_directory(base, parts[:-1])
        try:
            self._remove_entry(parent_fd, parts[-1])
        finally:
            os.close(parent_fd)

    def regular_file_exists(self, path: str) -> bool:
        """确认路径是授权根内未经过 symlink 的普通文件。"""
        try:
            base, parts = self._resolve_virtual_path(path, writable=False)
            if not parts:
                return False
            parent_fd = self._open_directory(base, parts[:-1])
            try:
                item_stat = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                return stat.S_ISREG(item_stat.st_mode)
            finally:
                os.close(parent_fd)
        except (FileNotFoundError, NotADirectoryError, PermissionError, ValueError):
            return False

    def _resolve_virtual_path(self, path: str, *, writable: bool) -> tuple[Path, tuple[str, ...]]:
        raw = str(path or "").strip()
        pure = PurePosixPath(raw)
        if not raw or not pure.is_absolute() or ".." in pure.parts or "\\" in raw:
            raise ValueError("invalid virtual path")
        normalized = pure.as_posix()
        workspace_prefix = VIRTUAL_PATH_PREFIX.rstrip("/")
        skills_prefix = VIRTUAL_SKILLS_PATH.rstrip("/")
        if normalized == workspace_prefix:
            return self.workspace_root, ()
        if normalized.startswith(f"{workspace_prefix}/"):
            return self.workspace_root, tuple(PurePosixPath(normalized[len(workspace_prefix) + 1 :]).parts)
        if not writable and normalized.startswith(f"{skills_prefix}/"):
            return self.skills_root, tuple(PurePosixPath(normalized[len(skills_prefix) + 1 :]).parts)
        raise PermissionError("path is outside the current UserWorkspace")

    @staticmethod
    def _require_within(path: str, root: str, *, allow_root: bool = True) -> None:
        normalized_path = PurePosixPath(str(path)).as_posix()
        normalized_root = PurePosixPath(str(root)).as_posix().rstrip("/")
        if normalized_path == normalized_root:
            if allow_root:
                return
            raise ValueError("operation cannot target the Workdir root")
        if not normalized_path.startswith(f"{normalized_root}/"):
            raise ValueError("path is outside the Workdir")

    @staticmethod
    def _open_directory(base: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
        try:
            return open_directory_fd(base, parts, create=create)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PermissionError("symlink paths are not allowed") from exc
            raise

    @classmethod
    def _remove_entry(cls, parent_fd: int, name: str) -> None:
        item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(item_stat.st_mode):
            raise PermissionError("symlink paths are not allowed")
        if not stat.S_ISDIR(item_stat.st_mode):
            if not stat.S_ISREG(item_stat.st_mode):
                raise PermissionError("only regular files and directories can be deleted")
            os.unlink(name, dir_fd=parent_fd)
            return
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for child_name in os.listdir(child_fd):
                cls._remove_entry(child_fd, child_name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)

    @staticmethod
    def _write_all(file_fd: int, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            offset += os.write(file_fd, content[offset:])
