"""Skill 运行时解析。"""

from __future__ import annotations

from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.skills.repository import SkillRepository
from yuxi.agents.skills.service import list_accessible_skills, normalize_string_list
from yuxi.agents.toolkits import get_all_tool_instances
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import VIRTUAL_PERSONAL_SKILLS_PATH, VIRTUAL_SKILLS_PATH


class SkillPromptMetadata(TypedDict):
    """模型提示词使用的 Skill 元数据。"""

    name: str
    description: str
    path: str


class SkillDependencyNode(TypedDict):
    """单个 Skill 的运行时依赖。"""

    tools: list[str]
    mcps: list[str]
    skills: list[str]


async def _list_skills_from_db(db: AsyncSession | None = None, user=None) -> list:
    """从数据库加载当前调用方可访问的 Skill。"""
    if db is not None:
        if user is not None:
            return await list_accessible_skills(db, user)
        repo = SkillRepository(db)
        return await repo.list_enabled()

    async with pg_manager.get_async_session_context() as session:
        if user is not None:
            return await list_accessible_skills(session, user)
        repo = SkillRepository(session)
        return await repo.list_enabled()


def build_prompt_metadata(skills: list) -> dict[str, SkillPromptMetadata]:
    """按共享投影与个人 UserWorkspace 的真实路径构建提示词元数据。"""
    result: dict[str, SkillPromptMetadata] = {}
    for item in skills:
        if not item.slug:
            continue
        root = (
            VIRTUAL_PERSONAL_SKILLS_PATH if getattr(item, "source_scope", None) == "personal" else VIRTUAL_SKILLS_PATH
        )
        result[item.slug] = {
            "name": item.name,
            "description": item.description,
            "path": f"{root}/{item.slug}/SKILL.md",
        }
    return result


def build_dependency_map(skills: list) -> dict[str, SkillDependencyNode]:
    """从已授权 Skill 构建确定性的依赖映射。"""
    result: dict[str, SkillDependencyNode] = {}
    for item in skills:
        if not item.slug:
            continue
        result[item.slug] = {
            "tools": normalize_string_list(item.tool_dependencies or []),
            "mcps": normalize_string_list(item.mcp_dependencies or []),
            "skills": normalize_string_list(item.skill_dependencies or []),
        }
    return result


def build_source_map(skills: list) -> dict[str, str]:
    """构建当前用户授权共享 Skill 的只读投影来源。"""
    return {
        item.slug: str(item.source_dir)
        for item in skills
        if item.slug and getattr(item, "source_dir", None) and getattr(item, "source_scope", None) != "personal"
    }


def expand_skill_closure(
    slugs: list[str] | None,
    dependency_map: dict[str, SkillDependencyNode],
) -> list[str]:
    """展开 Skill 依赖闭包并保持根与依赖的声明顺序。"""
    ordered_roots = normalize_string_list(slugs)
    if not ordered_roots:
        return []

    result: list[str] = []
    seen: set[str] = set()

    def dfs(slug: str, stack: set[str]) -> None:
        if slug in stack:
            logger.warning(f"Cycle detected in skill dependencies, skip: {' -> '.join([*stack, slug])}")
            return
        if slug in seen:
            return

        node = dependency_map.get(slug)
        if not node:
            logger.warning(f"Skill dependency target not found in DB, skip: {slug}")
            return

        seen.add(slug)
        result.append(slug)
        next_stack = set(stack)
        next_stack.add(slug)
        for dep in node.get("skills", []):
            dfs(dep, next_stack)

    for root in ordered_roots:
        dfs(root, set())
    return result


async def resolve_runtime_skills_for_context(
    context,
    *,
    db: AsyncSession | None = None,
    user=None,
) -> dict[str, Any]:
    """从已授权 Skill 派生当前 Agent Run 的运行时 scope。"""
    skill_items = await _list_skills_from_db(db, user)
    dependency_map = build_dependency_map(skill_items)
    prompt_metadata = build_prompt_metadata(skill_items)
    available = set(dependency_map)
    selected = normalize_string_list(getattr(context, "skills", None))
    context_skills = [slug for slug in selected if slug in available]
    prompt_skills = expand_skill_closure(context_skills, dependency_map)
    return {
        "context_skills": context_skills,
        "prompt_skills": prompt_skills,
        "readable_skills": prompt_skills,
        "runtime_skill_metadata": prompt_metadata,
        "runtime_skill_dependency_map": dependency_map,
        "runtime_skill_sources": build_source_map(skill_items),
    }


def resolve_skill_gated_tools(context) -> list:
    """解析所有可见 Skill 依赖且需注册到 ToolNode 的本地工具。"""
    dependency_map = getattr(context, "_runtime_skill_dependency_map", {}) or {}
    readable_skills = getattr(context, "_readable_skills", []) or []
    tool_names: set[str] = set()
    for slug in readable_skills:
        node = dependency_map.get(slug) or {}
        tool_names.update(node.get("tools", []))
    if not tool_names:
        return []
    return [tool for tool in get_all_tool_instances() if tool.name in tool_names]


def build_dependency_bundle(
    activated_skills: list[str],
    dependency_map: dict[str, SkillDependencyNode],
) -> dict[str, list[str]]:
    """汇总直接激活 Skill 的本地工具和 MCP 依赖。"""
    tools: list[str] = []
    mcps: list[str] = []
    seen_tools: set[str] = set()
    seen_mcps: set[str] = set()

    for slug in activated_skills:
        dependency = dependency_map.get(slug, {})
        for tool_name in dependency.get("tools", []):
            if tool_name in seen_tools:
                continue
            seen_tools.add(tool_name)
            tools.append(tool_name)
        for mcp_name in dependency.get("mcps", []):
            if mcp_name in seen_mcps:
                continue
            seen_mcps.add(mcp_name)
            mcps.append(mcp_name)

    return {"tools": tools, "mcps": mcps}
