"""Public Agent 目录与终端用户身份解析。"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.repositories.definitions import AgentRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.agents.models.definitions import Agent
from yuxi.modules.identity.models import APIKey, User

DEFAULT_END_USER_ID = "__default__"


async def resolve_public_user(*, owner: User, api_key: APIKey, end_user_id: str | None, db: AsyncSession) -> User:
    """在 Public 边界把缺省或显式外部身份解析为 APP 独立用户。"""
    identity = DEFAULT_END_USER_ID if end_user_id is None else end_user_id
    if not identity or identity != identity.strip() or len(identity) > 128:
        raise HTTPException(status_code=422, detail="X-End-User-Id 必须为 1 至 128 个无首尾空白的字符")

    user = await UserRepository(db).get_or_create_public_end_user(
        owner=owner, app_id=api_key.app_id, end_user_id=identity
    )
    if user.is_deleted:
        raise HTTPException(status_code=403, detail="终端用户已停用")
    await db.commit()
    return user


async def list_public_agents(*, user: User, db: AsyncSession) -> list[Agent]:
    """列出当前身份可调用的主 Agent。"""
    return await AgentRepository(db).list_visible(user=user)


async def get_public_agent(*, agent_id: str, user: User, db: AsyncSession) -> Agent:
    """按后端可见性读取主 Agent。"""
    agent = await AgentRepository(db).get_visible_by_slug(slug=agent_id, user=user, kind="main")
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return agent
