"""把旧 Project/Thread 文件一次性导入 UserWorkspace。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm.attributes import flag_modified

from yuxi.agents.backends.sandbox.paths import (
    ensure_user_workspace,
    user_workdir_host_dir,
    workdir_virtual_dir,
)
from yuxi.config import get_legacy_storage_dir
from yuxi.services.workspace_filesystem import WorkspaceFilesystem
from yuxi.storage.postgres.models_business import Conversation, Message, ToolCall
from yuxi.utils.paths import VIRTUAL_PATH_PREFIX

_SAFE_LEGACY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_LEGACY_ATTACHMENT_STORAGE_FIELDS = {
    "bucket_name",
    "original_object_name",
    "markdown_object_name",
    "minio_url",
}


@dataclass(frozen=True, slots=True)
class LegacyWorkdirBinding:
    """旧 Workdir 的文件身份与所属用户。"""

    workdir_id: str
    uid: str


@dataclass(frozen=True, slots=True)
class LegacyConversationBinding:
    """旧 Conversation 到 Workdir 的映射。"""

    thread_id: str
    uid: str
    workdir_id: str


async def read_legacy_bindings(db) -> tuple[tuple[LegacyWorkdirBinding, ...], tuple[LegacyConversationBinding, ...]]:
    """读取可重放的旧目录映射；新安装返回空集合。"""
    conversations_table = bool(await db.scalar(text("SELECT to_regclass('conversations') IS NOT NULL")))
    if not conversations_table:
        return (), ()
    project_table = bool(await db.scalar(text("SELECT to_regclass('project_workdirs') IS NOT NULL")))
    workdir_column = bool(
        await db.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'conversations' AND column_name = 'workdir_id')"
            )
        )
    )
    workdirs: list[LegacyWorkdirBinding] = []
    if project_table:
        phase_table = bool(await db.scalar(text("SELECT to_regclass('file_storage_materializations') IS NOT NULL")))
        phase = None
        if phase_table:
            phase = await db.scalar(
                text("SELECT phase FROM file_storage_materializations WHERE id = 'project-workdir-v1'")
            )
        rows = await db.execute(text("SELECT id, uid, materialization_status FROM project_workdirs ORDER BY id"))
        workdirs = []
        for row in rows:
            workdir_id = _safe_legacy_component(row.id, "Workdir ID")
            if row.materialization_status != "ready":
                raise RuntimeError(f"旧 Workdir 尚未完成物化: {workdir_id}")
            workdirs.append(LegacyWorkdirBinding(workdir_id, str(row.uid)))
        if workdirs and phase != "active":
            raise RuntimeError("旧文件存储尚未全局激活，拒绝切换到 UserWorkspace")
    conversations: list[LegacyConversationBinding] = []
    if workdir_column:
        rows = await db.execute(
            text(
                "SELECT thread_id, uid, workdir_id FROM conversations "
                "WHERE workdir_id IS NOT NULL ORDER BY id"
            )
        )
        conversations = [
            LegacyConversationBinding(
                _safe_legacy_component(row.thread_id, "Thread ID"),
                str(row.uid),
                _safe_legacy_component(row.workdir_id, "Workdir ID"),
            )
            for row in rows
        ]
    else:
        workdir_path_column = bool(
            await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'conversations' AND column_name = 'workdir_path')"
                )
            )
        )
        if workdir_path_column:
            rows = await db.execute(text("SELECT thread_id, uid, workdir_path FROM conversations ORDER BY id"))
            for row in rows:
                workdir_id = _current_workdir_id(row.workdir_path)
                if workdir_id is None:
                    continue
                project_source = Path(os.getenv("YUXI_LEGACY_PROJECTS_DIR", "legacy-projects")) / workdir_id
                if project_table or project_source.exists() or _legacy_thread_data_exists(str(row.thread_id)):
                    conversations.append(
                        LegacyConversationBinding(
                            _safe_legacy_component(row.thread_id, "Thread ID"),
                            str(row.uid),
                            workdir_id,
                        )
                    )
        else:
            subagent_table = bool(await db.scalar(text("SELECT to_regclass('subagent_threads') IS NOT NULL")))
            if subagent_table:
                rows = await db.execute(
                    text(
                        "SELECT child.thread_id, child.uid, "
                        "COALESCE(parent.thread_id, child.thread_id) AS owner_thread_id, "
                        "COALESCE(parent.uid, child.uid) AS owner_uid "
                        "FROM conversations AS child "
                        "LEFT JOIN subagent_threads AS relation ON relation.child_conversation_id = child.id "
                        "LEFT JOIN conversations AS parent ON parent.id = relation.parent_conversation_id "
                        "ORDER BY child.id"
                    )
                )
            else:
                rows = await db.execute(
                    text(
                        "SELECT thread_id, uid, thread_id AS owner_thread_id, uid AS owner_uid "
                        "FROM conversations ORDER BY id"
                    )
                )
            for row in rows:
                thread_id = _safe_legacy_component(row.thread_id, "Thread ID")
                if not _legacy_thread_data_exists(thread_id):
                    continue
                owner_uid = str(row.owner_uid)
                owner_thread_id = _safe_legacy_component(row.owner_thread_id, "Thread ID")
                workdir_id = f"legacy-{hashlib.md5(f'{owner_uid}:{owner_thread_id}'.encode()).hexdigest()}"
                conversations.append(LegacyConversationBinding(thread_id, str(row.uid), workdir_id))
    owners = {item.workdir_id: item.uid for item in workdirs}
    for item in conversations:
        owner = owners.setdefault(item.workdir_id, item.uid)
        if owner != item.uid:
            raise RuntimeError("旧 Workdir 被不同用户引用，拒绝迁移")
    return tuple(LegacyWorkdirBinding(workdir_id, uid) for workdir_id, uid in sorted(owners.items())), tuple(
        conversations
    )


def import_legacy_workdirs(
    workdirs: tuple[LegacyWorkdirBinding, ...],
    conversations: tuple[LegacyConversationBinding, ...],
) -> None:
    """原子导入旧目录；所有目标验证成功前保留旧源。"""
    legacy_projects = Path(os.getenv("YUXI_LEGACY_PROJECTS_DIR", "legacy-projects"))
    conversations_by_workdir: dict[str, list[LegacyConversationBinding]] = {}
    for conversation in conversations:
        conversations_by_workdir.setdefault(conversation.workdir_id, []).append(conversation)

    for binding in workdirs:
        _safe_legacy_component(binding.workdir_id, "Workdir ID")
        ensure_user_workspace(binding.uid)
        workspace_files = WorkspaceFilesystem(binding.uid)
        try:
            workspace_files.create_authorized_directory(VIRTUAL_PATH_PREFIX, "projects", root=VIRTUAL_PATH_PREFIX)
        except FileExistsError:
            pass
        projects_root = user_workdir_host_dir(binding.uid, "projects")
        target = projects_root / binding.workdir_id
        staging = projects_root / f".import-{binding.workdir_id}-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir():
                    raise RuntimeError(f"Workdir 迁移目标不是安全目录: {binding.workdir_id}")
                _merge_tree(target, staging)
            project_source = legacy_projects / binding.workdir_id
            if project_source.exists() or project_source.is_symlink():
                _merge_tree(project_source, staging)
            for conversation in conversations_by_workdir.get(binding.workdir_id, []):
                legacy_user_data = get_legacy_storage_dir() / "threads" / conversation.thread_id / "user-data"
                for namespace in ("uploads", "outputs"):
                    source = legacy_user_data / namespace
                    if source.exists() or source.is_symlink():
                        _merge_tree(source, staging / namespace)
            (staging / "uploads").mkdir(exist_ok=True)
            (staging / "outputs").mkdir(exist_ok=True)
            staged_manifest = _tree_manifest(staging)
            if target.exists() or target.is_symlink():
                if target.is_symlink() or _tree_manifest(target) != staged_manifest:
                    raise RuntimeError(f"Workdir 迁移目标冲突: {binding.workdir_id}")
                shutil.rmtree(staging)
            else:
                os.replace(staging, target)
            if _tree_manifest(target) != staged_manifest:
                raise RuntimeError(f"Workdir 迁移校验失败: {binding.workdir_id}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def cleanup_legacy_workdir_sources(
    workdirs: tuple[LegacyWorkdirBinding, ...],
    conversations: tuple[LegacyConversationBinding, ...],
) -> None:
    """仅在数据库与最终目录验证提交后删除已导入旧源。"""
    legacy_projects = Path(os.getenv("YUXI_LEGACY_PROJECTS_DIR", "legacy-projects"))
    for binding in workdirs:
        _safe_legacy_component(binding.workdir_id, "Workdir ID")
        source = legacy_projects / binding.workdir_id
        if source.is_symlink():
            raise RuntimeError("旧 Project 来源变成 symlink，拒绝清理")
        if source.is_dir():
            shutil.rmtree(source)
    for conversation in conversations:
        legacy_user_data = get_legacy_storage_dir() / "threads" / conversation.thread_id / "user-data"
        for namespace in ("uploads", "outputs"):
            shutil.rmtree(legacy_user_data / namespace, ignore_errors=True)


def _legacy_thread_data_exists(thread_id: str) -> bool:
    root = get_legacy_storage_dir() / "threads" / thread_id / "user-data"
    return any((root / namespace).exists() or (root / namespace).is_symlink() for namespace in ("uploads", "outputs"))


def _current_workdir_id(workdir_path: object) -> str | None:
    if not isinstance(workdir_path, str) or not workdir_path.startswith("projects/"):
        return None
    relative = workdir_path.removeprefix("projects/")
    if "/" in relative:
        return None
    return _safe_legacy_component(relative, "Workdir ID")


async def rewrite_legacy_workdir_paths(db) -> None:
    """把仍被运行时读取的旧虚拟路径改写到当前 Workdir。"""
    result = await db.execute(select(Conversation).order_by(Conversation.id))
    conversations = list(result.scalars().all())
    by_id = {conversation.id: conversation for conversation in conversations}
    for conversation in conversations:
        metadata = dict(conversation.extra_metadata or {})
        attachments = metadata.get("attachments")
        if isinstance(attachments, list):
            virtual_workdir = workdir_virtual_dir(conversation.workdir_path)
            metadata["attachments"] = [
                _rewrite_attachment(conversation.thread_id, virtual_workdir, item)
                if isinstance(item, dict)
                else item
                for item in attachments
            ]
            conversation.extra_metadata = metadata
            flag_modified(conversation, "extra_metadata")

    if not by_id:
        return
    rows = await db.execute(
        select(ToolCall, Message.conversation_id)
        .join(Message, Message.id == ToolCall.message_id)
        .where(Message.conversation_id.in_(list(by_id)), ToolCall.tool_name == "present_artifacts")
    )
    for tool_call, conversation_id in rows.all():
        conversation = by_id[conversation_id]
        tool_input = dict(tool_call.tool_input or {})
        filepaths = tool_input.get("filepaths")
        if isinstance(filepaths, list):
            virtual_workdir = workdir_virtual_dir(conversation.workdir_path)
            tool_input["filepaths"] = [_rewrite_path(path, virtual_workdir) for path in filepaths]
            tool_call.tool_input = tool_input
    await db.flush()


async def verify_workdir_bindings(db) -> None:
    """回读 Conversation 行与最终目录，确认 schema 和文件一致。"""
    result = await db.execute(select(Conversation).where(Conversation.status != "deleted"))
    for conversation in result.scalars():
        expected_prefix = "projects/"
        if not conversation.workdir_path.startswith(expected_prefix):
            continue
        try:
            user_workdir_host_dir(conversation.uid, conversation.workdir_path)
        except (OSError, ValueError):
            raise RuntimeError(f"Conversation {conversation.thread_id} 的 Workdir 未完成迁移")


def _rewrite_attachment(thread_id: str, workdir_path: str, record: dict) -> dict:
    rewritten = {key: value for key, value in record.items() if key not in _LEGACY_ATTACHMENT_STORAGE_FIELDS}
    for field in ("path", "original_path", "file_path"):
        rewritten[field] = _rewrite_path(rewritten.get(field), workdir_path)
    if isinstance(rewritten.get("path"), str):
        rewritten["artifact_url"] = f"/api/chat/thread/{thread_id}/artifacts/{rewritten['path'].lstrip('/')}"
    if isinstance(rewritten.get("original_path"), str):
        rewritten["original_artifact_url"] = (
            f"/api/chat/thread/{thread_id}/artifacts/{rewritten['original_path'].lstrip('/')}"
        )
    return rewritten


def _rewrite_path(path: object, workdir_path: str) -> object:
    if not isinstance(path, str):
        return path
    relative_workdir = workdir_path.removeprefix("/home/gem/user-data/")
    workdir_id = relative_workdir.split("/", 1)[-1]
    old_project = f"/home/gem/projects/project-{workdir_id}"
    if path == old_project or path.startswith(f"{old_project}/"):
        return f"{workdir_path}{path[len(old_project):]}"
    old_workspace = "/home/gem/user-data/workspace"
    if path == old_workspace or path.startswith(f"{old_workspace}/"):
        return f"/home/gem/user-data{path[len(old_workspace):]}"
    for namespace in ("uploads", "outputs"):
        old_root = f"/home/gem/user-data/{namespace}"
        if path == old_root or path.startswith(f"{old_root}/"):
            return f"{workdir_path}{path[len('/home/gem/user-data'):]}"
    return path


def _merge_tree(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"旧 Workdir 来源不是安全目录: {source.name}")
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.is_symlink():
            raise RuntimeError(f"旧 Workdir 包含 symlink: {entry.name}")
        destination = target / entry.name
        if entry.is_dir():
            _merge_tree(entry, destination)
        elif entry.is_file():
            if destination.exists():
                if not destination.is_file() or _file_digest(destination) != _file_digest(entry):
                    raise RuntimeError(f"旧 Workdir 文件冲突: {entry.name}")
            else:
                shutil.copy2(entry, destination)
        else:
            raise RuntimeError(f"旧 Workdir 包含非常规文件: {entry.name}")


def _tree_manifest(root: Path) -> tuple[tuple[str, str], ...]:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Workdir manifest 根必须是真实目录")
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Workdir manifest 拒绝 symlink")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative + "/", ""))
        elif path.is_file():
            entries.append((relative, _file_digest(path)))
        else:
            raise RuntimeError("Workdir manifest 拒绝非常规文件")
    return tuple(entries)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as buffer:
        while chunk := buffer.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_legacy_component(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_LEGACY_ID_RE.fullmatch(normalized):
        raise RuntimeError(f"旧 {label} 包含不安全路径字符")
    return normalized
