"""OIDC provider discovery 与远端协议调用。"""

from typing import Any

import httpx

from yuxi.infrastructure.observability.logging import logger


class OIDCProviderMetadata:
    """OIDC Provider 元数据"""

    def __init__(self):
        self.authorization_endpoint: str | None = None
        self.token_endpoint: str | None = None
        self.userinfo_endpoint: str | None = None
        self.end_session_endpoint: str | None = None
        self.last_error: str | None = None
        self._loaded = False

    async def load(self, issuer_url: str) -> bool:
        """从 discovery 端点加载元数据"""
        if self._loaded:
            return True

        discovery_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(discovery_url, timeout=30.0)
                response.raise_for_status()
                metadata = response.json()

            self.authorization_endpoint = metadata.get("authorization_endpoint")
            self.token_endpoint = metadata.get("token_endpoint")
            self.userinfo_endpoint = metadata.get("userinfo_endpoint")
            self.end_session_endpoint = metadata.get("end_session_endpoint")

            # 登录 URL 生成至少需要 authorization_endpoint。
            if not self.authorization_endpoint:
                self.last_error = "discovery 响应缺少 authorization_endpoint"
                logger.error(f"Failed to load OIDC discovery: {self.last_error}, url={discovery_url}")
                return False

            self._loaded = True
            self.last_error = None
            logger.info(f"OIDC discovery loaded from {discovery_url}")
            return True

        except Exception as e:
            self.last_error = f"{type(e).__name__}: {repr(e)}"
            logger.error(f"Failed to load OIDC discovery: {self.last_error}, url={discovery_url}")
            return False


async def exchange_code_for_token(metadata: OIDCProviderMetadata, config, code: str) -> dict[str, Any] | None:
    """通过授权码调用 provider 的 token 端点。"""
    if not metadata.token_endpoint:
        return None
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri or "/api/auth/oidc/callback",
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                metadata.token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.error(f"Failed to exchange code for token: {exc}")
        return None


async def get_userinfo(metadata: OIDCProviderMetadata, access_token: str) -> dict[str, Any] | None:
    """从 provider 的 userinfo 端点读取声明。"""
    if not metadata.userinfo_endpoint:
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(metadata.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}, timeout=30.0)
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.error(f"Failed to get userinfo: {exc}")
        return None
