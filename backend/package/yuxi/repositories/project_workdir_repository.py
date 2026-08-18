"""Project Workdir 持久身份 Repository。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ProjectWorkdir

MATERIALIZATION_STATUSES = frozenset({"pending", "importing", "prepared", "ready", "error"})


def project_workdir_storage_key(workdir_id: str) -> str:
    """生成不包含用户身份的稳定存储键。"""
    value = str(workdir_id or "").strip()
    if not value:
        raise ValueError("workdir_id 不能为空")
    return f"projects/{value}"


class ProjectWorkdirRepository:
    """管理 Project Workdir 身份和物化状态。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def create_default(self, *, uid: str) -> ProjectWorkdir:
        """为一个顶层 Conversation 创建默认独立 Workdir。"""
        workdir_id = str(uuid.uuid4())
        workdir = ProjectWorkdir(
            id=workdir_id,
            uid=str(uid),
            storage_key=project_workdir_storage_key(workdir_id),
            materialization_status="pending",
        )
        self.db.add(workdir)
        await self.db.flush()
        return workdir

    async def get_for_user(self, workdir_id: str, uid: str) -> ProjectWorkdir | None:
        result = await self.db.execute(
            select(ProjectWorkdir).where(
                ProjectWorkdir.id == workdir_id,
                ProjectWorkdir.uid == str(uid),
            )
        )
        return result.scalar_one_or_none()

    async def require_for_user(self, workdir_id: str, uid: str) -> ProjectWorkdir:
        """读取用户可见 Workdir，不允许跨用户绑定。"""
        workdir = await self.get_for_user(workdir_id, uid)
        if workdir is None:
            raise ValueError("Project Workdir 不存在或不属于当前用户")
        return workdir

    async def set_materialization_status(
        self,
        workdir: ProjectWorkdir,
        *,
        status: str,
        error_message: str | None = None,
    ) -> ProjectWorkdir:
        """记录物化状态；4R-B 的 epoch Owner 再约束跨记录转换。"""
        normalized = str(status or "").strip()
        if normalized not in MATERIALIZATION_STATUSES:
            raise ValueError(f"无效的 Workdir 物化状态: {normalized}")
        workdir.materialization_status = normalized
        workdir.materialization_error = str(error_message) if error_message else None
        await self.db.flush()
        return workdir
