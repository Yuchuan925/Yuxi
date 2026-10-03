"""OIDC HTTP 回调和重定向响应。"""

from urllib.parse import urlencode

from fastapi import HTTPException, status
from fastapi.responses import RedirectResponse

from yuxi.modules.identity.oidc import (
    FRONTEND_CALLBACK_PATH,
    FRONTEND_LOGIN_PATH,
    OIDCUtils,
    build_oidc_login_response,
    oidc_config,
)


def _redirect_to_callback(exchange_code: str) -> RedirectResponse:
    """成功后重定向到前端 OIDC 回调页面，仅携带一次性 code"""
    url = f"{FRONTEND_CALLBACK_PATH}?{urlencode({'code': exchange_code})}"
    return RedirectResponse(url=url, status_code=302)


def _redirect_to_login_with_error(error_message: str) -> RedirectResponse:
    """失败时重定向到登录页并携带错误信息"""
    url = f"{FRONTEND_LOGIN_PATH}?{urlencode({'oidc_error': error_message})}"
    return RedirectResponse(url=url, status_code=302)


async def get_oidc_config_handler():
    """获取 OIDC 配置（供前端使用）"""
    if not oidc_config.enabled or not oidc_config.is_configured():
        return {"enabled": False}

    provider_name = oidc_config.provider_name
    return {"enabled": True, "provider_name": provider_name}


async def oidc_callback_handler(code: str, state: str, db):
    """处理 OIDC 回调 - 重定向到前端 Vue 路由"""

    if not oidc_config.is_token_exchange_configured():
        return _redirect_to_login_with_error("OIDC 配置不完整，请联系管理员")

    if not OIDCUtils.verify_state(state):
        return _redirect_to_login_with_error("登录会话已过期，请返回登录页重试")

    token_response = await OIDCUtils.exchange_code_for_token(code)
    if not token_response:
        return _redirect_to_login_with_error("无法获取访问令牌，请返回登录页重试")

    access_token = token_response.get("access_token")
    if not access_token:
        return _redirect_to_login_with_error("无法获取访问令牌，请返回登录页重试")

    userinfo = await OIDCUtils.get_userinfo(access_token)
    if not userinfo:
        return _redirect_to_login_with_error("无法获取用户信息，请返回登录页重试")

    extracted_info = OIDCUtils.extract_user_info(userinfo)
    sub = extracted_info["sub"]

    if not sub:
        return _redirect_to_login_with_error("无法获取用户标识，请返回登录页重试")

    response_data, error = await build_oidc_login_response(db, extracted_info)
    if error:
        return _redirect_to_login_with_error(error)

    exchange_code = OIDCUtils.generate_login_code(response_data)
    return _redirect_to_callback(exchange_code)


async def oidc_exchange_code_handler(code: str) -> dict:
    """用一次性 code 交换登录响应数据"""
    token_data = OIDCUtils.consume_login_code(code)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="登录 code 无效或已过期，请重新登录",
        )
    return token_data


async def oidc_login_url_handler(redirect_path: str = "/"):
    """获取 OIDC 登录 URL"""
    if not oidc_config.enabled or not oidc_config.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC 登录暂不可用，请联系管理员",
        )

    login_url = await OIDCUtils.build_authorization_url(redirect_path)
    if not login_url:
        metadata_error = OIDCUtils.get_last_metadata_error()
        if metadata_error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"生成登录链接失败：{metadata_error}",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="生成登录链接失败，请稍后重试或联系管理员",
        )

    return {"login_url": login_url}
