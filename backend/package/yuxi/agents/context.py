"""Define the configurable parameters for the agent."""

import uuid
from dataclasses import MISSING, dataclass, field, fields
from typing import get_origin

from yuxi import config as sys_config


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

    user_id: str = field(
        default_factory=lambda: str(uuid.uuid4()),
        metadata={"name": "用户ID", "configurable": False, "description": "用来唯一标识一个用户"},
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

    tools: list[str] = field(
        default_factory=lambda: ["ask_user_question", "tavily_search"],
        metadata={
            "name": "工具",
            "description": "内置的工具。",
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

    mcps: list[str] = field(
        default_factory=list,
        metadata={
            "name": "MCP服务器",
            "options": [],
            "description": (
                "MCP服务器列表，建议使用支持 SSE 的 MCP 服务器，"
                "如果需要使用 uvx 或 npx 运行的服务器，也请在项目外部启动 MCP 服务器，并在项目中配置 MCP 服务器。"
            ),
            "kind": "mcps",
        },
    )

    skills: list[str] = field(
        default_factory=list,
        metadata={
            "name": "Skills",
            "options": [],
            "description": "可选技能列表（由超级管理员维护）。运行时仅挂载并只读暴露选中的 "
            "skills。技能依赖的工具和 MCP 服务器也会被自动挂载。",
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

    subagents: list[str] = field(
        default_factory=list,
        metadata={
            "name": "子智能体",
            "options": [],
            "description": "可选子智能体列表。为空表示不启用任何 SubAgent。但依然会启用一个 general-purpose 的子智能体",
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
        },
    )

    @classmethod
    def get_configurable_items(cls):
        """实现一个可配置的参数列表，在 UI 上配置时使用"""
        configurable_items = {}
        for f in fields(cls):
            if f.init and not f.metadata.get("hide", False):
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
