"""在 API、worker 与 Sandbox 启动前完成一次性存储迁移。"""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import shutil
from pathlib import Path

from sqlalchemy import select, text

from yuxi.agents.skills.service import (
    legacy_skill_storage_migration_completed,
    mark_legacy_skill_storage_migrated,
    migrate_legacy_skill_storage,
)
from yuxi.config import get_checkpoint_dir, get_legacy_storage_dir, get_user_data_dir
from yuxi.config.options import ensure_options_in_db, migrate_legacy_system_options
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.project_workdir_repository import FILE_STORAGE_MATERIALIZATION_ID
from yuxi.services.project_workdir_materialization_service import ensure_project_workdir_materialized
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import FileStorageMaterialization

_QUIESCENCE_TOKEN_ENV = "YUXI_STORAGE_MIGRATION_QUIESCENCE_TOKEN"
_QUIESCENCE_FILE_ENV = "YUXI_STORAGE_MIGRATION_QUIESCENCE_FILE"
_CHECKPOINT_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SQLITE_CHECKPOINT_FILES = {
    "aio_history.db",
    "aio_history.db-journal",
    "aio_history.db-shm",
    "aio_history.db-wal",
}


def _legacy_skill_roots_exist() -> bool:
    """判断是否仍有会被停机迁移删除的历史 Skill 目录。"""
    if legacy_skill_storage_migration_completed():
        return False
    shared_skills = get_legacy_storage_dir() / "skills"
    if shared_skills.is_symlink() or (shared_skills.is_dir() and any(shared_skills.iterdir())):
        return True
    shared_root = get_user_data_dir() / "shared"
    if not shared_root.is_dir():
        return False
    for uid_dir in shared_root.iterdir():
        legacy_skills = uid_dir / "workspace" / "agents" / "skills"
        if legacy_skills.is_symlink() or (legacy_skills.is_dir() and any(legacy_skills.iterdir())):
            return True
    return False


def _legacy_persistent_files_exist() -> bool:
    """判断旧广域目录是否仍保存系统配置或 SQLite checkpoint。"""
    legacy_root = get_legacy_storage_dir()
    if (legacy_root / "config/base.toml").is_file():
        return True
    agents_root = legacy_root / "agents"
    return agents_root.is_symlink() or (
        agents_root.is_dir()
        and any(path.is_file() or path.is_symlink() for path in agents_root.glob("*/aio_history.db*"))
    )


def _migrate_legacy_sqlite_checkpoints() -> None:
    """把旧显式 SQLite checkpoint 搬到 API/worker 共享的独立持久域。"""
    source_root = get_legacy_storage_dir() / "agents"
    if not source_root.exists() and not source_root.is_symlink():
        return
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"历史 checkpoint 根目录非法: {source_root}")
    target_root = get_checkpoint_dir() / "agents"
    for component_dir in sorted(source_root.iterdir(), key=lambda path: path.name):
        if (
            not _CHECKPOINT_COMPONENT_PATTERN.fullmatch(component_dir.name)
            or component_dir.is_symlink()
            or not component_dir.is_dir()
        ):
            raise ValueError(f"历史 checkpoint 组件目录非法: {component_dir}")
        for source in sorted(component_dir.iterdir(), key=lambda path: path.name):
            if source.name not in _SQLITE_CHECKPOINT_FILES:
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"历史 checkpoint 文件非法: {source}")
            target = target_root / component_dir.name / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.read_bytes() != source.read_bytes():
                    raise ValueError(f"SQLite checkpoint 新旧内容冲突: {target}")
            else:
                shutil.copy2(source, target, follow_symlinks=False)
    for component_dir in sorted(source_root.iterdir(), key=lambda path: path.name):
        for source in sorted(component_dir.iterdir(), key=lambda path: path.name):
            if source.name in _SQLITE_CHECKPOINT_FILES and source.is_file() and not source.is_symlink():
                source.unlink()
        if component_dir.is_dir() and not component_dir.is_symlink() and not any(component_dir.iterdir()):
            component_dir.rmdir()
    if not any(source_root.iterdir()):
        source_root.rmdir()


async def _legacy_cutover_pending_before_schema_init() -> bool:
    """用旧 schema 的持久事实区分首次安装、待迁移升级和已完成部署。"""
    async with pg_manager.get_async_session_context() as session:
        users_table_exists = bool(await session.scalar(text("SELECT to_regclass('users') IS NOT NULL")))
        if not users_table_exists:
            return False
        control_table_exists = bool(
            await session.scalar(text("SELECT to_regclass('file_storage_materializations') IS NOT NULL"))
        )
        if not control_table_exists:
            return True
        phase = await session.scalar(
            select(FileStorageMaterialization.phase).where(
                FileStorageMaterialization.id == FILE_STORAGE_MATERIALIZATION_ID
            )
        )
    return phase != "active"


def _require_quiescence_proof() -> None:
    """校验由宿主停机脚本创建的一次性证明，禁止普通 up 执行破坏性迁移。"""
    expected = os.getenv(_QUIESCENCE_TOKEN_ENV, "").strip()
    proof_file = Path(
        os.getenv(
            _QUIESCENCE_FILE_ENV,
            str(get_legacy_storage_dir() / ".storage-migration-quiesced"),
        )
    )
    try:
        actual = proof_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("检测到旧文件数据；请先运行 scripts/migrate-storage.sh 停止旧运行环境") from exc
    if not expected or not actual or not hmac.compare_digest(actual, expected):
        raise RuntimeError("存储迁移停机证明无效；请重新运行 scripts/migrate-storage.sh")


async def _converge_database_state(*, fail_nonterminal_runs: bool) -> None:
    """导入历史系统配置；仅在已停机的旧布局切换中收敛非终态 Run。"""
    async with pg_manager.get_async_session_context() as session:
        if fail_nonterminal_runs:
            await AgentRunRepository(session).fail_nonterminal_for_storage_migration()
        await ensure_options_in_db(session)
        await migrate_legacy_system_options(
            session,
            legacy_config_file=get_legacy_storage_dir() / "config" / "base.toml",
        )
        await session.commit()


async def main() -> None:
    """初始化迁移所需 schema，并在停机窗口切换全部文件 Owner。"""
    pg_manager.initialize()
    try:
        legacy_cutover_pending = await _legacy_cutover_pending_before_schema_init()
        await pg_manager.create_business_tables()
        await pg_manager.ensure_business_schema()
        requires_quiescence = legacy_cutover_pending or _legacy_skill_roots_exist() or _legacy_persistent_files_exist()
        if requires_quiescence:
            _require_quiescence_proof()
        _migrate_legacy_sqlite_checkpoints()
        await _converge_database_state(fail_nonterminal_runs=requires_quiescence)
        legacy_config_file = get_legacy_storage_dir() / "config/base.toml"
        if legacy_config_file.is_file() and not legacy_config_file.is_symlink():
            legacy_config_file.unlink()
        await ensure_project_workdir_materialized()
        async with pg_manager.get_async_session_context() as session:
            await migrate_legacy_skill_storage(session, remove_personal_legacy=True)
        mark_legacy_skill_storage_migrated()
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
