"""公开 item 身份和快照复用 Message，索引由 Turn 锁串行分配。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.repositories.runs import AgentRunRepository
from yuxi.modules.agents.repositories.turn import AgentTurnRepository
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.shared.hashing import hash_id
from yuxi.shared.datetime import format_utc_datetime


class PublicItemRepository:
    """在 Thread→Turn→Run 锁内登记已经进入用户输出链路的 item。"""

    def __init__(self, db: AsyncSession):
        """使用调用方的 owning transaction。"""
        self.db = db

    async def save(
        self,
        *,
        run_id: str,
        worker_id: str,
        operation_id: str,
        role: str,
        key: str,
        item: dict,
        content_index: int | None = None,
        cancelled_partial: bool = False,
    ) -> dict:
        """首次分配身份和 output_index，后续只替换相同 item 的完整值。"""
        run = await AgentRunRepository(self.db).get_run(run_id)
        if run is None:
            raise ValueError("公开输出缺少 Run")
        conversation = await ConversationRepository(self.db).lock_conversation_by_thread_id(run.conversation_thread_id)
        if conversation is None or conversation.uid != run.uid or conversation.app_id != run.app_id:
            raise ValueError("公开输出 Thread 归属不一致")
        turn = await AgentTurnRepository(self.db).get_for_scope(
            turn_id=run.turn_id,
            thread_id=run.conversation_thread_id,
            uid=run.uid,
            app_id=run.app_id,
            for_update=True,
        )
        if turn is None or turn.current_run_id != run.id:
            raise ValueError("公开输出不属于当前 Turn")
        run_repo = AgentRunRepository(self.db)
        lock_output = run_repo.lock_cancel_snapshot if cancelled_partial else run_repo.lock_output_persistence
        await lock_output(run_id, worker_id=worker_id, conversation_thread_id=run.conversation_thread_id)
        message = await self.db.scalar(
            select(Message).where(
                Message.run_id == run_id,
                Message.operation_id == operation_id,
                Message.role == role,
            )
        )
        if message is None or message.turn_id != turn.id or message.conversation_id != conversation.id:
            raise ValueError("公开 item 缺少相同 Run 的审计来源")
        metadata = dict(message.extra_metadata or {})
        public = dict(metadata.get("public_items") or {})
        current = public.get(key)
        if cancelled_partial and (
            current is None
            or role != "assistant"
            or key != "message"
            or item.get("type") != "message"
            or item.get("status") != "incomplete"
        ):
            raise ValueError("取消快照只能更新已经公开的 message，不能新增输出或执行工具")
        if current is None:
            identity = hash_id("item_", f"{run.id}:{message.id}:{key}", length=64)
            output_index = turn.next_output_index
            turn.next_output_index += 1
        else:
            identity = current["id"]
            output_index = current["yuxi"]["output_index"]
        result = {
            **item,
            "id": identity,
            "turn_id": turn.id,
            "yuxi": {
                **{
                    field: item.get("yuxi", {})[field]
                    for field in (
                        "reasoning",
                        "content_blocks",
                        "source_content_index",
                        "completed_content_indices",
                        "completed_reasoning_indices",
                    )
                    if field in item.get("yuxi", {})
                },
                "run_id": run.id,
                "output_index": output_index,
                "message_id": message.id,
                "created_at": format_utc_datetime(message.created_at),
            },
        }
        if content_index is not None:
            result["yuxi"]["source_content_index"] = content_index
        if role == "tool":
            declaration = await self.declared_call(message)
            if declaration is None:
                raise ValueError("公开工具结果缺少已公开的调用声明")
            source_message, call = declaration
            result["yuxi"].update(call_item_id=call["id"], call_run_id=source_message.run_id)
            if item["status"] != "in_progress":
                source_metadata = dict(source_message.extra_metadata)
                source_items = dict(source_metadata["public_items"])
                source_items[f"call:{item['call_id']}"] = {**call, "status": item["status"]}
                source_message.extra_metadata = {**source_metadata, "public_items": source_items}
        public[key] = result
        message.extra_metadata = {**metadata, "public_items": public}
        await self.db.flush()
        return result

    async def declared_call(self, tool_message: Message) -> tuple[Message, dict] | None:
        """仅使用工具审计已校验的声明身份关联恢复前的公开调用。"""
        source_id = (tool_message.extra_metadata or {}).get("source_model_message_id")
        source = await self.db.get(Message, source_id) if source_id is not None else None
        if (
            source is None
            or source.conversation_id != tool_message.conversation_id
            or source.turn_id != tool_message.turn_id
        ):
            return None
        call = (source.extra_metadata or {}).get("public_items", {}).get(f"call:{tool_message.operation_id}")
        return (source, call) if call is not None else None

    async def resumed_call(self, *, run_id: str, call_id: str) -> dict:
        """返回当前工具执行明确绑定的公开调用，不按相邻 Run 猜测。"""
        message = await self.db.scalar(
            select(Message).where(Message.run_id == run_id, Message.role == "tool", Message.operation_id == call_id)
        )
        declaration = await self.declared_call(message) if message is not None else None
        if declaration is None:
            raise ValueError("恢复执行缺少公开调用声明")
        return declaration[1]

    async def list_items(
        self,
        *,
        thread_id: str,
        uid: str,
        app_id: str | None,
        turn_id: str | None = None,
    ) -> list[tuple[Message, AgentRun | None, str | None]]:
        """只读取当前用户和 APP 的 Message 与结果归属，不返回审计 DTO。"""
        from yuxi.modules.agents.models.threads import Conversation
        from yuxi.modules.agents.models.turns import AgentTurn

        statement = (
            select(Message, AgentRun, AgentTurn.result_run_id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .outerjoin(AgentRun, Message.run_id == AgentRun.id)
            .outerjoin(AgentTurn, Message.turn_id == AgentTurn.id)
            .where(Conversation.thread_id == thread_id, Conversation.uid == uid, Conversation.app_id == app_id)
            .options(selectinload(Message.tool_calls))
            .order_by(Message.id)
        )
        if turn_id is not None:
            statement = statement.where(Message.turn_id == turn_id)
        return list((await self.db.execute(statement)).all())
