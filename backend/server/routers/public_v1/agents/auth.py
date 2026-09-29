"""Public Agent 路由的认证与资源作用域。"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services.agents.directory import resolve_public_user
from yuxi.services.agents.scope import ActorScope
from yuxi.storage.postgres.models_business import APIKey, User


@dataclass(frozen=True, slots=True)
class PublicAgentContext:
    """认证完成后的资源用户、凭据所有者与 APP 身份。"""

    user: User
    owner: User
    api_key: APIKey | None

    @property
    def scope(self) -> ActorScope:
        """把 HTTP 身份收敛为用例作用域。"""
        return ActorScope(
            uid=str(self.user.uid),
            app_id=self.api_key.app_id if self.api_key else None,
            api_key_id=self.api_key.id if self.api_key else None,
            is_superadmin=self.api_key is None and self.user.role == "superadmin",
        )


async def require_public_context(
    request: Request,
    end_user_id: str | None = Header(default=None, alias="X-End-User-Id"),
    owner: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
) -> PublicAgentContext:
    """将产品 JWT、完整 Key 和 APP Key 映射到各自的用户作用域。"""
    api_key = getattr(request.state, "api_key", None)
    if api_key is None:
        if end_user_id is not None:
            raise HTTPException(status_code=403, detail="X-End-User-Id 仅适用于 API Key")
        return PublicAgentContext(user=owner, owner=owner, api_key=None)
    if not api_key.app_id:
        if api_key.access_level != "full":
            raise HTTPException(status_code=403, detail="Agents Public API 需要绑定 app_id 的 API Key")
        if end_user_id is not None:
            raise HTTPException(status_code=403, detail="无 APP 的 full Key 不接受 X-End-User-Id")
        return PublicAgentContext(user=owner, owner=owner, api_key=api_key)
    user = await resolve_public_user(owner=owner, api_key=api_key, end_user_id=end_user_id, db=db)
    return PublicAgentContext(user=user, owner=owner, api_key=api_key)
