from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

from yuxi.modules.agents.runtime.middlewares.authorization import AgentExecutionRevoked

logger = logging.getLogger(__name__)

_RETHROW_EXCEPTIONS = (AgentExecutionRevoked, GraphBubbleUp, asyncio.CancelledError, KeyboardInterrupt, SystemExit)

_ERROR_HINT = (
    "请分析报错原因后继续：参数问题请修正后重试；该工具不支持当前输入"
    "（如路径域不匹配、文件不可达）时改用其它合适的工具或方法；"
    "同一调用连续两次同因失败后停止重试，如实向用户说明情况。"
)


class ToolErrorGuardMiddleware(AgentMiddleware):
    """把工具执行异常隔离为工具结果消息（对话不中断）。"""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        try:
            return handler(request)
        except _RETHROW_EXCEPTIONS:
            raise
        except Exception as exc:
            return self._error_tool_message(request, exc)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        failure = None

        async def execute():
            """在子任务内捕获控制异常，由原调用任务继续传播。"""
            nonlocal failure
            try:
                return await handler(request)
            except BaseException as exc:
                failure = exc

        execution = asyncio.create_task(execute())
        try:
            result = await asyncio.shield(execution)
            if failure is not None:
                raise failure
            return result
        except asyncio.CancelledError:
            # 线程/远端工具不能因取消 Python await 就算结束；收敛后外层才释放 Run lease。
            while not execution.done():
                try:
                    await asyncio.shield(execution)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not execution.cancelled():
                execution.exception()
            raise
        except _RETHROW_EXCEPTIONS:
            raise
        except Exception as exc:
            return self._error_tool_message(request, exc)

    @staticmethod
    def _error_tool_message(request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_call = request.tool_call or {}
        name = str(tool_call.get("name") or "unknown")
        tool_call_id = str(tool_call.get("id") or "")
        # 工具异常文本可能包含凭据或私有路径，不能进入模型上下文或日志。
        error_type = type(exc).__name__
        logger.warning("[tool-error-guard] 工具 %s 执行异常：%s", name, error_type)
        content = f"工具 {name} 执行失败：{error_type}\n{_ERROR_HINT}"
        return ToolMessage(content=content, name=name, tool_call_id=tool_call_id, status="error")
