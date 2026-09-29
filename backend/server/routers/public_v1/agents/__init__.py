"""Agent 对话的 Public Thread 协议与 Session 命名适配。"""

from fastapi import APIRouter

from .capabilities import router as capabilities_router
from .directory import router as directory_router
from .events import router as events_router
from .sessions import router as sessions_router
from .threads import router as threads_router
from .turns import router as turns_router

public_agents_router = APIRouter(prefix="/v1/agents", tags=["agents-public-v1"])
public_agents_router.include_router(threads_router)
public_agents_router.include_router(turns_router)
public_agents_router.include_router(events_router)
public_agents_router.include_router(capabilities_router)
public_agents_router.include_router(sessions_router)
public_agents_router.include_router(directory_router)

__all__ = ["public_agents_router"]
