"""真实 PostgreSQL 的消息、目录、版本和评估关系负向证据。"""

import os
import asyncio
import sys
import textwrap

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_schema_migration_version import _create_isolated_manager, _drop_isolated_schema
from yuxi.migrations.schema import create_business_tables, create_knowledge_tables, ensure_business_schema

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本测试只使用独立 Schema。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """资源由独立 Schema 拥有。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本测试不创建沙盒。"""
    yield


async def test_knowledge_relationships_reject_cross_scope_and_keep_delete_semantics():
    """单 ID 都合法仍不能跨 KB、Dataset 或 active version 归属。"""
    schema, admin, engine, manager = await _create_isolated_manager("pytest_relations")
    try:
        await create_knowledge_tables(manager)
        async with engine.begin() as conn:
            for sql in (
                "INSERT INTO knowledge_bases (kb_id,name,kb_type) VALUES ('a','A','milvus'),('b','B','milvus')",
                "INSERT INTO knowledge_files (file_id,kb_id,filename) VALUES ('fa','a','a'),('fb','b','b'),('folder','a','folder')",
                "INSERT INTO evaluation_datasets (dataset_id,kb_id,name) VALUES ('da','a','A'),('db','b','B'),('da2','a','A2')",
                "INSERT INTO evaluation_dataset_items (item_id,dataset_id,kb_id,item_index,query_text) VALUES ('ia','da','a',0,'a'),('ib','db','b',0,'b'),('ia2','da2','a',0,'a2')",
                "INSERT INTO evaluation_runs (run_id,kb_id,dataset_id,name) VALUES ('ra','a','da','run')",
                "INSERT INTO knowledge_file_versions (version_id,file_id,kb_id,generation,is_active) VALUES ('va','fa','a',1,true)",
            ):
                await conn.execute(text(sql))
        invalid = (
            ("fk_knowledge_files_parent_kb", "UPDATE knowledge_files SET parent_id='fa' WHERE file_id='fb'"),
            (
                "fk_knowledge_files_active_version",
                "UPDATE knowledge_files SET active_version_id='va' WHERE file_id='fb'",
            ),
            (
                "fk_knowledge_files_active_version",
                "UPDATE knowledge_files SET active_version_id='va',active_generation=2 WHERE file_id='fa'",
            ),
            (
                "uq_knowledge_file_versions_active",
                "INSERT INTO knowledge_file_versions (version_id,file_id,kb_id,generation,is_active) VALUES ('va2','fa','a',2,true)",
            ),
            (
                "fk_evaluation_runs_dataset_kb",
                "INSERT INTO evaluation_runs (run_id,kb_id,dataset_id,name) VALUES ('bad','a','db','bad')",
            ),
            (
                "fk_evaluation_run_items_run_dataset",
                "INSERT INTO evaluation_run_items (run_id,dataset_id,dataset_item_id,item_index,query_text) VALUES ('ra','db','ib',0,'bad')",
            ),
            (
                "fk_evaluation_run_items_item_dataset",
                "INSERT INTO evaluation_run_items (run_id,dataset_id,dataset_item_id,item_index,query_text) VALUES ('ra','da','ia2',0,'bad')",
            ),
            (
                "fk_evaluation_run_items_item_dataset",
                "INSERT INTO evaluation_run_items (run_id,dataset_item_id,item_index,query_text) VALUES ('ra','ib',0,'bad')",
            ),
        )
        for constraint, sql in invalid:
            with pytest.raises(IntegrityError, match=constraint):
                async with engine.begin() as conn:
                    await conn.execute(text(sql))
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE knowledge_files SET parent_id='folder' WHERE file_id='fa'"))
            await conn.execute(text("DELETE FROM knowledge_files WHERE file_id='folder'"))
            await conn.execute(
                text(
                    "INSERT INTO evaluation_run_items (run_id,dataset_id,dataset_item_id,item_index,query_text) VALUES ('ra','da','ia',0,'snapshot')"
                )
            )
            await conn.execute(text("DELETE FROM evaluation_datasets WHERE dataset_id='da'"))
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT parent_id FROM knowledge_files WHERE file_id='fa'")) is None
            assert await conn.scalar(text("SELECT dataset_id FROM evaluation_runs WHERE run_id='ra'")) is None
            row = (
                await conn.execute(
                    text("SELECT dataset_id,dataset_item_id,query_text FROM evaluation_run_items WHERE run_id='ra'")
                )
            ).one()
            assert tuple(row) == (None, None, "snapshot")
    finally:
        await _drop_isolated_schema(schema, admin, engine)


