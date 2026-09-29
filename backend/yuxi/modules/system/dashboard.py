"""Dashboard 统计与监控业务用例服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.modules.system.repositories.dashboard import DashboardRepository


class DashboardService:
    """封装 Dashboard 统计读模型、会话查询与时间序列分析。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = DashboardRepository(db)
        self.conv_repo = ConversationRepository(db)

    async def get_basic_stats(self) -> dict[str, Any]:
        """读取基础统计指标（会话数、消息数、用户数）。"""
        return await self.repo.get_basic_stats()

    async def get_user_activity_stats(self, *, now: datetime | None = None) -> dict[str, Any]:
        """统计用户总量与活跃趋势。"""
        return await self.repo.get_user_activity_stats(now=now)

    async def get_tool_call_stats(self, *, now: datetime | None = None) -> dict[str, Any]:
        """统计工具调用总量、成功率与分布。"""
        return await self.repo.get_tool_call_stats(now=now)

    async def get_agent_analytics(self) -> dict[str, Any]:
        """汇总智能体对话与工具使用情况。"""
        return await self.repo.get_agent_analytics()

    async def get_call_timeseries(self, *, metric_type: str, time_range: str = "14days") -> dict[str, Any]:
        """查询调用分析时间序列。"""
        return await self.repo.get_call_timeseries(
            metric_type=metric_type,
            time_range=time_range,
        )

    async def get_thread_analytics(
        self,
        *,
        time_range: str = "30days",
        agent_id: str | None = None,
        include_subagents: bool = False,
    ) -> dict[str, Any]:
        """汇总会话（Thread）多维分析统计。"""
        return await self.repo.get_thread_analytics(
            time_range=time_range,
            agent_id=agent_id,
            include_subagents=include_subagents,
        )

    async def get_conversation_filter_options(self) -> dict[str, list[dict[str, Any]]]:
        """读取会话审计用户与 Agent 筛选项。"""
        return await self.repo.get_conversation_filter_options()

    async def list_conversations(
        self,
        *,
        uid: str | None = None,
        agent_id: str | None = None,
        status: str = "all",
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询并组装 Dashboard 对话列表。"""
        return await self.repo.list_conversations(
            uid=uid,
            agent_id=agent_id,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
        )

    async def get_conversation_detail(self, thread_id: str) -> dict[str, Any] | None:
        """获取指定会话完整消息流水与统计。"""
        conversation = await self.conv_repo.get_conversation_by_thread_id(thread_id)
        if not conversation:
            return None

        messages = await self.conv_repo.get_messages(conversation.id)
        stats = await self.conv_repo.get_stats(conversation.id)
        audit_metadata = await self.repo.get_conversation_audit_metadata(conversation)
        message_list = []
        for message in messages:
            message_data = {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "message_type": message.message_type,
                "created_at": format_utc_datetime(message.created_at) or "",
                "token_count": message.token_count,
            }
            if message.tool_calls:
                message_data["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "tool_name": tool_call.tool_name,
                        "tool_input": tool_call.tool_input,
                        "tool_output": tool_call.tool_output,
                        "status": tool_call.status,
                        "error_message": tool_call.error_message,
                    }
                    for tool_call in message.tool_calls
                ]
            message_list.append(message_data)

        return {
            "thread_id": conversation.thread_id,
            "uid": conversation.uid,
            "username": audit_metadata["username"],
            "user_avatar": audit_metadata["user_avatar"],
            "user_deleted": audit_metadata["user_deleted"],
            "agent_id": conversation.agent_id,
            "agent_name": audit_metadata["agent_name"],
            "agent_avatar": audit_metadata["agent_avatar"],
            "agent_deleted": audit_metadata["agent_deleted"],
            "title": conversation.title,
            "status": conversation.status,
            "is_pinned": bool(conversation.is_pinned),
            "message_count": stats.message_count if stats else len(message_list),
            "created_at": format_utc_datetime(conversation.created_at) or "",
            "updated_at": format_utc_datetime(conversation.updated_at) or "",
            **await self.repo.get_conversation_token_usage(conversation.id),
            "messages": message_list,
        }
