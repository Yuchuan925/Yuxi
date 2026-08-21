import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_raw_prefix = os.getenv("SANDBOX_VIRTUAL_PATH_PREFIX")
VIRTUAL_PATH_PREFIX = (_raw_prefix.strip() if _raw_prefix else "/home/gem/user-data") or "/home/gem/user-data"
if not VIRTUAL_PATH_PREFIX.startswith("/"):
    VIRTUAL_PATH_PREFIX = f"/{VIRTUAL_PATH_PREFIX}"
WORKSPACE_DIR_NAME = "workspace"
WORKSPACE_AGENTS_DIR_NAME = "agents"
WORKSPACE_AGENT_CONTEXT_FILES = {
    "AGENTS.md": "# AGENTS\n\n以下是约束 Agent 行为的一些要求\n",
    "USER.md": "# USER\n\n以下是有关用户的一些信息\n",
    "MEMORY.md": "# MEMORY\n\n以下是 Agent 需要记住的一些信息\n",
}
UPLOADS_DIR_NAME = "uploads"
OUTPUTS_DIR_NAME = "outputs"
LARGE_TOOL_RESULTS_DIR_NAME = "large_tool_results"
CONVERSATION_HISTORY_DIR_NAME = "conversation_history"
VIRTUAL_SKILLS_PATH = "/home/gem/skills"

# Sandbox 直接把 UserWorkspace 映射到该根；宿主机布局中的 ``workspace``
# 只属于存储实现，不进入模型可见路径。
VIRTUAL_PATH_WORKSPACE = VIRTUAL_PATH_PREFIX
VIRTUAL_PERSONAL_SKILLS_PATH = (Path(VIRTUAL_PATH_PREFIX) / WORKSPACE_AGENTS_DIR_NAME / "skills").as_posix()

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_directory_fd(root: Path | int, parts: tuple[str, ...], *, create: bool = False) -> int:
    """从可信目录逐层 no-follow 打开路径，返回调用方负责关闭的 fd。

    ``parts`` 必须是已校验的单路径组件；传入 fd 时函数复制而不接管原 fd。
    """
    directory_fd = os.dup(root) if isinstance(root, int) else os.open(root, _DIRECTORY_OPEN_FLAGS)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    try:
                        item_stat = os.stat(part, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError:
                        raise exc
                    if stat.S_ISLNK(item_stat.st_mode):
                        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), part) from exc
                raise
            previous_fd = directory_fd
            directory_fd = child_fd
            os.close(previous_fd)
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


@contextmanager
def open_regular_file_fd(
    root: Path | int,
    parts: tuple[str, ...],
    *,
    writable: bool = False,
) -> Iterator[tuple[int, os.stat_result]]:
    """从可信根 no-follow 打开普通文件，并在同一 fd 上校验类型。"""
    if not parts:
        raise IsADirectoryError(str(root))
    try:
        parent_fd = open_directory_fd(root, parts[:-1])
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError("symlink paths are not allowed") from exc
        raise
    file_fd = None
    try:
        flags = (os.O_WRONLY if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PermissionError("symlink paths are not allowed") from exc
            raise
        file_stat = os.fstat(file_fd)
        if stat.S_ISDIR(file_stat.st_mode):
            raise IsADirectoryError(parts[-1])
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError("only regular files are allowed")
        yield file_fd, file_stat
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def workdir_runtime_paths(workdir_path: str) -> tuple[str, str]:
    """返回当前 Workdir 的大结果与对话历史目录。"""
    outputs = (Path(workdir_path) / OUTPUTS_DIR_NAME).as_posix()
    return (
        (Path(outputs) / LARGE_TOOL_RESULTS_DIR_NAME).as_posix(),
        (Path(outputs) / CONVERSATION_HISTORY_DIR_NAME).as_posix(),
    )


def ensure_within_root(path: Path, root: Path, *, error_message: str) -> Path:
    """确认真实路径位于指定根目录内，否则拒绝越界访问。"""
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(error_message) from None
    return path


__all__ = [
    "VIRTUAL_PATH_PREFIX",
    "WORKSPACE_DIR_NAME",
    "WORKSPACE_AGENTS_DIR_NAME",
    "WORKSPACE_AGENT_CONTEXT_FILES",
    "UPLOADS_DIR_NAME",
    "OUTPUTS_DIR_NAME",
    "LARGE_TOOL_RESULTS_DIR_NAME",
    "CONVERSATION_HISTORY_DIR_NAME",
    "VIRTUAL_PATH_WORKSPACE",
    "VIRTUAL_PERSONAL_SKILLS_PATH",
    "open_directory_fd",
    "open_regular_file_fd",
    "workdir_runtime_paths",
    "VIRTUAL_SKILLS_PATH",
    "ensure_within_root",
]
