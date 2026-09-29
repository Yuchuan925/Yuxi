"""可见 Agent 的 Public 目录入口。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from server.routers.public_v1.agents.auth import PublicAgentContext, require_public_context
from server.utils.auth_middleware import get_db
from yuxi.services.agents.directory import get_public_agent, list_public_agents
from yuxi.storage.postgres.models_business import Agent

router = APIRouter(dependencies=[Depends(require_public_context)])


@router.get("/")
async def list_agents(
    context: PublicAgentContext = Depends(require_public_context), db: AsyncSession = Depends(get_db)
):
    """列出凭据所有者可见的主 Agent。"""
    agents = await list_public_agents(user=context.owner, db=db)
    return {"data": [_agent_response(agent) for agent in agents]}


@router.get("/{agent_id}")
async def retrieve_agent(
    agent_id: str,
    context: PublicAgentContext = Depends(require_public_context),
    db: AsyncSession = Depends(get_db),
):
    """按后端可见性读取主 Agent。"""
    agent = await get_public_agent(agent_id=agent_id, user=context.owner, db=db)
    return _agent_response(agent)


def _agent_response(agent: Agent) -> dict:
    """只输出公开目录字段。"""
    return {"id": agent.slug, "object": "agent", "name": agent.name, "description": agent.description}
