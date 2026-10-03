from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.modules.agents.runtime.agent_backends import (
    AgentBackendNotFoundError,
    get_agent_backend,
    list_agent_backend_info,
)
from yuxi.modules.agents.runtime.context import filter_declared_config
from yuxi.modules.agents.repositories.definitions import (
    AgentRepository,
    is_builtin_agent,
    user_can_access_agent,
    user_can_manage_agent,
)
from yuxi.modules.agents.services.configuration import prepare_agent_config_write
from yuxi.modules.identity.models import User

from yuxi.api.dependencies.auth import get_superadmin_user, get_db, get_required_user

agent_router = APIRouter(prefix="/agent", tags=["agent"])


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visibility: Literal["private", "shared"] = "private"
    name: str
    backend_id: str = "ChatbotAgent"
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    pics: list[str] | None = None
    config_json: dict | None = None
    share_config: dict | None = None
    is_subagent: bool | None = None
    set_default: bool = False


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    pics: list[str] | None = None
    config_json: dict | None = None
    share_config: dict | None = None
    is_subagent: bool | None = None


def _filter_agent_config_json(backend_id: str, config_json: dict | None) -> dict:
    backend = get_agent_backend(backend_id)
    context_schema = backend.context_schema
    return filter_declared_config(config_json or {}, context_schema=context_schema)


async def _serialize_agent(
    repo: AgentRepository,
    item,
    user: User,
    *,
    include_configurable_items: bool = False,
    backend_info_cache: dict[tuple[str, bool, str], dict] | None = None,
) -> dict:
    try:
        data = await repo.serialize(
            item,
            user=user,
            include_configurable_items=include_configurable_items,
            backend_info_cache=backend_info_cache,
        )
        data["config_json"] = _filter_agent_config_json(item.backend_id, data.get("config_json"))
    except AgentBackendNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return data


@agent_router.get("/backends")
async def list_agent_backends(current_user: User = Depends(get_required_user)):
    infos = await list_agent_backend_info()
    return {
        "backends": [
            {
                **info,
                "type": "agent_backend",
                "can_create": current_user.role in {"admin", "superadmin"} or info["backend_id"] != "SubAgentBackend",
            }
            for info in infos
        ]
    }


@agent_router.get("/backends/{backend_id}")
async def get_agent_backend_detail(
    backend_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        backend = get_agent_backend(backend_id)
    except AgentBackendNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    info = await backend.get_info(user_role=current_user.role, db=db, user=current_user)
    return {**info, "backend_id": backend_id, "type": "agent_backend"}


@agent_router.get("")
async def list_agents(
    include_subagents: bool = Query(False),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    await repo.ensure_default_agent()
    items = await repo.list_visible(user=current_user, include_subagent_definitions=include_subagents)
    backend_info_cache: dict[tuple[str, bool, str], dict] = {}
    agents = [await _serialize_agent(repo, item, current_user, backend_info_cache=backend_info_cache) for item in items]
    return {"agents": agents}


@agent_router.get("/default")
async def get_default_agent(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    repo = AgentRepository(db)
    item = await repo.ensure_default_agent()
    if not item or not user_can_access_agent(current_user, item):
        raise HTTPException(status_code=404, detail="默认智能体不可访问")
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}


@agent_router.post("")
async def create_agent(
    payload: AgentCreate, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    try:
        backend = get_agent_backend(payload.backend_id)
    except AgentBackendNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if payload.set_default:
        raise HTTPException(status_code=422, detail="默认智能体已固定为内置智能助手")

    repo = AgentRepository(db)
    try:
        config_json, config_resource_access = await prepare_agent_config_write(
            payload.config_json or {},
            context_schema=backend.context_schema,
            db=db,
            user=current_user,
        )
        item = await repo.create(
            name=payload.name,
            slug=payload.slug,
            backend_id=payload.backend_id,
            description=payload.description,
            icon=payload.icon,
            pics=payload.pics,
            config_json=config_json,
            config_resource_access=config_resource_access,
            share_config=payload.share_config,
            is_default=payload.set_default,
            is_subagent=payload.is_subagent,
            created_by=str(current_user.uid),
            creator=current_user,
            visibility=payload.visibility,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}


@agent_router.get("/{agent_id}")
async def get_agent(agent_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="any", for_run=False)
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}


@agent_router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="any", for_run=False)
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not user_can_manage_agent(current_user, item):
        raise HTTPException(status_code=403, detail="不能编辑非自己创建的智能体")

    try:
        backend = get_agent_backend(item.backend_id)
        config_json = None
        config_resource_access = None
        if payload.config_json is not None:
            config_json, config_resource_access = await prepare_agent_config_write(
                payload.config_json,
                context_schema=backend.context_schema,
                db=db,
                user=current_user,
            )

        updated = await repo.update(
            item,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            pics=payload.pics,
            config_json=config_json,
            config_resource_access=config_resource_access,
            share_config=payload.share_config,
            is_subagent=payload.is_subagent,
            updated_by=str(current_user.uid),
            updater=current_user,
            fields_set=payload.model_fields_set,
        )
    except AgentBackendNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, updated, current_user, include_configurable_items=True)}


@agent_router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind="any", for_run=False)
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not user_can_manage_agent(current_user, item):
        raise HTTPException(status_code=403, detail="不能删除非自己创建的智能体")
    if is_builtin_agent(item):
        raise HTTPException(status_code=409, detail="内置智能体不能删除")
    try:
        await repo.delete(agent=item, user=current_user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"success": True}


@agent_router.post("/{agent_id}/set_default")
async def set_agent_default(
    agent_id: str,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    repo = AgentRepository(db)
    agent_slug = agent_id  # 兼容既有路径参数名；这里实际是 Agent.slug。
    item = await repo.get_by_slug(agent_slug)
    if not item:
        raise HTTPException(status_code=404, detail="智能体不存在")
    try:
        updated = await repo.set_default(agent=item, updated_by=str(current_user.uid))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, updated, current_user, include_configurable_items=True)}


class AgentPublish(BaseModel):
    """发布时明确提交共享范围。"""

    model_config = ConfigDict(extra="forbid")
    share_config: dict


@agent_router.post("/{agent_id}/publish")
async def publish_agent(
    agent_id: str,
    payload: AgentPublish,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """发布自己的私有 Agent，保留身份和历史数据归属。"""
    repo = AgentRepository(db)
    try:
        item = await repo.publish(slug=agent_id, user=current_user, share_config=payload.share_config)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agent": await _serialize_agent(repo, item, current_user, include_configurable_items=True)}
