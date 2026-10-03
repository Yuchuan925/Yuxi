"""组织与初始身份管理用例的事务 Owner。"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.modules.identity.repositories.departments import DepartmentRepository
from yuxi.modules.identity.repositories.users import UserRepository
from yuxi.modules.identity.models import Department, User
from yuxi.modules.identity.security import AuthUtils
from yuxi.shared.datetime import utc_now_naive

_INITIALIZATION_LOCK_KEY = 0x59555849


class IdentityConflictError(Exception):
    """唯一身份或部门事实在提交时发生冲突。"""


class SystemAlreadyInitializedError(Exception):
    """系统初始化已由当前或并发请求完成。"""


@dataclass(frozen=True)
class DepartmentAdminCreation:
    """同一事务创建的部门和管理员。"""

    department: Department
    admin: User


async def list_managed_users_page(
    db: AsyncSession,
    *,
    offset: int,
    limit: int,
    is_superadmin: bool,
    visible_department_id: int | None,
    department_id: int | None,
    role: str | None,
    search: str | None,
) -> dict:
    """返回管理员可见范围内的用户分页。"""
    effective_department_id = department_id if is_superadmin else visible_department_id
    if not is_superadmin and effective_department_id is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    rows, total = await UserRepository(db).list_page_with_department(
        offset=offset,
        limit=limit,
        department_id=effective_department_id,
        role=role,
        search=search,
    )
    items = []
    for user, department_name in rows:
        item = user.to_dict()
        item["department_name"] = department_name
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def create_department_with_admin(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    admin_uid: str,
    admin_password: str,
    admin_phone: str | None,
) -> DepartmentAdminCreation:
    """原子创建部门和首位管理员。"""

    password_hash = AuthUtils.hash_password(admin_password)
    try:
        try:
            department = await DepartmentRepository(db).create(
                {
                    "name": name,
                    "description": description,
                }
            )
            admin = await UserRepository(db).create(
                {
                    "username": admin_uid,
                    "uid": admin_uid,
                    "phone_number": admin_phone,
                    "password_hash": password_hash,
                    "role": "admin",
                    "department_id": department.id,
                }
            )
        except IntegrityError as exc:
            raise IdentityConflictError("部门名称、管理员用户ID、用户名或手机号已存在") from exc

        await db.commit()
        return DepartmentAdminCreation(department=department, admin=admin)
    except Exception:
        await db.rollback()
        raise


async def initialize_system_admin(
    db: AsyncSession,
    *,
    uid: str,
    password: str,
    phone_number: str | None,
) -> DepartmentAdminCreation:
    """串行、原子地创建默认部门和超级管理员。"""

    password_hash = AuthUtils.hash_password(password)
    try:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _INITIALIZATION_LOCK_KEY})

        if not await UserRepository(db).is_first_run():
            raise SystemAlreadyInitializedError("系统已经初始化，无法再次创建初始管理员")

        try:
            department = await DepartmentRepository(db).create(
                {
                    "name": "默认部门",
                    "description": "系统初始化时创建的默认部门",
                }
            )
            admin = await UserRepository(db).create(
                {
                    "username": uid,
                    "uid": uid,
                    "phone_number": phone_number,
                    "avatar": None,
                    "password_hash": password_hash,
                    "role": "superadmin",
                    "department_id": department.id,
                    "last_login": utc_now_naive(),
                }
            )
        except IntegrityError as exc:
            raise IdentityConflictError("初始化身份事实与现有数据库约束冲突") from exc

        await db.commit()
        return DepartmentAdminCreation(department=department, admin=admin)
    except SystemAlreadyInitializedError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise


async def promote_user_role(db: AsyncSession, *, user: User, role: str) -> None:
    """只提升有效产品用户角色，持久化前锁定当前身份。"""
    from sqlalchemy import select

    current = await db.scalar(
        select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
    )
    ranks = {"user": 0, "admin": 1, "superadmin": 2}
    if current is None or current.is_deleted or current.user_kind != "human":
        raise ValueError("只能提升有效产品用户角色")
    if role not in ranks or ranks[role] < ranks[current.role]:
        raise ValueError("角色只允许提升，禁止降级")
    current.role = role
