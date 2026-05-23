from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.buildin import agent_manager
from yuxi.storage.postgres.models_business import Agent, User
from yuxi.utils.datetime_utils import utc_now_naive

DEFAULT_AGENT_SLUG = "default-chatbot"
DEFAULT_AGENT_NAME = "智能助手"
DEFAULT_AGENT_BACKEND_ID = "ChatbotAgent"
DEFAULT_SHARE_CONFIG = {"access_level": "global", "department_ids": [], "user_uids": []}
ACCESS_LEVELS = {"global", "department", "user"}
ADMIN_ROLES = {"admin", "superadmin"}


def is_builtin_agent(agent: Agent) -> bool:
    return agent.slug == DEFAULT_AGENT_SLUG


def _normalize_department_ids(department_ids: list | None) -> list[int]:
    return [int(department_id) for department_id in department_ids or []]


def _normalize_user_uids(user_uids: list | None) -> list[str]:
    return [uid for uid in (str(uid).strip() for uid in user_uids or []) if uid]


def normalize_agent_share_config(
    share_config: dict | None,
    *,
    user_uid: str | None = None,
    department_id: int | str | None = None,
    force_private: bool = False,
) -> dict:
    if force_private:
        if not user_uid:
            raise ValueError("私有智能体必须绑定创建用户")
        return {"access_level": "user", "department_ids": [], "user_uids": [str(user_uid)]}

    config = share_config or {}
    access_level = config.get("access_level") or "global"
    if access_level not in ACCESS_LEVELS:
        raise ValueError("无效的智能体权限等级")

    if access_level == "global":
        return DEFAULT_SHARE_CONFIG.copy()

    if access_level == "department":
        department_ids = _normalize_department_ids(config.get("department_ids"))
        if department_id is not None:
            department_ids.append(int(department_id))
        department_ids = sorted(set(department_ids))
        if not department_ids:
            raise ValueError("部门共享至少需要选择一个部门")
        return {"access_level": "department", "department_ids": department_ids, "user_uids": []}

    user_uids = _normalize_user_uids(config.get("user_uids"))
    if user_uid:
        user_uids.append(str(user_uid))
    user_uids = sorted(set(user_uids))
    if not user_uids:
        raise ValueError("指定人可访问至少需要选择一个用户")
    return {"access_level": "user", "department_ids": [], "user_uids": user_uids}


def user_can_access_agent(user: User, agent: Agent) -> bool:
    if user.role == "superadmin":
        return True
    user_uid = str(user.uid)
    if agent.created_by == user_uid:
        return True

    share_config = agent.share_config or DEFAULT_SHARE_CONFIG.copy()
    access_level = share_config.get("access_level")
    if access_level == "global":
        return True

    if access_level == "department":
        if user.department_id is None:
            return False
        try:
            return int(user.department_id) in [int(value) for value in share_config.get("department_ids") or []]
        except (TypeError, ValueError):
            return False

    if access_level == "user":
        return user_uid in (share_config.get("user_uids") or [])

    return False


def user_can_manage_agent(user: User, agent: Agent) -> bool:
    return user.role in ADMIN_ROLES or agent.created_by == str(user.uid)


def _slugify(value: str | None) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower()).strip("-")
    return base[:56] or f"agent-{uuid.uuid4().hex[:12]}"


