"""解析 Conversation 的 Project Workdir 与共享 runtime 绑定。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend
from yuxi.agents.backends.sandbox.paths import project_workdir_virtual_dir
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.project_workdir_repository import ProjectWorkdirRepository
from yuxi.repositories.subagent_thread_repository import SubagentThreadRepository
from yuxi.services.project_workdir_materialization_service import require_project_workdir_active


@dataclass(frozen=True, slots=True)
class ProjectWorkdirBinding:
    """一个用户可见 Conversation 的实时文件与 runtime 绑定。"""

    conversation_id: int
    thread_id: str
    runtime_scope_id: str
    workdir_id: str
    workdir_path: str
    uid: str

    def create_file_backend(self, *, create_if_missing: bool = True) -> ProvisionerSandboxBackend:
        """创建只承载实时文件 API 的 Workdir 桥接 backend。"""
        file_scope_id = f"workdir-files-{self.workdir_id}"
        return ProvisionerSandboxBackend(
            thread_id=file_scope_id,
            uid=self.uid,
            sandbox_instance_id=file_scope_id,
            workdir_id=self.workdir_id,
            create_if_missing=create_if_missing,
        )


async def resolve_project_workdir_binding(
    *,
    thread_id: str,
    uid: str,
    db,
    require_active: bool = True,
) -> ProjectWorkdirBinding:
    """授权并解析线程的根 runtime 与 Project Workdir。"""
    active_control = await require_project_workdir_active(db) if require_active else None
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
    if conversation is None or conversation.uid != str(uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    if not conversation.workdir_id:
        raise RuntimeError("Conversation 缺少 Project Workdir")
    workdir = await ProjectWorkdirRepository(db).require_for_user(conversation.workdir_id, str(uid))
    if active_control and (
        workdir.materialization_status != "ready" or workdir.materialization_epoch_id != active_control.epoch_id
    ):
        raise RuntimeError("Project Workdir 未在当前 active epoch 准备完成")

    relation_repo = SubagentThreadRepository(db)
    conversation_repo = ConversationRepository(db)
    runtime_root = conversation
    visited_conversation_ids = {conversation.id}
    while relation := await relation_repo.get_by_child_conversation_for_user(runtime_root.id, str(uid)):
        parent = await conversation_repo.get_conversation_by_id(relation.parent_conversation_id)
        if parent is None or parent.uid != str(uid) or parent.status == "deleted":
            raise RuntimeError("子 Conversation 的根 runtime 不可用")
        if parent.workdir_id != conversation.workdir_id:
            raise RuntimeError("父子 Conversation 的 Project Workdir 绑定不一致")
        if parent.id in visited_conversation_ids:
            raise RuntimeError("子 Conversation 的 runtime 关系存在循环")
        visited_conversation_ids.add(parent.id)
        runtime_root = parent

    return ProjectWorkdirBinding(
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
        runtime_scope_id=runtime_root.thread_id,
        workdir_id=conversation.workdir_id,
        workdir_path=project_workdir_virtual_dir(conversation.workdir_id),
        uid=str(uid),
    )
