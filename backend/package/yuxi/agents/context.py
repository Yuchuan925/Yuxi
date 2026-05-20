"""Define the configurable parameters for the agent."""

import uuid
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, get_origin

from yuxi import config as sys_config


def _role_can_access(auth: str | None, role: str | None) -> bool:
    if not auth:
        return True
    if auth == "admin":
        return role in {"admin", "superadmin"}
    if auth == "superadmin":
        return role == "superadmin"
    return False


def filter_config_by_role(
    config_json: dict,
    role: str | None,
    context_schema: type["BaseContext"] | None = None,
) -> dict:
    """按 Context 字段 metadata.auth 过滤 config_json.context。"""
    if not isinstance(config_json, dict):
        return {}

    schema = context_schema or BaseContext
    restricted_fields = {
        f.name
        for f in fields(schema)
        if f.metadata.get("auth") and not _role_can_access(str(f.metadata.get("auth")), role)
    }
    if not restricted_fields:
        return dict(config_json)

    filtered = dict(config_json)
    context = filtered.get("context")
    if isinstance(context, dict):
        filtered["context"] = {key: value for key, value in context.items() if key not in restricted_fields}
    return filtered


@dataclass(kw_only=True)
class BaseContext:
    """
    定义一个基础 Context 供 各类 graph 继承

    配置优先级:
    1. 运行时配置(RunnableConfig)：最高优先级，直接从函数参数传入
    2. 类默认配置：最低优先级，类中定义的默认值
    """

    def update(self, data: dict):
        """更新配置字段"""
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    thread_id: str = field(
        default_factory=lambda: str(uuid.uuid4()),
        metadata={"name": "线程ID", "configurable": False, "description": "用来唯一标识一个对话线程"},
    )

    uid: str = field(
        default_factory=lambda: str(uuid.uuid4()),
        metadata={"name": "UID", "configurable": False, "description": "用来唯一标识一个用户"},
    )

    system_prompt: str = field(
        default="You are a helpful assistant.",
        metadata={"name": "系统提示词", "description": "用来描述智能体的角色和行为", "kind": "prompt"},
    )

    model: str = field(
        default=sys_config.default_model,
        metadata={
            "name": "智能体模型",
            "options": [],
            "description": "智能体的驱动模型，建议选择 Agent 能力较强的模型，不建议使用小参数模型。",
            "kind": "llm",
        },
    )

    tools: list[str] | None = field(
        default=None,
        metadata={
            "name": "工具",
            "description": "内置的工具。默认选择当前用户可用的全部工具。",
            "type": "list",
            "kind": "tools",
        },
    )

    knowledges: list[str] | None = field(
        default=None,
        metadata={
            "name": "知识库",
            "description": "知识库列表，可以在左侧知识库页面中创建知识库。默认选择当前用户可访问的全部知识库。",
            "type": "list",
            "kind": "knowledges",
        },
    )

    mcps: list[str] | None = field(
        default=None,
        metadata={
            "name": "MCP服务器",
            "options": [],
            "description": (
                "MCP服务器列表，默认选择当前用户可用的全部 MCP 服务器。建议使用支持 SSE 的 MCP 服务器，"
                "如果需要使用 uvx 或 npx 运行的服务器，也请在项目外部启动 MCP 服务器，并在项目中配置 MCP 服务器。"
            ),
            "type": "list",
            "kind": "mcps",
        },
    )

    skills: list[str] | None = field(
        default=None,
        metadata={
            "name": "Skills",
            "options": [],
            "description": "可选技能列表（由超级管理员维护），默认选择当前用户可用的全部 skills。"
            "技能依赖的工具和 MCP 服务器也会被自动挂载。",
            "type": "list",
            "kind": "skills",
        },
    )

    subagents_model: str = field(
        default=sys_config.default_model,
        metadata={
            "name": "子智能体的默认模型",
            "description": "为所有子智能体设置默认模型，可在各子智能体配置中单独覆盖。",
            "kind": "llm",
        },
    )

    subagents: list[str] | None = field(
        default=None,
        metadata={
            "name": "子智能体",
            "options": [],
            "description": (
                "可选子智能体列表，默认选择当前用户可用的全部 SubAgent。"
                "为空表示不启用任何 SubAgent，但依然会启用一个 general-purpose 的子智能体。"
            ),
            "type": "list",
            "kind": "subagents",
        },
    )

    summary_threshold: int = field(
        default=100,
        metadata={
            "name": "上下文摘要触发阈值 (KB)",
            "description": "当上下文大小超过该值时，启用摘要功能以优化上下文使用。单位为 KB，默认值为 100KB。",
            "type": "number",
            "auth": "admin",
        },
    )

    @classmethod
    def get_configurable_items(cls, user_role: str | None = None):
        """实现一个可配置的参数列表，在 UI 上配置时使用"""
        configurable_items = {}
        for f in fields(cls):
            if f.init and not f.metadata.get("hide", False):
                if user_role is not None and not _role_can_access(f.metadata.get("auth"), user_role):
                    continue
                if f.metadata.get("configurable", True):
                    type_name = cls._get_type_name(f.type)

                    options = f.metadata.get("options", [])
                    if callable(options):
                        options = options()

                    configurable_items[f.name] = {
                        "type": f.metadata.get("type", type_name),
                        "name": f.metadata.get("name", f.name),
                        "options": options,
                        "default": f.default
                        if f.default is not MISSING
                        else f.default_factory()
                        if f.default_factory is not MISSING
                        else None,
                        "description": f.metadata.get("description", ""),
                        "kind": f.metadata.get("kind", ""),
                    }

        return configurable_items

    @classmethod
    def _get_type_name(cls, field_type) -> str:
        """获取类型名称"""
        origin = get_origin(field_type)
        if origin is not None:
            if hasattr(origin, "__name__"):
                return origin.__name__
            return str(origin)
        elif hasattr(field_type, "__name__"):
            return field_type.__name__
        else:
            return str(field_type)

    def update_from_dict(self, data: dict):
        """从字典更新配置字段"""
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)


