"""SQLite 项目绑定与真实文件系统上的 Workdir/UserWorkspace 契约。"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.modules.workspace.paths import ensure_bound_user_workdir
from yuxi.modules.agents.repositories.threads import ConversationRepository
from yuxi.infrastructure.postgres.base import Base
from yuxi.modules.workspace.models import Project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_conversations_share_workdir_only_through_project(monkeypatch, tmp_path: Path):
    """同一 Project 的 Conversation 共享目录，不各自保存 Workdir 路径。"""
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "user-data"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            first_path = f"projects/2026-10-03_12-00-00_{uuid.uuid4().hex[:8]}"
            ensure_bound_user_workdir("user-1", first_path)
            project = Project(
                id=str(uuid.uuid4()),
                uid="user-1",
                selection_status="implicit",
                workdir_path=first_path,
                directory_mode="managed",
            )
            db.add(project)
            await db.flush()
            first = await ConversationRepository(db).add_conversation(
                uid="user-1",
                agent_id="main",
                thread_id="thread-1",
                project_id=project.id,
            )
            await db.commit()
            assert first.project_id == project.id
            assert not hasattr(first, "workdir_path")
            first_directory = tmp_path / "user-data" / "shared" / "user-1" / "workspace" / first_path
            assert first_directory.is_dir()

            second = await ConversationRepository(db).add_conversation(
                uid="user-1",
                agent_id="main",
                thread_id="thread-2",
                project_id=project.id,
            )
            await db.commit()
            assert second.project_id == first.project_id
    finally:
        await engine.dispose()
