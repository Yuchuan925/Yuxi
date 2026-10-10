"""OIDC 登录用例拥有身份写入与成功响应的事务边界。"""

from yuxi.infrastructure.observability.logging import logger
from yuxi.modules.identity import oidc
from yuxi.modules.identity.security import AuthUtils


async def build_oidc_login_response(db, extracted_info: dict) -> tuple[dict | None, str | None]:
    """完成身份校验、开户与响应装配后统一提交，失败统一回滚。"""
    try:
        response, error = await _prepare_oidc_login_response(db, extracted_info)
        if error:
            await db.rollback()
        else:
            await db.commit()
        return response, error
    except BaseException:
        await db.rollback()
        raise


async def _prepare_oidc_login_response(db, extracted_info: dict) -> tuple[dict | None, str | None]:
    """验证账号绑定并构造一次性 code 的登录数据。"""
    # 查找用户：总是先通过 sub 查找，保证绑定关系可验证
    sub = extracted_info["sub"]
    user_by_sub = await oidc.find_user_by_oidc_sub(db, sub)

    if oidc.oidc_config.use_raw_username:
        # 使用原始用户名模式
        username = extracted_info["username"]
        user = None
        if username:
            user_by_name = await oidc.find_oidc_user_by_raw_username(db, username)
            if user_by_name and user_by_name.user_kind == "end_user":
                return None, "终端用户不能用于 OIDC 登录"
            if user_by_name and user_by_name.is_deleted:
                user_by_name = None

            if user_by_sub:
                # sub 已经绑定到一个用户
                if user_by_name and user_by_sub.id == user_by_name.id:
                    # sub 绑定的用户就是找到的用户名用户 -> 验证通过
                    user = user_by_name
                    logger.info(f"OIDC user logged in with raw username: {username} (sub: {sub})")
                else:
                    # sub 已经绑定到另一个用户，存在冲突，拒绝登录
                    conflict_name = user_by_sub.username if not user_by_name else user_by_name.username
                    logger.warning(
                        f"OIDC sub {sub} is already bound to a different user, "
                        f"login rejected to prevent account hijacking (conflict: {conflict_name})"
                    )
                    return None, "OIDC标识已绑定到其他账号，请联系管理员处理绑定冲突"
            else:
                # sub 尚未绑定到任何用户
                if user_by_name:
                    # 用户名存在，且 sub 没有绑定 -> 允许登录，并创建绑定记录
                    # 在不修改表结构的情况下，我们创建一个占位用户 oidc:{sub} 来记录绑定关系
                    # 这个占位用户不会被用来登录，仅用于存储sub -> 用户的绑定关系
                    user = user_by_name
                    logger.info(f"Binding new OIDC sub {sub} to existing user with raw username: {username}")
                    # 创建绑定占位用户（后台静默创建，不影响现有用户）
                    await oidc.create_oidc_binding_placeholder(db, sub, user_by_name)
                else:
                    # 用户名不存在，需要创建新用户
                    if oidc.oidc_config.auto_create_user:
                        user = None  # 让后续逻辑创建
                    else:
                        return None, "用户不存在，请联系管理员开通账号"
        else:
            # 没有获取到 username，回退到按sub查找
            user = user_by_sub
    else:
        # 标准 OIDC 模式，通过 sub 查找
        user = user_by_sub

    if user:
        await oidc.update_oidc_user_login(db, user)
        logger.info(f"OIDC user logged in: {user.username}")
    elif oidc.oidc_config.auto_create_user:
        deleted_user = await oidc.find_deleted_oidc_user_by_sub(db, sub)
        if deleted_user:
            user = await oidc.restore_deleted_oidc_user(db, deleted_user, extracted_info)
            logger.info(f"OIDC deleted user restored and logged in: {user.username}")
        else:
            # 从用户信息中获取部门信息
            dept_name = extracted_info.get("department_name")
            dept_desc = extracted_info.get("department_description")
            dept = await oidc.get_or_create_oidc_department(db, dept_name, dept_desc)
            department_id = dept.id if dept else None
            user = await oidc.create_oidc_user(db, extracted_info, department_id)
    else:
        return None, "用户未注册，请联系管理员开通账号"

    if user.user_kind == "end_user":
        return None, "终端用户不能用于 OIDC 登录"
    if user.is_deleted:
        return None, "该账户已注销"

    token_data = {"sub": str(user.id)}
    jwt_token = AuthUtils.create_access_token(token_data)

    department_name = None
    if user.department_id:
        department_name = await oidc.get_oidc_department_name(db, user.department_id)

    response_data = {
        "access_token": jwt_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "uid": user.uid,
        "phone_number": user.phone_number,
        "avatar": user.avatar,
        "role": user.role,
        "department_id": user.department_id,
        "department_name": department_name,
    }

    return response_data, None
