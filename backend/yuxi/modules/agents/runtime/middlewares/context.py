"""通用的 Context 相关中间件"""

from collections.abc import Callable

from langchain.agents.middleware import ModelRequest, ModelResponse, dynamic_prompt, wrap_model_call

from yuxi.infrastructure.observability.logging import logger
from yuxi.modules.models.chat import load_chat_model, resolve_chat_model_spec


@dynamic_prompt
def context_aware_prompt(request: ModelRequest) -> str:
    """从 runtime context 动态生成系统提示词"""
    return request.runtime.context.system_prompt


@wrap_model_call
async def context_based_model(request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
    """从 runtime context 动态选择模型"""
    model_spec = resolve_chat_model_spec(request.runtime.context.model)
    # 重试由外层中间件执行，每次请求都必须重新进入授权边界。
    model = load_chat_model(model_spec, session_id=request.runtime.context.thread_id, uid=request.runtime.context.uid, max_retries=0)

    request = request.override(model=model)
    logger.debug(f"Using model {model_spec} for request {request.messages[-1].content[:200]}")
    return await handler(request)
