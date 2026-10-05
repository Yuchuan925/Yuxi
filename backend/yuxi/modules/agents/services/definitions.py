"""Agent 定义与专属 Skill 的共同生命周期。"""

import asyncio
import shutil

from yuxi.infrastructure.runtime_settings import get_skill_data_dir
from yuxi.modules.agents.repositories.definitions import AgentRepository
from yuxi.modules.agents.runtime.agent_backends import get_agent_backend
from yuxi.modules.agents.runtime.context import validate_resource_selection
from yuxi.modules.agents.services.configuration import prepare_agent_config_write
from yuxi.modules.extensions.mcp.runtime import clear_mcp_server_tools_cache
from yuxi.modules.extensions.mcp.service import create_mcp_server
from yuxi.modules.extensions.skills.bound import publish_bound_skill
from yuxi.modules.extensions.skills.draft import prepared_uploaded_skill
from yuxi.modules.extensions.skills.package import parse_skill_dir_metadata
from yuxi.modules.extensions.skills.projection import invalidate_skill_projections, refresh_committed_skill_projections
from yuxi.modules.extensions.skills.repository import SkillRepository
from yuxi.modules.extensions.skills.shared import validate_skill_dependencies
from yuxi.modules.identity.repositories.users import UserRepository


async def create_agent_definition(db, *, operator, mcp_servers=None, skill_upload=None, **fields):
    """统一准备 Agent、远程 MCP 与可选专属包，只有完整结果可以提交。"""
    backend = get_agent_backend(fields["backend_id"])
    if fields.pop("set_default", False):
        raise ValueError("默认智能体已固定为内置智能助手")
    operator = await UserRepository(db).lock_active_human(operator.uid)
    if operator is None:
        raise PermissionError("必须由有效产品用户创建智能体")
    if mcp_servers and operator.role != "superadmin":
        raise PermissionError("仅超级管理员可以导入 MCP 配置")

    try:
        imported = []
        for config in mcp_servers or []:
            server = await create_mcp_server(db, **config, created_by=operator.username, commit=False)
            imported.append(server.slug)
        config_json = fields.pop("config_json", None) or {}
        if imported:
            context = config_json.setdefault("context", {})
            if not isinstance(context, dict):
                raise ValueError("智能体 context 必须为对象")
            selected = validate_resource_selection("mcps", context.get("mcps", []))
            if selected != "all":
                context["mcps"] = list(dict.fromkeys([*selected, *imported]))
        config_json, resource_access = await prepare_agent_config_write(
            config_json, context_schema=backend.context_schema, db=db, user=operator
        )
        agent = await AgentRepository(db).create(
            **fields,
            config_json=config_json,
            config_resource_access=resource_access,
            created_by=operator.uid,
            creator=operator,
            commit=False,
        )
        if skill_upload is not None:
            filename, file_bytes = skill_upload
            with prepared_uploaded_skill(filename=filename, file_bytes=file_bytes) as source:
                await publish_bound_skill(
                    db,
                    agent=agent,
                    operator=operator,
                    source=source,
                    source_slug=parse_skill_dir_metadata(source)["slug"],
                )
        else:
            await db.commit()
    except Exception:
        await db.rollback()
        raise

    for slug in imported:
        clear_mcp_server_tools_cache(slug)
    return agent


async def commit_agent_definition(db, agent, user, *, refresh_binding: bool = True) -> None:
    """定义授权变化时校验绑定依赖并撤下旧投影，再共同提交。"""
    item = await SkillRepository(db).get_by_bound_agent_id(agent.id, for_update=True) if refresh_binding else None
    uids = []
    if item is not None:
        item.bound_agent = agent
        await validate_skill_dependencies(
            db=db,
            parent=item,
            tool_dependencies=item.tool_dependencies,
            mcp_dependencies=item.mcp_dependencies,
            skill_dependencies=item.skill_dependencies,
            available_skills={row.slug: row for row in await SkillRepository(db).list_enabled_readable(user)},
        )
        uids = await invalidate_skill_projections(db, item.slug)
    await db.commit()
    await refresh_committed_skill_projections(uids)


async def update_agent_definition(db, agent, **kwargs):
    """配置与绑定授权在一个事务中生效。"""
    agent = await AgentRepository(db).update(agent, **kwargs, commit=False)
    await commit_agent_definition(db, agent, kwargs["updater"], refresh_binding=kwargs.get("share_config") is not None)
    return agent


async def delete_agent_definition(db, *, agent, user) -> None:
    """沿用活跃执行拒绝规则，提交删除后清理已经定位的专属内容。"""
    # repository 先持有 Agent/Thread 锁并证明无活跃执行，随后由 FK 删除绑定行。
    from yuxi.modules.extensions.skills.bound import lock_manageable_agent

    agent, user = await lock_manageable_agent(db, agent.slug, user)
    item = await SkillRepository(db).get_by_bound_agent_id(agent.id, for_update=True)
    await AgentRepository(db).delete(agent=agent, user=user, commit=False)
    if item is None:
        await db.commit()
        return
    slug = item.slug
    packages = get_skill_data_dir() / "packages" / slug
    owned = list(packages.iterdir()) if packages.is_dir() else []
    uids = await invalidate_skill_projections(db, slug)
    await db.commit()
    for root in owned:
        try:
            await asyncio.to_thread(shutil.rmtree, root)
        except FileNotFoundError:
            pass  # 无引用目录可能已被其他清理完成。
    await refresh_committed_skill_projections(uids)