class AgentRepository:
    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def ensure_default_agent(self, *, created_by: str | None = None) -> Agent:
        agent = await self.get_by_slug(DEFAULT_AGENT_SLUG)
        if agent:
            needs_update = False
            if agent.share_config != DEFAULT_SHARE_CONFIG:
                agent.share_config = DEFAULT_SHARE_CONFIG.copy()
                needs_update = True
            if not agent.is_default:
                return await self.set_default(agent=agent, updated_by=created_by)
            if needs_update:
                agent.updated_by = created_by
                agent.updated_at = utc_now_naive()
                await self.db.commit()
                await self.db.refresh(agent)
            return agent

        agent = Agent(
            slug=DEFAULT_AGENT_SLUG,
            backend_id=DEFAULT_AGENT_BACKEND_ID,
            name=DEFAULT_AGENT_NAME,
            description=None,
            icon=None,
            pics=[],
            config_json={"context": {}},
            share_config=DEFAULT_SHARE_CONFIG.copy(),
            is_default=True,
            created_by=created_by,
            updated_by=created_by,
            created_at=utc_now_naive(),
            updated_at=utc_now_naive(),
        )
        self.db.add(agent)
        await self.db.commit()
        await self.db.refresh(agent)
        return agent

    async def list_visible(self, *, user: User) -> list[Agent]:
        result = await self.db.execute(select(Agent).order_by(Agent.is_default.desc(), Agent.id.asc()))
        agents = list(result.scalars().all())
        if user.role == "superadmin":
            return agents
        return [agent for agent in agents if user_can_access_agent(user, agent)]

    async def get_by_slug(self, slug: str) -> Agent | None:
        result = await self.db.execute(select(Agent).where(Agent.slug == slug))
        return result.scalar_one_or_none()

    async def get_visible_by_slug(self, *, slug: str, user: User) -> Agent | None:
        agent = await self.get_by_slug(slug)
        if agent and user_can_access_agent(user, agent):
            return agent
        return None

    async def get_default(self) -> Agent | None:
        result = await self.db.execute(select(Agent).where(Agent.is_default.is_(True)))
        return result.scalar_one_or_none()

    async def set_default(self, *, agent: Agent, updated_by: str | None = None) -> Agent:
        if not is_builtin_agent(agent):
            raise ValueError("默认智能体已固定为内置智能助手")
        share_config = agent.share_config or DEFAULT_SHARE_CONFIG.copy()
        if share_config.get("access_level") != "global":
            raise ValueError("内置智能体必须全局共享")

        now = utc_now_naive()
        await self.db.execute(update(Agent).where(Agent.is_default.is_(True)).values(is_default=False, updated_at=now))
        agent.is_default = True
        agent.updated_by = updated_by
        agent.updated_at = now
        await self.db.commit()
        await self.db.refresh(agent)
        return agent

    async def _slug_exists(self, slug: str) -> bool:
        result = await self.db.execute(select(Agent.id).where(Agent.slug == slug))
        return result.scalar_one_or_none() is not None

    async def _unique_slug(self, desired: str | None, name: str) -> str:
        base = _slugify(desired or name)
        candidate = base
        idx = 2
        while await self._slug_exists(candidate):
            suffix = f"-{idx}"
            candidate = f"{base[: 80 - len(suffix)]}{suffix}"
            idx += 1
        return candidate

    async def create(
        self,
        *,
        name: str,
        backend_id: str,
        slug: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        pics: list[str] | None = None,
        config_json: dict | None = None,
        share_config: dict | None = None,
        is_default: bool = False,
        created_by: str | None = None,
        creator: User | None = None,
    ) -> Agent:
        normalized_share_config = normalize_agent_share_config(
            share_config,
            user_uid=str(creator.uid) if creator else created_by,
            department_id=creator.department_id if creator else None,
            force_private=bool(creator and creator.role not in ADMIN_ROLES),
        )
        if is_default and normalized_share_config.get("access_level") != "global":
            raise ValueError("默认智能体必须全局共享")

        agent = Agent(
            slug=await self._unique_slug(slug, name),
            backend_id=backend_id,
            name=name.strip() or "未命名智能体",
            description=description,
            icon=icon,
            pics=pics or [],
            config_json=config_json or {"context": {}},
            share_config=normalized_share_config,
            is_default=False,
            created_by=created_by,
            updated_by=created_by,
            created_at=utc_now_naive(),
            updated_at=utc_now_naive(),
        )
        self.db.add(agent)
        await self.db.commit()
        await self.db.refresh(agent)
        if is_default:
            return await self.set_default(agent=agent, updated_by=created_by)
        return agent

    async def update(
        self,
        agent: Agent,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        pics: list[str] | None = None,
        config_json: dict | None = None,
        share_config: dict | None = None,
        updated_by: str | None = None,
        updater: User | None = None,
    ) -> Agent:
        if name is not None:
            agent.name = name.strip() or "未命名智能体"
        if description is not None:
            agent.description = description
        if icon is not None:
            agent.icon = icon
        if pics is not None:
            agent.pics = pics
        if config_json is not None:
            agent.config_json = config_json
        if share_config is not None:
            if is_builtin_agent(agent):
                agent.share_config = DEFAULT_SHARE_CONFIG.copy()
            else:
                normalized_share_config = normalize_agent_share_config(
                    share_config,
                    user_uid=str(updater.uid) if updater else updated_by,
                    department_id=updater.department_id if updater else None,
                    force_private=bool(updater and updater.role not in ADMIN_ROLES),
                )
                agent.share_config = normalized_share_config

        agent.updated_by = updated_by
        agent.updated_at = utc_now_naive()
        await self.db.commit()
        await self.db.refresh(agent)
        return agent

    async def delete(self, *, agent: Agent) -> None:
        await self.db.delete(agent)
        await self.db.commit()

    async def serialize(
        self,
        agent: Agent,
        *,
        user: User,
        include_configurable_items: bool = False,
        backend_info_cache: dict[tuple[str, bool, str], dict] | None = None,
    ) -> dict[str, Any]:
        data = agent.to_dict()
        data["can_manage"] = user_can_manage_agent(user, agent)
        data["is_builtin"] = is_builtin_agent(agent)
        data["permission_locked"] = is_builtin_agent(agent)

        backend = agent_manager.get_agent(agent.backend_id)
        if backend:
            cache_key = (agent.backend_id, include_configurable_items, user.role)
            backend_info = backend_info_cache.get(cache_key) if backend_info_cache is not None else None
            if backend_info is None:
                backend_info = await backend.get_info(
                    include_configurable_items=include_configurable_items,
                    user_role=user.role,
                    db=self.db if include_configurable_items else None,
                    user=user if include_configurable_items else None,
                )
                if backend_info_cache is not None:
                    backend_info_cache[cache_key] = backend_info
            data["capabilities"] = backend_info.get("capabilities", [])
            data["metadata"] = backend_info.get("metadata", {})
            if include_configurable_items:
                data["configurable_items"] = backend_info.get("configurable_items", {})
        else:
            data["capabilities"] = []
            data["metadata"] = {}
            if include_configurable_items:
                data["configurable_items"] = {}
        return data
