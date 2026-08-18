"""Project Workdir 持久身份 Repository。"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import FileStorageMaterialization, ProjectWorkdir

MATERIALIZATION_STATUSES = frozenset({"pending", "importing", "prepared", "ready", "error"})
FILE_STORAGE_MATERIALIZATION_ID = "project-workdir-v1"
FILE_STORAGE_PHASES = frozenset({"pending", "fenced", "preparing", "active", "error"})


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
        control_result = await self.db.execute(
            select(FileStorageMaterialization).where(
                FileStorageMaterialization.id == FILE_STORAGE_MATERIALIZATION_ID,
                FileStorageMaterialization.phase == "active",
            )
        )
        active_control = control_result.scalar_one_or_none()
        workdir = ProjectWorkdir(
            id=workdir_id,
            uid=str(uid),
            storage_key=project_workdir_storage_key(workdir_id),
            materialization_status="ready" if active_control else "pending",
            materialization_epoch_id=active_control.epoch_id if active_control else None,
            source_fingerprint=hashlib.sha256(b"").hexdigest() if active_control else None,
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

    async def list_all(self, *, for_update: bool = False) -> list[ProjectWorkdir]:
        """按稳定顺序读取全部 Workdir；物化事务可选择锁行。"""
        statement = select(ProjectWorkdir).order_by(ProjectWorkdir.id)
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement)
        return list(result.scalars().all())

    async def set_materialization_result(
        self,
        workdir: ProjectWorkdir,
        *,
        status: str,
        epoch_id: str,
        source_fingerprint: str | None = None,
        error_message: str | None = None,
    ) -> ProjectWorkdir:
        """记录一个 epoch 对单个 Workdir 的物化结果。"""
        await self.set_materialization_status(workdir, status=status, error_message=error_message)
        workdir.materialization_epoch_id = str(epoch_id)
        workdir.source_fingerprint = str(source_fingerprint) if source_fingerprint else None
        await self.db.flush()
        return workdir


class FileStorageMaterializationRepository:
    """拥有实时 Workdir 主链路的全局 fence 与 activation。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get(self, *, for_update: bool = False) -> FileStorageMaterialization:
        statement = select(FileStorageMaterialization).where(
            FileStorageMaterialization.id == FILE_STORAGE_MATERIALIZATION_ID
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement)
        control = result.scalar_one_or_none()
        if control is None:
            control = FileStorageMaterialization(id=FILE_STORAGE_MATERIALIZATION_ID, phase="pending")
            self.db.add(control)
            await self.db.flush()
        return control

    async def set_phase(
        self,
        control: FileStorageMaterialization,
        *,
        phase: str,
        epoch_id: str | None = None,
        inventory_fingerprint: str | None = None,
        error_message: str | None = None,
        activated_at: datetime | None = None,
    ) -> FileStorageMaterialization:
        """更新全局 fence 状态；调用方负责持有 control 行锁。"""
        normalized = str(phase or "").strip()
        if normalized not in FILE_STORAGE_PHASES:
            raise ValueError(f"无效的文件主链路物化阶段: {normalized}")
        control.phase = normalized
        if epoch_id is not None:
            control.epoch_id = str(epoch_id)
        if inventory_fingerprint is not None:
            control.inventory_fingerprint = str(inventory_fingerprint)
        control.error_message = str(error_message) if error_message else None
        if activated_at is not None:
            control.activated_at = activated_at
        await self.db.flush()
        return control
