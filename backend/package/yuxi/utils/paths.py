import os
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
    "workdir_runtime_paths",
    "VIRTUAL_SKILLS_PATH",
    "ensure_within_root",
]
