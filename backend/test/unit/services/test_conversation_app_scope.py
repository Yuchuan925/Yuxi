"""Thread 已读写入的 APP 归属不能只依赖 HTTP 预检。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import yuxi.modules.agents.services.threads as threads
from yuxi.modules.agents.services.scope import ActorScope


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_side_effect_rejects_same_user_other_app(monkeypatch):
    """直接调用 service 也不能跨 APP 使用相同 UID 的 Thread。"""

    class Repository:
        def __init__(self, _db):
            """绑定测试事务。"""

        async def lock_conversation_by_thread_id(self, _thread_id):
            return SimpleNamespace(uid="user-1", app_id="app-a", status="active")

    monkeypatch.setattr(threads, "ConversationRepository", Repository)
    with pytest.raises(HTTPException) as exc:
        await threads.mark_thread_viewed(
            db=object(), scope=ActorScope(uid="user-1", app_id="app-b"), thread_id="thread-1"
        )
    assert exc.value.status_code == 404