async def test_unselected_message_and_run_thread_must_match_execution_scope():
    """不被结果指针选择的审计消息也必须属于相同 Thread/Turn/Run。"""
    schema, admin, engine, manager = await _create_isolated_manager("pytest_message_scope")
    try:
        await create_business_tables(manager)
        await ensure_business_schema(manager)
        async with engine.begin() as conn:
            for sql in (
                "INSERT INTO users (uid,username,password_hash,role,is_deleted,login_failed_count) VALUES ('u','u','fixture','user',0,0)",
                "INSERT INTO projects (id,uid,selection_status,workdir_path,directory_mode) VALUES ('p','u','implicit','projects/p','managed')",
                "INSERT INTO conversations (thread_id,uid,agent_id,project_id,status,is_pinned) VALUES ('ta','u','main','p','active',false),('tb','u','main','p','active',false)",
                "INSERT INTO agent_turns (id,conversation_thread_id,uid,status,created_at) VALUES ('turn','ta','u','running',CURRENT_TIMESTAMP)",
                "INSERT INTO agent_runs (id,conversation_thread_id,runtime_scope_id,turn_id,conversation_id,agent_slug,uid,status,source,channel,run_type,origin_metadata,input_payload,token_usage,runtime_cleanup_pending) SELECT 'run','ta','ta','turn',id,'main','u','pending','chat','web','chat','{}','{}','{}',false FROM conversations WHERE thread_id='ta'",
            ):
                await conn.execute(text(sql))
        with pytest.raises(IntegrityError, match="fk_messages_run_turn_conversation"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO messages (conversation_id,role,content,run_id,turn_id,delivery_status,message_type) SELECT id,'assistant','audit','run','turn','complete','model_audit' FROM conversations WHERE thread_id='tb'"
                    )
                )
        with pytest.raises(IntegrityError, match="fk_agent_runs_conversation_thread"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE agent_runs SET conversation_id=(SELECT id FROM conversations WHERE thread_id='tb') WHERE id='run'"
                    )
                )
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM messages")) == 0
            assert await conn.scalar(text("SELECT conversation_thread_id FROM agent_runs WHERE id='run'")) == "ta"
    finally:
        await _drop_isolated_schema(schema, admin, engine)


async def test_killed_fresh_initializer_is_recoverable_without_adopting_old_data(monkeypatch):
    """真实终止建表后的进程，持久初始化标记使第二次运行可安全重试。"""
    from yuxi.migrations import main as migration
    from yuxi.infrastructure.postgres.schema import (
        get_schema_versions,
        BUSINESS_SCHEMA_VERSION,
        KNOWLEDGE_SCHEMA_VERSION,
    )

    schema, admin, engine, manager = await _create_isolated_manager("pytest_killed_initializer")
    code = textwrap.dedent("""
        import asyncio, os
        from sqlalchemy.ext.asyncio import create_async_engine
        from yuxi.infrastructure.postgres.manager import PostgresManager
        from yuxi.migrations import main
        manager = PostgresManager()
        manager.async_engine = create_async_engine(os.environ['POSTGRES_URL'], connect_args={'server_settings': {'search_path': os.environ['INITIALIZER_TEST_SCHEMA']}})
        manager._initialized = True
        manager.initialize = lambda: None
        main.pg_manager = manager
        async def stop_after_tables(manager):
            os._exit(23)
        main.setup_langgraph_checkpointer = stop_after_tables
        asyncio.run(main.main())
    """)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            env={**os.environ, "INITIALIZER_TEST_SCHEMA": schema},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode == 23, error.decode()
        assert await get_schema_versions(manager) == {"initializing": 1}
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('agent_runs') IS NOT NULL"))
        manager.AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(migration, "pg_manager", manager)
        monkeypatch.setattr(manager, "initialize", lambda: None)
        monkeypatch.setattr(manager, "close", lambda: asyncio.sleep(0))

        async def checkpoint_setup(manager):
            """本例只验证初始化进程恢复；checkpoint 装配由真实 E2E 覆盖。"""

        monkeypatch.setattr(migration, "setup_langgraph_checkpointer", checkpoint_setup)
        await migration.main()
        assert await get_schema_versions(manager) == {
            "business": BUSINESS_SCHEMA_VERSION,
            "knowledge": KNOWLEDGE_SCHEMA_VERSION,
        }
    finally:
        await _drop_isolated_schema(schema, admin, engine)