_DEFAULT_ALL_CONTEXT_FIELDS = frozenset({"tools", "knowledges", "mcps", "skills", "subagents"})


def _normalize_selected_resource_names(value: Any, available: list[str]) -> list[str]:
    if not isinstance(value, list):
        return []

    allowed = set(available)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen or name not in allowed:
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def _resource_fields_requiring_available_names(normalized: dict, resource_fields: set[str]) -> set[str]:
    fields_to_load: set[str] = set()
    for field_name in resource_fields:
        current = normalized.get(field_name)
        if current is None:
            fields_to_load.add(field_name)
        elif isinstance(current, list) and current:
            fields_to_load.add(field_name)
        else:
            normalized[field_name] = []
    return fields_to_load


async def normalize_agent_context_config(
    context: dict | None,
    *,
    db,
    user,
    context_schema: type[BaseContext] | None = None,
) -> dict:
    schema = context_schema or BaseContext
    raw_context = dict(context) if isinstance(context, dict) else {}
    filtered = filter_config_by_role({"context": raw_context}, getattr(user, "role", None), schema)
    normalized = dict(filtered.get("context") or {})
    field_names = {item.name for item in fields(schema)}
    resource_fields = _DEFAULT_ALL_CONTEXT_FIELDS & field_names
    if not resource_fields:
        return normalized

    fields_to_load = _resource_fields_requiring_available_names(normalized, resource_fields)
    if not fields_to_load:
        return normalized

    available: dict[str, list[str]] = {}
    if "tools" in fields_to_load:
        from yuxi.agents.toolkits import get_all_tool_instances

        available["tools"] = [
            tool.name for tool in get_all_tool_instances() if isinstance(getattr(tool, "name", None), str)
        ]
    if "knowledges" in fields_to_load:
        from yuxi.knowledge import knowledge_base

        databases = (await knowledge_base.get_databases_by_user(user)).get("databases", [])
        available["knowledges"] = [
            str(db_item.get("db_id") or db_item.get("id"))
            for db_item in databases
            if isinstance(db_item, dict) and (db_item.get("db_id") or db_item.get("id"))
        ]
    if "mcps" in fields_to_load:
        from yuxi.services.mcp_service import get_enabled_mcp_server_names

        available["mcps"] = await get_enabled_mcp_server_names(db=db)
    if "skills" in fields_to_load:
        from yuxi.services.skill_service import list_skill_slugs

        available["skills"] = await list_skill_slugs(db)
    if "subagents" in fields_to_load:
        from yuxi.services.subagent_service import get_enabled_subagent_names

        available["subagents"] = await get_enabled_subagent_names(db)

    for field_name, available_names in available.items():
        current = normalized.get(field_name)
        if current is None:
            normalized[field_name] = available_names
        else:
            normalized[field_name] = _normalize_selected_resource_names(current, available_names)

    return normalized
