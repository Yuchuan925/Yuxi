"""真实 PostgreSQL 上的用户 Skill 投影并发授权测试。"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.skills import service as skill_service
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Skill, User

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _wait_for_advisory_waiter(session_factory, lock_identity) -> bool:
    """等待真实 PostgreSQL 观察到同一 advisory lock 的阻塞者。"""
    deadline = asyncio.get_running_loop().time() + 5
    async with session_factory() as observer_db:
        while asyncio.get_running_loop().time() < deadline:
            waiting = bool(
                await observer_db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted "
                        "AND classid::bigint = :classid "
                        "AND objid::bigint = :objid AND objsubid = :objsubid)"
                    ),
                    {
                        "classid": lock_identity.classid,
                        "objid": lock_identity.objid,
                        "objsubid": lock_identity.objsubid,
                    },
                )
            )
            if waiting:
                return True
            await asyncio.sleep(0.01)
    return False


async def test_projection_refresh_waits_for_lock_then_reloads_revoked_authorization(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """等待 uid 锁的 refresh 必须在取锁后重读最新授权。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def local_session_context():
        async with session_factory() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    monkeypatch.setattr(pg_manager, "get_async_session_context", local_session_context)
    monkeypatch.setattr(skill_service, "get_save_dir", lambda: tmp_path)

    async def no_personal_skills(_uid: str, *, refresh: bool = False):
        del refresh
        return skill_service.PersonalSkillSnapshot(items=[], scanned_at="test", from_cache=False)

    monkeypatch.setattr(skill_service, "list_personal_skills", no_personal_skills)

    suffix = uuid.uuid4().hex
    uid = f"pytest-skill-user-{suffix}"
    slug = f"pytest-skill-{suffix}"
    source_dir = tmp_path / "skills" / slug
    source_dir.mkdir(parents=True)
    (source_dir / "SKILL.md").write_text("# authorized\n", encoding="utf-8")
    user_id: int | None = None
    skill_id: int | None = None
    refresh_task: asyncio.Task[dict[str, str]] | None = None
    policy_task: asyncio.Task[None] | None = None
    lock_scope = f"yuxi:skills:user-projection:v1:{uid}"

    try:
        async with session_factory() as db:
            user = User(username=uid, uid=uid, password_hash="test", role="user")
            skill = Skill(
                slug=slug,
                name=slug,
                description="PostgreSQL advisory lock integration fixture",
                source_type="upload",
                tool_dependencies=[],
                mcp_dependencies=[],
                skill_dependencies=[],
                dir_path=f"skills/{slug}",
                share_config={
                    "version": 2,
                    "read_scope": {
                        "access_level": "user",
                        "department_ids": [],
                        "user_uids": [uid],
                    },
                    "manage_scope": None,
                },
                enabled=True,
                created_by="another-user",
            )
            db.add_all([user, skill])
            await db.commit()
            user_id = user.id
            skill_id = skill.id

        skill_service.sync_user_accessible_skills(uid, {slug: source_dir})
        projection = skill_service.get_user_skills_root_dir(uid)
        assert (projection / slug / "SKILL.md").is_file()

        async with session_factory() as lock_db:
            await lock_db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_scope))"),
                {"lock_scope": lock_scope},
            )
            lock_identity = (
                await lock_db.execute(
                    text(
                        "SELECT classid::bigint, objid::bigint, objsubid "
                        "FROM pg_locks WHERE pid = pg_backend_pid() "
                        "AND locktype = 'advisory' AND granted"
                    )
                )
            ).one()
            refresh_task = asyncio.create_task(skill_service.refresh_user_skill_projection_async(uid))

            assert await _wait_for_advisory_waiter(session_factory, lock_identity), (
                "refresh did not wait on the expected PostgreSQL advisory lock"
            )
            assert not refresh_task.done()

            async with session_factory() as revoke_db:
                await revoke_db.execute(
                    update(Skill)
                    .where(Skill.id == skill_id)
                    .values(
                        share_config={
                            "version": 2,
                            "read_scope": {
                                "access_level": "user",
                                "department_ids": [],
                                "user_uids": ["different-user"],
                            },
                            "manage_scope": None,
                        }
                    )
                )
                await revoke_db.commit()

            await lock_db.commit()

        refreshed_sources = await asyncio.wait_for(refresh_task, timeout=5)
        assert slug not in refreshed_sources
        assert not (projection / slug).exists()

        async with session_factory() as db:
            await db.execute(
                update(Skill)
                .where(Skill.id == skill_id)
                .values(
                    share_config={
                        "version": 2,
                        "read_scope": {
                            "access_level": "user",
                            "department_ids": [],
                            "user_uids": [uid],
                        },
                        "manage_scope": None,
                    }
                )
            )
            await db.commit()
        await skill_service.refresh_user_skill_projection_async(uid)
        assert (projection / slug / "SKILL.md").is_file()

        async with session_factory() as lock_db:
            await lock_db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_scope))"),
                {"lock_scope": lock_scope},
            )
            lock_identity = (
                await lock_db.execute(
                    text(
                        "SELECT classid::bigint, objid::bigint, objsubid "
                        "FROM pg_locks WHERE pid = pg_backend_pid() "
                        "AND locktype = 'advisory' AND granted"
                    )
                )
            ).one()
            async with session_factory() as policy_db:
                await policy_db.execute(
                    update(Skill)
                    .where(Skill.id == skill_id)
                    .values(
                        share_config={
                            "version": 2,
                            "read_scope": {
                                "access_level": "user",
                                "department_ids": [],
                                "user_uids": ["different-user"],
                            },
                            "manage_scope": None,
                        }
                    )
                )
                policy_task = asyncio.create_task(skill_service.apply_skill_projection_policy_change(policy_db, slug))
                assert await _wait_for_advisory_waiter(session_factory, lock_identity), (
                    "policy mutation did not wait on the uid projection lock"
                )
                assert not policy_task.done()
                assert (projection / slug / "SKILL.md").is_file()
                await lock_db.commit()
                await asyncio.wait_for(policy_task, timeout=5)

        assert not (projection / slug).exists()
        async with session_factory() as db:
            persisted_share_config = await db.scalar(select(Skill.share_config).where(Skill.id == skill_id))
        assert persisted_share_config["read_scope"]["user_uids"] == ["different-user"]
    finally:
        for task in (refresh_task, policy_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        async with session_factory() as db:
            if skill_id is not None:
                await db.execute(delete(Skill).where(Skill.id == skill_id))
            if user_id is not None:
                await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        await engine.dispose()
