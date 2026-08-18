"""线程 outputs revision 持久化查询。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import Conversation, ThreadOutputRevision
from yuxi.utils.datetime_utils import utc_now_naive


class OutputRevisionConflictError(ValueError):
    """同一路径相对 base 被两个 revision 修改为不同内容。"""


def _manifest_by_path(files: list[dict]) -> dict[str, dict]:
    """将完整 manifest 转成唯一虚拟路径索引。"""
    result: dict[str, dict] = {}
    for item in files:
        path = str(item.get("path") or "")
        if not path or path in result:
            raise ValueError("outputs manifest 包含空路径或重复路径")
        result[path] = dict(item)
    return result


def _descriptor_content(item: dict | None) -> tuple | None:
    """返回与对象存储位置无关的文件内容身份。"""
    if item is None:
        return None
    return (
        str(item.get("sha256") or ""),
        int(item.get("size") or 0),
    )


def merge_output_manifests(*, base: list[dict], staged: list[dict], current: list[dict]) -> list[dict]:
    """按路径执行三方合并，仅拒绝同一路径的不同内容变更。"""
    base_by_path = _manifest_by_path(base)
    staged_by_path = _manifest_by_path(staged)
    current_by_path = _manifest_by_path(current)
    merged: list[dict] = []
    for path in sorted(base_by_path.keys() | staged_by_path.keys() | current_by_path.keys()):
        base_item = base_by_path.get(path)
        staged_item = staged_by_path.get(path)
        current_item = current_by_path.get(path)
        staged_changed = _descriptor_content(staged_item) != _descriptor_content(base_item)
        current_changed = _descriptor_content(current_item) != _descriptor_content(base_item)
        if staged_changed and current_changed:
            if _descriptor_content(staged_item) != _descriptor_content(current_item):
                raise OutputRevisionConflictError(f"outputs path 冲突: {path}")
            selected = current_item
        elif staged_changed:
            selected = staged_item
        else:
            selected = current_item
        if selected is not None:
            merged.append(selected)
    return merged


def apply_checkpoint_delta(
    *,
    checkpoint: list[dict],
    staged: list[dict],
    ancestor: list[dict],
    current: list[dict],
) -> list[dict]:
    """只把子 Run 相对私有 checkpoint 的改动应用到公开 current。"""
    checkpoint_by_path = _manifest_by_path(checkpoint)
    staged_by_path = _manifest_by_path(staged)
    ancestor_by_path = _manifest_by_path(ancestor)
    current_by_path = _manifest_by_path(current)
    merged: list[dict] = []
    paths = checkpoint_by_path.keys() | staged_by_path.keys() | ancestor_by_path.keys() | current_by_path.keys()
    for path in sorted(paths):
        checkpoint_item = checkpoint_by_path.get(path)
        staged_item = staged_by_path.get(path)
        ancestor_item = ancestor_by_path.get(path)
        current_item = current_by_path.get(path)
        child_changed = _descriptor_content(staged_item) != _descriptor_content(checkpoint_item)
        current_changed = _descriptor_content(current_item) != _descriptor_content(ancestor_item)
        if child_changed:
            if current_changed and _descriptor_content(staged_item) != _descriptor_content(current_item):
                raise OutputRevisionConflictError(f"outputs path 冲突: {path}")
            selected = staged_item
        else:
            selected = current_item
        if selected is not None:
            merged.append(selected)
    return merged


class ThreadOutputRepository:
    """维护线程当前 revision 与不可变发布记录。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get_current(self, conversation: Conversation) -> ThreadOutputRevision | None:
        """读取 Conversation 明确指向的当前已发布 revision。"""
        revision_id = getattr(conversation, "current_output_revision_id", None)
        if not revision_id:
            return None
        result = await self.db.execute(
            select(ThreadOutputRevision).where(
                ThreadOutputRevision.id == revision_id,
                ThreadOutputRevision.conversation_id == conversation.id,
                ThreadOutputRevision.status == "published",
            )
        )
        return result.scalar_one_or_none()

    async def get_snapshot(
        self,
        conversation: Conversation,
        revision_id: str,
        *,
        statuses: tuple[str, ...] = ("published", "checkpoint"),
    ) -> ThreadOutputRevision | None:
        """按 conversation 作用域读取可 hydrate 的不可变快照。"""
        result = await self.db.execute(
            select(ThreadOutputRevision).where(
                ThreadOutputRevision.id == revision_id,
                ThreadOutputRevision.conversation_id == conversation.id,
                ThreadOutputRevision.status.in_(statuses),
            )
        )
        return result.scalar_one_or_none()

    async def get_revision_for_run(
        self,
        conversation: Conversation,
        run_id: str,
        *,
        status: str,
        base_revision_id: str | None = None,
    ) -> ThreadOutputRevision | None:
        """按 Run、状态和可选基线读取 outputs revision。"""
        conditions = [
            ThreadOutputRevision.conversation_id == conversation.id,
            ThreadOutputRevision.run_id == run_id,
            ThreadOutputRevision.status == status,
        ]
        if base_revision_id is not None:
            conditions.append(ThreadOutputRevision.base_revision_id == base_revision_id)
        result = await self.db.execute(
            select(ThreadOutputRevision).where(*conditions).order_by(ThreadOutputRevision.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def create_staging(
        self,
        *,
        revision_id: str,
        conversation: Conversation,
        run_id: str | None,
        base_revision_id: str | None,
    ) -> ThreadOutputRevision:
        """在对象写入前记录 durable staging intent。"""
        revision = ThreadOutputRevision(
            id=revision_id,
            conversation_id=conversation.id,
            thread_id=conversation.thread_id,
            uid=conversation.uid,
            run_id=run_id,
            base_revision_id=base_revision_id,
            status="staging",
            files=[],
        )
        self.db.add(revision)
        await self.db.flush()
        return revision

    async def set_files(self, revision_id: str, files: list[dict]) -> ThreadOutputRevision:
        """保存 staging revision 的完整快照描述符。"""
        revision = await self.db.get(ThreadOutputRevision, revision_id, with_for_update=True)
        if revision is None or revision.status != "staging":
            raise ValueError("outputs revision 不处于 staging 状态")
        revision.files = files
        await self.db.flush()
        return revision

    async def publish(self, revision_id: str) -> ThreadOutputRevision:
        """锁定线程后发布；base 落后时三方合并不冲突的路径。"""
        revision = await self.db.get(ThreadOutputRevision, revision_id, with_for_update=True)
        if revision is None or revision.status != "staging":
            raise ValueError("outputs revision 不处于 staging 状态")
        result = await self.db.execute(
            select(Conversation)
            .where(Conversation.id == revision.conversation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise ValueError("outputs revision 所属线程不存在")
        base_revision = None
        if revision.base_revision_id:
            base_revision = await self.db.get(ThreadOutputRevision, revision.base_revision_id)

        if base_revision is not None and base_revision.status == "checkpoint":
            ancestor_files: list[dict] = []
            if base_revision.base_revision_id:
                ancestor_revision = await self.db.get(ThreadOutputRevision, base_revision.base_revision_id)
                if (
                    ancestor_revision is None
                    or ancestor_revision.conversation_id != revision.conversation_id
                    or ancestor_revision.status != "published"
                ):
                    raise ValueError("outputs checkpoint ancestor 不可用于合并")
                ancestor_files = list(ancestor_revision.files or [])
            current_files: list[dict] = []
            if conversation.current_output_revision_id:
                current_revision = await self.db.get(ThreadOutputRevision, conversation.current_output_revision_id)
                if (
                    current_revision is None
                    or current_revision.conversation_id != revision.conversation_id
                    or current_revision.status != "published"
                ):
                    raise ValueError("outputs current revision 不可用于合并")
                current_files = list(current_revision.files or [])
            published_files = apply_checkpoint_delta(
                checkpoint=list(base_revision.files or []),
                staged=list(revision.files or []),
                ancestor=ancestor_files,
                current=current_files,
            )
            projection = ThreadOutputRevision(
                id=uuid.uuid4().hex,
                conversation_id=revision.conversation_id,
                thread_id=revision.thread_id,
                uid=revision.uid,
                run_id=revision.run_id,
                base_revision_id=conversation.current_output_revision_id,
                status="published",
                files=published_files,
                published_at=utc_now_naive(),
            )
            revision.status = "checkpoint"
            revision.error_message = None
            conversation.current_output_revision_id = projection.id
            self.db.add(projection)
            await self.db.flush()
            return projection
        elif conversation.current_output_revision_id != revision.base_revision_id:
            base_files: list[dict] = []
            if revision.base_revision_id:
                if (
                    base_revision is None
                    or base_revision.conversation_id != revision.conversation_id
                    or base_revision.status != "published"
                ):
                    raise ValueError("outputs base revision 不可用于合并")
                base_files = list(base_revision.files or [])

            current_revision = await self.db.get(ThreadOutputRevision, conversation.current_output_revision_id)
            if (
                current_revision is None
                or current_revision.conversation_id != revision.conversation_id
                or current_revision.status != "published"
            ):
                raise ValueError("outputs current revision 不可用于合并")
            revision.files = merge_output_manifests(
                base=base_files,
                staged=list(revision.files or []),
                current=list(current_revision.files or []),
            )

        conversation.current_output_revision_id = revision.id
        revision.status = "published"
        revision.published_at = utc_now_naive()
        revision.error_message = None
        await self.db.flush()
        return revision

    async def set_checkpoint_files(self, revision_id: str, files: list[dict]) -> ThreadOutputRevision:
        """把同步后的描述符写回私有 checkpoint，作为跨重试幂等结果。"""
        revision = await self.db.get(ThreadOutputRevision, revision_id, with_for_update=True)
        if revision is None or revision.status != "checkpoint":
            raise ValueError("outputs revision 不是 checkpoint")
        revision.files = files
        await self.db.flush()
        return revision

    async def checkpoint(self, revision_id: str) -> ThreadOutputRevision:
        """将 staging revision 固化为私有快照，不推进 Conversation current。"""
        revision = await self.db.get(ThreadOutputRevision, revision_id, with_for_update=True)
        if revision is None or revision.status != "staging":
            raise ValueError("outputs revision 不处于 staging 状态")
        revision.status = "checkpoint"
        revision.error_message = None
        await self.db.flush()
        return revision

    async def mark_status(self, revision_id: str, status: str, error_message: str | None) -> None:
        """记录 staging 后的失败或确认不明事实。"""
        if status not in {"conflict", "failed", "unknown"}:
            raise ValueError("unsupported output revision status")
        revision = await self.db.get(ThreadOutputRevision, revision_id, with_for_update=True)
        if revision is None or revision.status in {"checkpoint", "published"}:
            return
        revision.status = status
        revision.error_message = (error_message or "")[:2000] or None
        await self.db.flush()
