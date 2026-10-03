from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.presets import discover_agent_presets
from yuxi.modules.agents.runtime.agent_backends import get_agent_backend
from yuxi.modules.agents.runtime.context import BaseContext, filter_config_by_role, resolve_agent_resource_options
from yuxi.modules.identity.models import User


async def prepare_agent_config_write(
    config_json: dict[str, Any],
    *,
    context_schema: type[BaseContext] | None,
    db: AsyncSession,
    user: User,
) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """过滤可写配置，并解析本次资源补丁对应的可访问键。"""
    filtered = filter_config_by_role(config_json, user.role, context_schema)
    if filtered != config_json:
        raise ValueError("配置包含当前角色不可写或未声明的字段")
    context = filtered.get("context")
    if not isinstance(context, dict):
        return filtered, {}

    resource_fields = (context_schema or BaseContext).get_resource_fields()
    submitted_fields = {
        field_name
        for field_name in resource_fields.keys() & context.keys()
        if isinstance(context[field_name], list) and context[field_name]
    }
    if not submitted_fields:
        return filtered, {}

    option_fields = {resource_fields[name] for name in submitted_fields}
    options = await resolve_agent_resource_options(option_fields, db=db, user=user)

    resource_access: dict[str, set[str]] = {}
    for field_name in submitted_fields:
        option_field = resource_fields[field_name]
        if option_field not in options:
            raise RuntimeError(f"智能体资源字段 {field_name} 缺少权限解析结果")
        resource_access[field_name] = {option["key"] for option in options[option_field]}
    return filtered, resource_access


async def initialize_agent_presets(db: AsyncSession) -> None:
    """确认所有角色后端存在，再按既有落库规则初始化。"""
    from yuxi.modules.agents.repositories.definitions import AgentRepository

    presets = discover_agent_presets()
    for preset in presets:
        get_agent_backend(preset.backend_id)
    repository = AgentRepository(db)
    for preset in presets:
        await repository.ensure_preset(preset)
