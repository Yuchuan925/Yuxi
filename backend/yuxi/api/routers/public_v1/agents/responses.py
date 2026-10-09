"""公开接口错误及长期 SSE 的文档说明。"""

from typing import Any

from pydantic import BaseModel


class PublicError(BaseModel):
    """现有 FastAPI 错误结构。"""

    detail: str | dict[str, Any] | list[dict[str, Any]]



PUBLIC_ERRORS = {
    401: {"model": PublicError, "description": "缺少有效 JWT 或 API Key。"},
    403: {"model": PublicError, "description": "凭据权限或终端用户作用域不允许此操作。"},
    404: {"model": PublicError, "description": "资源不存在，或当前用户/APP 不可见。"},
    409: {
        "model": PublicError,
        "description": "幂等键对应不同意图，或当前生命周期不允许操作；查询持久状态后调整请求。",
    },
    422: {"model": PublicError, "description": "字段、图片、配置、事件类型或恢复游标无效；修正请求后重试。"},
}

SSE_RESPONSE = {
    "description": "长期 Session 事件订阅；客户端根据目标 Turn 终态回读结果并主动关闭连接。",
    "content": {
        "text/event-stream": {
            "schema": {"type": "string"},
            "example": (
                "id: <cursor>\nevent: agent.session.turn.completed\n"
                'data: {"type":"agent.session.turn.completed","event_id":"<event-id>",'
                '"session_id":"<session-id>","turn_id":"<turn-id>",'
                '"turn":{"id":"<turn-id>","object":"agent.session.turn",'
                '"session_id":"<session-id>","agent_id":"default-chatbot","subagent_id":null,'
                '"status":"completed","created_at":1780000000,"started_at":1780000000,'
                '"completed_at":1780000002,"error":null,"usage":null},'
                '"yuxi":{"run_id":"<run-id>","input_id":"<input-id>",'
                '"result_run_id":"<run-id>","current_run_id":"<run-id>",'
                '"created_by_run_id":null,"error_type":null}}\n\n'
            ),
        }
    },
}
