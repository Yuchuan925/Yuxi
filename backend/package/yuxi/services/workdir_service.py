"""解析 Conversation 在当前 UserWorkspace 中的 Workdir 绑定。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from yuxi.agents.backends.sandbox.paths import ensure_bound_user_workdir, workdir_virtual_dir
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.workspace_filesystem import WorkspaceFilesystem


@dataclass(frozen=True, slots=True)
class WorkdirBinding:
    """一个用户可见 Conversation 的 Workdir 路径。"""

    conversation_id: int
    thread_id: str
    workdir_path: str
    virtual_path: str
    uid: str

    def create_file_backend(self) -> WorkspaceFilesystem:
        """创建 uid 级 UserWorkspace 文件访问边界。"""
        return WorkspaceFilesystem(self.uid)


async def resolve_workdir_binding(*, thread_id: str, uid: str, db) -> WorkdirBinding:
    """授权并解析线程的 UserWorkspace 相对 Workdir。"""
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
    if conversation is None or conversation.uid != str(uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    if not conversation.workdir_path:
        raise RuntimeError("Conversation 缺少 Workdir 路径")
    ensure_bound_user_workdir(str(uid), conversation.workdir_path)
    return WorkdirBinding(
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
        workdir_path=conversation.workdir_path,
        virtual_path=workdir_virtual_dir(conversation.workdir_path),
        uid=str(uid),
    )
