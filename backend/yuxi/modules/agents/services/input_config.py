"""在接收输入时冻结模型与工具审批配置。"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.modules.agents.runtime.tool_approval import DEFAULT_TOOL_APPROVAL_MODE, normalize_tool_approval_mode
from yuxi.modules.models.providers.cache import model_cache
from yuxi.modules.system.options import system_options


async def resolve_agent_run_config(
    model_spec: str | None,
    tool_approval_mode: str | None,
    agent_item,
    agent_backend,
    db: AsyncSession | None = None,
) -> tuple[str, str]:
    """一次冻结本次输入的模型与审批模式。"""
    context = load_agent_run_context(agent_item, agent_backend)
    return (
        await resolve_agent_run_model_spec(model_spec, getattr(context, "model", None), db),
        resolve_agent_run_tool_approval_mode(tool_approval_mode, getattr(context, "tool_approval_mode", None)),
    )


def load_agent_run_context(agent_item, agent_backend):
    """只读取 Agent 配置，不准备 worker 的运行时 Context。"""
    context = agent_backend.context_schema()
    config_json = getattr(agent_item, "config_json", None) or {}
    config_context = config_json.get("context") if isinstance(config_json, dict) else {}
    if isinstance(config_context, dict):
        context.update_config(config_context)
    return context


async def resolve_agent_run_model_spec(
    requested_model: str | None, configured_model: str | None, db: AsyncSession | None = None
) -> str:
    """按显式值、Agent 配置和系统默认值选择聊天模型。"""
    model_spec = next(
        (
            candidate.strip()
            for candidate in (requested_model, configured_model)
            if isinstance(candidate, str) and candidate.strip()
        ),
        None,
    )
    if model_spec is None:
        model_spec = str((await system_options.get(db))["default_model"]).strip()
    info = model_cache.get_model_info(model_spec)
    if not info or info.model_type != "chat":
        raise HTTPException(
            status_code=422,
            detail={"code": "chat_model_not_found", "message": f"未找到可用聊天模型: '{model_spec}'"},
        )
    return model_spec


def resolve_agent_run_tool_approval_mode(requested_mode: str | None, configured_mode: str | None) -> str:
    """解析当前执行段的工具审批模式。"""
    source = requested_mode if requested_mode is not None else configured_mode or DEFAULT_TOOL_APPROVAL_MODE
    try:
        return normalize_tool_approval_mode(source)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
