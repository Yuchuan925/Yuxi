from yuxi.modules.agents.runtime.checkpoint_cleanup import CheckpointCleanupModel
from yuxi.modules.agents.runtime.middlewares.authorization import RuntimeAuthorizationMiddleware
from typing import Any

from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from yuxi.modules.agents.runtime import BaseAgent, BaseState
from yuxi.modules.agents.runtime.sandbox.backend import create_agent_composite_backend
from yuxi.modules.agents.runtime.middlewares.filesystem import create_agent_filesystem_middleware
from yuxi.modules.extensions.skills.runtime import sync_agent_context_skills
from yuxi.modules.agents.runtime.agent_backends.chatbot.prompt import TODO_MID_PROMPT, build_prompt_with_context
from yuxi.modules.agents.runtime.agent_backends.subagent.context import SubAgentContext
from yuxi.modules.agents.runtime.context import DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS
from yuxi.modules.agents.runtime.middlewares import (
    ImageInputCompatibilityMiddleware,
    NetworkRetryMiddleware,
    SteerMiddleware,
    TokenUsageMiddleware,
    ToolErrorGuardMiddleware,
    create_summary_middleware_from_context,
)
from yuxi.modules.agents.runtime.middlewares.skills import SkillsMiddleware
from yuxi.modules.agents.runtime.tool_approval import create_tool_approval_middleware, normalize_tool_approval_mode
from yuxi.modules.agents.runtime.sandbox.paths import runtime_workdir_path
from yuxi.modules.extensions.tools.runtime import resolve_configured_runtime_tools
from yuxi.modules.models.chat import load_chat_model, resolve_chat_model_spec

_SUBAGENT_DISABLED_TOOLS = frozenset({"present_artifacts", "install_skill"})


def _tool_name(tool) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def _filter_disabled_tools(tools, disabled_tools: frozenset[str]):
    return [tool for tool in tools if _tool_name(tool) not in disabled_tools]


class _SubAgentToolFilterMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self):
        self.disabled_tools = _SUBAGENT_DISABLED_TOOLS

    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=_filter_disabled_tools(request.tools or [], self.disabled_tools)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(tools=_filter_disabled_tools(request.tools or [], self.disabled_tools)))

    # 工具列表隐藏不构成执行边界；显式传入的禁用工具调用也必须拒绝。
    def wrap_tool_call(self, request, handler):
        denial = self._denied_tool_message(request)
        return denial if denial is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        denial = self._denied_tool_message(request)
        return denial if denial is not None else await handler(request)

    def _denied_tool_message(self, request) -> ToolMessage | None:
        """为禁用调用生成与原 tool call 绑定的拒绝结果。"""
        name = _tool_name(request.tool_call)
        if name not in self.disabled_tools:
            return None
        return ToolMessage(
            content=(f"工具 {name} 对子智能体不可用；请把结果交回主智能体，由主线程按审批流程执行该操作。"),
            tool_call_id=request.tool_call.get("id") or "",
            name=name,
            status="error",
        )


async def _build_middlewares(context, backend, tool_approval_mode: str, *, cleanup_model=None):
    # tool_approval_mode is normalized once by the caller (get_graph / SubAgentBackend.get_graph).

    middlewares = [
        # 子 Agent 的工具异常也在最外层隔离，避免打断父对话。
        ToolErrorGuardMiddleware(),
        SteerMiddleware(),
        create_agent_filesystem_middleware(
            getattr(context, "tool_token_limit", DEFAULT_TOOL_RESULT_EVICTION_K_TOKENS) * 1024,
            backend=backend,
            disabled_tools=_SUBAGENT_DISABLED_TOOLS,
        ),
        SkillsMiddleware(),
        create_summary_middleware_from_context(context, backend=backend, model=cleanup_model)
        if cleanup_model
        else create_summary_middleware_from_context(context, backend=backend),
        TodoListMiddleware(system_prompt=TODO_MID_PROMPT),
        PatchToolCallsMiddleware(),
        _SubAgentToolFilterMiddleware(),
        NetworkRetryMiddleware(max_retries=getattr(context, "model_retry_times", 2)),
        ImageInputCompatibilityMiddleware(),
        TokenUsageMiddleware(),
    ]

    approval = create_tool_approval_middleware(
        tool_approval_mode, current_project_path=runtime_workdir_path(context.workdir_relative_path)
    )
    if approval:
        middlewares.append(approval)
    # after_model 按逆序执行，授权必须先于审批产生等待点。
    middlewares.append(RuntimeAuthorizationMiddleware())
    return middlewares


class SubAgentBackend(BaseAgent):
    name = "子智能体"
    description = "用于被主智能体通过 subagent_start 工具调用的专用智能体后端。"
    context_schema = SubAgentContext

    async def get_info(
        self,
        include_configurable_items: bool = True,
        user_role: str | None = None,
        db=None,
        user=None,
    ):
        info = await super().get_info(
            include_configurable_items=include_configurable_items,
            user_role=user_role,
            db=db,
            user=user,
        )
        tools_item = (info.get("configurable_items") or {}).get("tools")
        if isinstance(tools_item, dict):
            tools_item["options"] = [
                option
                for option in tools_item.get("options") or []
                if option.get("key") not in _SUBAGENT_DISABLED_TOOLS
            ]
        return info

    async def get_graph(self, *, context, checkpoint_only: bool = False, **kwargs):
        """构建执行图；checkpoint_only 仅供归属已验证的取消服务使用状态 API。"""
        tool_approval_mode = normalize_tool_approval_mode(getattr(context, "tool_approval_mode", "default"))
        if checkpoint_only:
            model = CheckpointCleanupModel()
            tools = []
            prompt = ""
            middlewares = await _build_middlewares(context, None, tool_approval_mode, cleanup_model=model)
        else:
            if not getattr(context, "_runtime_prepared", False):
                raise ValueError("构图需要已准备的 Context")
            await sync_agent_context_skills(context)
            backend = create_agent_composite_backend(context)
            model = load_chat_model(
                fully_specified_name=resolve_chat_model_spec(context.model),
                session_id=context.thread_id,
                uid=context.uid,
                max_retries=0,
            )
            tools = _filter_disabled_tools(await resolve_configured_runtime_tools(context), _SUBAGENT_DISABLED_TOOLS)
            prompt = build_prompt_with_context(context)
            middlewares = await _build_middlewares(context, backend, tool_approval_mode)
        return create_agent(
            model=model,
            tools=tools,
            system_prompt=prompt,
            middleware=middlewares,
            state_schema=BaseState,
            checkpointer=await self._get_checkpointer(),
        )
