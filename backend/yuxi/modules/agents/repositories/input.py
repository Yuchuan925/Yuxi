"""Input 排队、消息成员与一次性消费事实。"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.models.inputs import AgentInput, AgentInputMessage, AgentInputReceipt
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.modules.agents.models.turns import AgentTurn
from yuxi.modules.agents.models.threads import Conversation
from yuxi.modules.agents.models.messages import Message
from yuxi.shared.datetime import utc_now_naive


class AgentInputRepository:
    """在 Thread 锁内维护 Input 的投递状态。"""

    def __init__(self, db: AsyncSession):
        """绑定调用方持有的事务。"""
        self.db = db

    async def create(
        self,
        *,
        input_id: str,
        thread_id: str,
        uid: str,
        app_id: str | None,
        agent_slug: str,
        kind: str,
        api_key_id: int | None = None,
        turn_id: str | None = None,
        input_payload: dict | None = None,
        source: str = "chat",
        channel: str = "web",
        external_id: str | None = None,
        origin_metadata: dict | None = None,
    ) -> AgentInput:
        """保存 follow-up 或固定目标 Turn 的 steer。"""
        if kind not in {"follow_up", "steer"} or (kind == "steer") != (turn_id is not None):
            raise ValueError("Input 种类与目标 Turn 不一致")
        input_item = AgentInput(
            id=input_id,
            conversation_thread_id=thread_id,
            uid=uid,
            app_id=app_id,
            api_key_id=api_key_id,
            agent_slug=agent_slug,
            kind=kind,
            status="pending",
            turn_id=turn_id,
            input_payload=input_payload or {},
            source=source,
            channel=channel,
            external_id=external_id,
            origin_metadata=origin_metadata or {},
        )
        self.db.add(input_item)
        await self.db.flush()
        return input_item

    async def get_for_scope(
        self, *, input_id: str, thread_id: str, uid: str, app_id: str | None, for_update: bool = False
    ) -> AgentInput | None:
        """按完整资源作用域读取或锁定 Input。"""
        statement = select(AgentInput).where(
            AgentInput.id == input_id,
            AgentInput.conversation_thread_id == thread_id,
            AgentInput.uid == uid,
            AgentInput.app_id == app_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement.execution_options(populate_existing=for_update))
        return result.scalar_one_or_none()

    async def get_pending_for_message(self, message_id: int) -> AgentInput | None:
        """查找附件消息所属的待消费 Input。"""
        result = await self.db.execute(
            select(AgentInput)
            .join(AgentInputMessage, AgentInputMessage.input_id == AgentInput.id)
            .where(AgentInputMessage.message_id == message_id, AgentInput.status == "pending")
        )
        return result.scalar_one_or_none()

    async def get_queue_head(
        self, *, thread_id: str, uid: str, app_id: str | None, for_update: bool = True
    ) -> AgentInput | None:
        """读取 follow-up FIFO 队头；调用方先锁 Thread。"""
        statement = (
            select(AgentInput)
            .where(
                AgentInput.conversation_thread_id == thread_id,
                AgentInput.uid == uid,
                AgentInput.app_id == app_id,
                AgentInput.kind == "follow_up",
                AgentInput.status == "pending",
            )
            .order_by(AgentInput.received_seq)
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement.execution_options(populate_existing=for_update))
        return result.scalar_one_or_none()

    async def get_pending_steer(
        self, *, thread_id: str, uid: str, app_id: str | None, turn_id: str, for_update: bool = True
    ) -> AgentInput | None:
        """读取并可锁定本轮唯一待消费 steer。"""
        statement = select(AgentInput).where(
            AgentInput.conversation_thread_id == thread_id,
            AgentInput.uid == uid,
            AgentInput.app_id == app_id,
            AgentInput.kind == "steer",
            AgentInput.status == "pending",
            AgentInput.turn_id == turn_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement.execution_options(populate_existing=for_update))
        return result.scalar_one_or_none()

    async def add_messages(self, *, input_id: str, receipt_id: str, message_ids: list[int]) -> None:
        """按原始事件顺序建立多消息成员关系。"""
        input_item = await self.db.get(AgentInput, input_id)
        receipt = await self.db.get(AgentInputReceipt, receipt_id)
        if input_item is None or input_item.status != "pending" or receipt is None or receipt.input_id != input_id:
            raise ValueError("只能向待消费 Input 追加所属 Receipt 的消息")
        if (receipt.uid, receipt.app_id, receipt.conversation_thread_id) != (
            input_item.uid,
            input_item.app_id,
            input_item.conversation_thread_id,
        ):
            raise ValueError("Receipt 与 Input 作用域不一致")
        messages = await self.db.execute(
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id.in_(message_ids), Conversation.thread_id == input_item.conversation_thread_id)
        )
        if len(set(messages.scalars())) != len(message_ids) or len(set(message_ids)) != len(message_ids):
            raise ValueError("消息不属于目标 Thread 或存在重复成员")
        self.db.add_all(
            AgentInputMessage(input_id=input_id, receipt_id=receipt_id, message_id=message_id, position=position)
            for position, message_id in enumerate(message_ids)
        )
        await self.db.flush()

    async def list_receipts(self, *, input_id: str, through_seq: int | None = None) -> list[AgentInputReceipt]:
        """按独立接收序号列出 Input 的原始事件。"""
        statement = select(AgentInputReceipt).where(AgentInputReceipt.input_id == input_id)
        if through_seq is not None:
            statement = statement.where(AgentInputReceipt.receive_seq <= through_seq)
        result = await self.db.execute(statement.order_by(AgentInputReceipt.receive_seq))
        return list(result.scalars())

    async def list_messages(self, input_id: str, through_seq: int | None = None) -> list[Message]:
        """按事件接收序号和事件内位置读取输入消息。"""
        statement = (
            select(Message)
            .join(AgentInputMessage, AgentInputMessage.message_id == Message.id)
            .join(AgentInputReceipt, AgentInputReceipt.id == AgentInputMessage.receipt_id)
            .where(AgentInputMessage.input_id == input_id)
        )
        if through_seq is not None:
            statement = statement.where(AgentInputReceipt.receive_seq <= through_seq)
        result = await self.db.execute(statement.order_by(AgentInputReceipt.receive_seq, AgentInputMessage.position))
        return list(result.scalars())

    async def list_messages_for_inputs(self, input_ids: list[str]) -> dict[str, list[Message]]:
        """一次查询读取多条 Input 的原始消息并保持接收顺序。"""
        messages_by_input: dict[str, list[Message]] = {input_id: [] for input_id in input_ids}
        if not messages_by_input:
            return messages_by_input
        result = await self.db.execute(
            select(AgentInputMessage.input_id, Message)
            .join(Message, Message.id == AgentInputMessage.message_id)
            .join(AgentInputReceipt, AgentInputReceipt.id == AgentInputMessage.receipt_id)
            .where(AgentInputMessage.input_id.in_(messages_by_input))
            .order_by(AgentInputMessage.input_id, AgentInputReceipt.receive_seq, AgentInputMessage.position)
        )
        for input_id, message in result:
            messages_by_input[input_id].append(message)
        return messages_by_input

    async def get_latest_receive_seq(self, input_id: str) -> int | None:
        """读取领取事务的批次截止序号。"""
        value = await self.db.scalar(
            select(func.max(AgentInputReceipt.receive_seq)).where(AgentInputReceipt.input_id == input_id)
        )
        return int(value) if value is not None else None

    async def consume(self, *, input_id: str, turn_id: str, run_id: str, cutoff_seq: int) -> AgentInput:
        """一次性固定输入的 Turn、Run 和已接收批次。"""
        input_item = await self.db.get(AgentInput, input_id, with_for_update=True)
        turn = await self.db.get(AgentTurn, turn_id)
        run = await self.db.get(AgentRun, run_id)
        if input_item is None or input_item.status != "pending":
            raise ValueError("Input 已领取或不存在")
        if turn is None or run is None or run.turn_id != turn_id or run.run_type == "subagent":
            raise ValueError("消费目标必须是本轮顶层 Run")
        if (input_item.uid, input_item.app_id, input_item.conversation_thread_id) != (
            turn.uid,
            turn.app_id,
            turn.conversation_thread_id,
        ) or run.conversation_thread_id != input_item.conversation_thread_id:
            raise ValueError("Input、Turn 与 Run 作用域不一致")
        if input_item.turn_id not in (None, turn_id) or run.input_id not in (None, input_id):
            raise ValueError("Input 或 Run 已绑定其他目标")
        latest_seq = await self.get_latest_receive_seq(input_id)
        if latest_seq is None or cutoff_seq != latest_seq:
            raise ValueError("Input 批次截止序号不是当前接收边界")

        input_item.status = "consumed"
        input_item.turn_id = turn_id
        input_item.consumed_run_id = run_id
        input_item.cutoff_seq = cutoff_seq
        input_item.consumed_at = utc_now_naive()
        run.input_id = input_id
        receipt_ids = select(AgentInputReceipt.id).where(
            AgentInputReceipt.input_id == input_id,
            AgentInputReceipt.receive_seq <= cutoff_seq,
        )
        message_ids = select(AgentInputMessage.message_id).where(AgentInputMessage.receipt_id.in_(receipt_ids))
        await self.db.execute(
            update(AgentInputReceipt)
            .where(AgentInputReceipt.id.in_(receipt_ids))
            .values(turn_id=turn_id, run_id=run_id)
        )
        await self.db.execute(
            update(Message).where(Message.id.in_(message_ids)).values(turn_id=turn_id, delivery_status="dispatched")
        )
        await self.db.flush()
        return input_item

    async def cancel(self, input_item: AgentInput) -> AgentInput:
        """取消仍未领取的 Input。"""
        if input_item.status != "pending":
            raise ValueError("只能取消待消费 Input")
        input_item.status = "cancelled"
        input_item.cancelled_at = utc_now_naive()
        message_ids = select(AgentInputMessage.message_id).where(AgentInputMessage.input_id == input_item.id)
        await self.db.execute(update(Message).where(Message.id.in_(message_ids)).values(delivery_status="cancelled"))
        await self.db.flush()
        return input_item

    async def cancel_pending_for_turn(self, *, turn_id: str) -> list[AgentInput]:
        """取消结束 Turn 未消费的 steer，不触碰后续 follow-up。"""
        result = await self.db.execute(
            select(AgentInput)
            .where(
                AgentInput.turn_id == turn_id,
                AgentInput.kind == "steer",
                AgentInput.status == "pending",
            )
            .with_for_update()
        )
        inputs = list(result.scalars())
        for input_item in inputs:
            input_item.status = "cancelled"
            input_item.cancelled_at = utc_now_naive()
        if inputs:
            message_ids = select(AgentInputMessage.message_id).where(
                AgentInputMessage.input_id.in_(input_item.id for input_item in inputs)
            )
            await self.db.execute(
                update(Message).where(Message.id.in_(message_ids)).values(delivery_status="cancelled")
            )
        await self.db.flush()
        return inputs

    async def list_pending_follow_ups(self, *, thread_id: str, uid: str, app_id: str | None) -> list[AgentInput]:
        """按 FIFO 顺序读取尚未建立 Turn 的输入。"""
        result = await self.db.execute(
            select(AgentInput)
            .where(
                AgentInput.conversation_thread_id == thread_id,
                AgentInput.uid == uid,
                AgentInput.app_id == app_id,
                AgentInput.kind == "follow_up",
                AgentInput.status == "pending",
            )
            .order_by(AgentInput.received_seq)
        )
        return list(result.scalars())
