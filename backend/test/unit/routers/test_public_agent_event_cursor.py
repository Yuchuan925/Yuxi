"""Public SSE 在响应头发出前校验恢复游标。"""

import pytest
from fastapi import HTTPException

from server.routers.public_v1.agents.events import public_stream_response
from yuxi.services.agents.scope import ActorScope


def test_invalid_cursor_rejected_before_streaming_response():
    """非法游标必须成为 HTTP 422，不能在 SSE 200 后断流。"""
    with pytest.raises(HTTPException) as error:
        public_stream_response(
            scope=ActorScope(uid="user", app_id=None),
            thread_id="thread",
            after_cursor="not-a-cursor",
        )
    assert error.value.status_code == 422
