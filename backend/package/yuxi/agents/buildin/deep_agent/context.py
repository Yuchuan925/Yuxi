"""Deep Agent Context - 基于BaseContext的深度分析上下文配置"""

from dataclasses import dataclass, field

from yuxi.agents import BaseContext

from .prompt import DEEP_PROMPT


@dataclass
class DeepContext(BaseContext):
    """
    Deep Agent 的上下文配置，继承自 BaseContext
    专门用于深度分析任务的配置管理
    """

    system_prompt: str = field(
        default=DEEP_PROMPT,
        metadata={"name": "系统提示词", "description": "Deep智能体的角色和行为指导", "kind": "prompt"},
    )

    subagents_model: str = field(
        default="siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash",
        metadata={
            "name": "Sub-agent Model",
            "description": "子智能体的默认模型，会被子智能体的配置覆盖。",
            "kind": "llm",
        },
    )
