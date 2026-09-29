"""Schema 初始化进程使用的 PostgreSQL 建表与当前约束 SQL。"""

from contextlib import asynccontextmanager
from sqlalchemy import text
from yuxi.modules.agents.models.runs import (
    AGENT_RUN_SHAPE_CONSTRAINT_NAME,
    AGENT_RUN_SHAPE_CONSTRAINT_SQL,
    AGENT_RUN_TERMINAL_STATUSES,
)
from yuxi.modules.workspace.models import PROJECT_STATUS_CONSTRAINT_NAME, PROJECT_STATUS_CONSTRAINT_SQL
from yuxi.infrastructure.postgres.base import BusinessBase, KnowledgeBase
from yuxi.infrastructure.observability.logging import logger

from yuxi.infrastructure.postgres.schema import SCHEMA_VERSION_TABLE

AGENT_RUN_TERMINAL_STATUS_SQL = ", ".join(f"'{status}'" for status in AGENT_RUN_TERMINAL_STATUSES)


AGENT_RUN_LEASE_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS worker_id VARCHAR(128)",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP WITHOUT TIME ZONE",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_status_lease_expires ON agent_runs(status, lease_expires_at)",
)

AGENT_RUN_EXECUTION_SEQ_SCHEMA_STATEMENTS = (
    "CREATE SEQUENCE IF NOT EXISTS agent_runs_execution_seq",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS execution_seq BIGINT",
    "ALTER TABLE IF EXISTS agent_runs ALTER COLUMN execution_seq SET DEFAULT nextval('agent_runs_execution_seq')",
    "ALTER TABLE IF EXISTS agent_runs ALTER COLUMN execution_seq SET NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_runs_execution_seq_unique ON agent_runs(execution_seq)",
    (
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_thread_execution_seq "
        "ON agent_runs(conversation_thread_id, execution_seq)"
    ),
)

AGENT_RUN_LANGFUSE_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS langfuse_trace_id VARCHAR(64)",
)

MESSAGE_AUDIT_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS operation_id VARCHAR(128)",
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS duration_ms BIGINT",
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS sequence BIGINT",
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS execution_status VARCHAR(32)",
    "ALTER TABLE IF EXISTS messages ADD COLUMN IF NOT EXISTS usage JSONB",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_run_role_operation_id "
        "ON messages(run_id, role, operation_id) WHERE operation_id IS NOT NULL"
    ),
    ("CREATE INDEX IF NOT EXISTS ix_messages_run_sequence ON messages(run_id, sequence) WHERE sequence IS NOT NULL"),
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_messages_execution_status'
              AND conrelid = 'messages'::regclass
        ) THEN
            ALTER TABLE messages
            ADD CONSTRAINT ck_messages_execution_status
            CHECK (
                execution_status IS NULL OR execution_status IN
                ('running', 'completed', 'failed', 'interrupted', 'abandoned')
            );
        END IF;
    END $$
    """,
)

AGENT_RUN_FACT_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS manifest JSONB",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS manifest_fingerprint VARCHAR(64)",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS manifest_recorded_at TIMESTAMP WITHOUT TIME ZONE",
    """
    CREATE TABLE IF NOT EXISTS agent_run_attempts (
        id SERIAL PRIMARY KEY,
        run_id VARCHAR(64) NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        attempt_no INTEGER NOT NULL,
        worker_id VARCHAR(128) NOT NULL,
        started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
        heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
        lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
        finished_at TIMESTAMP WITHOUT TIME ZONE,
        outcome VARCHAR(32),
        error_type VARCHAR(64),
        error_message TEXT,
        created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
        updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    )
    """,
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_run_attempts_run_attempt_no "
        "ON agent_run_attempts(run_id, attempt_no)"
    ),
    "CREATE INDEX IF NOT EXISTS ix_agent_run_attempts_open ON agent_run_attempts(run_id, finished_at)",
)

AGENT_RUN_TIMING_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS prepared_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS first_output_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS first_model_request_at TIMESTAMP WITHOUT TIME ZONE",
)

WORKDIR_PATH_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS projects (
        id VARCHAR(64) PRIMARY KEY,
        uid VARCHAR(64) NOT NULL CONSTRAINT fk_projects_uid_users REFERENCES users(uid) ON DELETE CASCADE,
        name VARCHAR(255),
        selection_status VARCHAR(20) NOT NULL,
        workdir_path VARCHAR(512) NOT NULL,
        directory_mode VARCHAR(20) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        deleted_at TIMESTAMP WITHOUT TIME ZONE,
        idempotency_key VARCHAR(128),
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_projects_id_uid UNIQUE (id, uid),
        CONSTRAINT uq_projects_uid_idempotency_key UNIQUE (uid, idempotency_key),
        CONSTRAINT ck_projects_selection_status CHECK (selection_status IN ('implicit', 'selectable')),
        CONSTRAINT ck_projects_directory_mode CHECK (directory_mode IN ('managed', 'linked')),
        CONSTRAINT {PROJECT_STATUS_CONSTRAINT_NAME} CHECK ({PROJECT_STATUS_CONSTRAINT_SQL})
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_projects_uid ON projects(uid)",
    "CREATE INDEX IF NOT EXISTS ix_projects_selection_status ON projects(selection_status)",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'",
    "ALTER TABLE IF EXISTS projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITHOUT TIME ZONE",
    "CREATE INDEX IF NOT EXISTS ix_projects_status ON projects(status)",
    f"""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = '{PROJECT_STATUS_CONSTRAINT_NAME}'
              AND conrelid = 'projects'::regclass
        ) THEN
            ALTER TABLE projects
            ADD CONSTRAINT {PROJECT_STATUS_CONSTRAINT_NAME} CHECK ({PROJECT_STATUS_CONSTRAINT_SQL});
        END IF;
    END $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_projects_uid_users'
              AND conrelid = 'projects'::regclass
        ) THEN
            ALTER TABLE projects
            ADD CONSTRAINT fk_projects_uid_users
            FOREIGN KEY (uid) REFERENCES users(uid) ON DELETE CASCADE;
        END IF;
    END $$
    """,
    "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS project_id VARCHAR(64)",
    "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS creation_request_id VARCHAR(64)",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_uid_creation_request_id "
        "ON conversations(uid, creation_request_id) WHERE creation_request_id IS NOT NULL"
    ),
    "ALTER TABLE IF EXISTS conversations ALTER COLUMN project_id SET NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_conversations_project_id ON conversations(project_id)",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_conversations_project_uid'
              AND conrelid = 'conversations'::regclass
        ) THEN
            ALTER TABLE conversations
            ADD CONSTRAINT fk_conversations_project_uid
            FOREIGN KEY (project_id, uid) REFERENCES projects(id, uid);
        END IF;
    END $$
    """,
)

KNOWLEDGE_FILE_TASK_OWNER_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS processing_task_id VARCHAR(64)",
    "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS processing_owner VARCHAR(128)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_files_processing_task_id ON knowledge_files(processing_task_id)",
)

TASK_DURABLE_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS handler_version INTEGER",
    "ALTER TABLE IF EXISTS tasks ALTER COLUMN handler_version SET DEFAULT 1",
    "ALTER TABLE IF EXISTS tasks ALTER COLUMN handler_version SET NOT NULL",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(64)",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS worker_id VARCHAR(128)",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS timeout_seconds DOUBLE PRECISION",
    "ALTER TABLE IF EXISTS tasks ALTER COLUMN timeout_seconds SET DEFAULT 21600.0",
    "ALTER TABLE IF EXISTS tasks ALTER COLUMN timeout_seconds SET NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_tasks_status_lease_expires ON tasks(status, lease_expires_at)",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_tasks_active_dedupe'
              AND conrelid = 'tasks'::regclass
        ) THEN
            ALTER TABLE tasks
            ADD CONSTRAINT uq_tasks_active_dedupe UNIQUE (type, dedupe_key);
        END IF;
    END $$
    """,
)

RUNTIME_SCOPE_SCHEMA_STATEMENTS = (
    "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS runtime_scope_id VARCHAR(64)",
    (
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS "
        "runtime_cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE"
    ),
    "ALTER TABLE IF EXISTS agent_runs ALTER COLUMN runtime_scope_id SET NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_runtime_scope_id ON agent_runs(runtime_scope_id)",
    ("CREATE INDEX IF NOT EXISTS ix_agent_runs_runtime_cleanup_pending ON agent_runs(runtime_cleanup_pending)"),
    f"""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = '{AGENT_RUN_SHAPE_CONSTRAINT_NAME}'
              AND conrelid = 'agent_runs'::regclass
        ) THEN
            BEGIN
                ALTER TABLE agent_runs
                ADD CONSTRAINT {AGENT_RUN_SHAPE_CONSTRAINT_NAME}
                CHECK ({AGENT_RUN_SHAPE_CONSTRAINT_SQL}) NOT VALID;
            EXCEPTION WHEN duplicate_object THEN
                NULL;
            END;
        END IF;
    END $$
    """,
)


@asynccontextmanager
async def schema_migration_lock(manager):
    """用独立 PostgreSQL session 串行化唯一 Schema migrator。"""
    manager._check_initialized()
    async with manager.async_engine.connect() as conn:
        params = {"lock_scope": "yuxi:schema-migration"}
        await conn.execute(text("SELECT pg_advisory_lock(hashtextextended(:lock_scope, 0))"), params)
        await conn.commit()
        try:
            yield
        finally:
            unlocked = await conn.scalar(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_scope, 0))"),
                params,
            )
            await conn.commit()
            if unlocked is not True:
                await conn.close()
                raise RuntimeError("Failed to release Yuxi schema migration advisory lock")


async def create_schema_version_table(manager) -> None:
    """创建轻量 Schema 版本表；仅允许迁移器调用。"""
    manager._check_initialized()
    async with manager.async_engine.begin() as conn:
        await conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} (
                    domain VARCHAR(32) PRIMARY KEY,
                    version INTEGER NOT NULL CHECK (version > 0),
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )


async def record_schema_version(manager, domain: str, version: int) -> None:
    """在对应域迁移完整成功后记录当前版本。"""
    manager._check_initialized()
    async with manager.async_engine.begin() as conn:
        await conn.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_VERSION_TABLE} (domain, version, applied_at)
                VALUES (:domain, :version, CURRENT_TIMESTAMP)
                ON CONFLICT (domain) DO UPDATE
                SET version = EXCLUDED.version, applied_at = EXCLUDED.applied_at
                """
            ),
            {"domain": domain, "version": version},
        )


async def create_knowledge_tables(manager):
    """创建知识与评估表。"""
    from yuxi.bootstrap.models import load_models

    load_models()
    manager._check_initialized()
    async with manager.async_engine.begin() as conn:
        await conn.run_sync(KnowledgeBase.metadata.create_all)
    logger.info("PostgreSQL knowledge tables created/checked")


async def create_business_tables(manager):
    """创建所有业务数据表"""
    from yuxi.bootstrap.models import load_models

    load_models()
    manager._check_initialized()
    async with manager.async_engine.begin() as conn:
        await conn.run_sync(BusinessBase.metadata.create_all)
    logger.info("PostgreSQL business tables created/checked")


async def ensure_knowledge_schema(manager):
    """确保知识库 schema 包含所有必要字段"""
    manager._check_initialized()
    stmts = [
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS embedding_model_spec VARCHAR(512)",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS llm_model_spec VARCHAR(512)",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS query_params JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS additional_params JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS share_config JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS mindmap JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS mindmap_file_ids JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS mindmap_metadata JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS sample_questions JSONB",
        "ALTER TABLE IF EXISTS knowledge_bases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS parent_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS original_filename VARCHAR(512)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS file_type VARCHAR(64)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS path VARCHAR(1024)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS minio_url VARCHAR(1024)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS markdown_file VARCHAR(1024)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS status VARCHAR(32)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS file_size BIGINT",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS chunk_count INTEGER DEFAULT 0",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS token_count BIGINT DEFAULT 0",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS content_type VARCHAR(64)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS processing_params JSONB",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS is_folder BOOLEAN",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
        *KNOWLEDGE_FILE_TASK_OWNER_SCHEMA_STATEMENTS,
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
        "ALTER TABLE IF EXISTS knowledge_files ADD COLUMN IF NOT EXISTS updated_by VARCHAR(64)",
        "ALTER TABLE IF EXISTS evaluation_datasets ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
        "ALTER TABLE IF EXISTS evaluation_datasets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS evaluation_datasets ADD COLUMN IF NOT EXISTS build_metadata JSONB",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS name VARCHAR(255)",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS metrics JSONB",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS overall_score DOUBLE PRECISION",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS total_items INTEGER",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS completed_items INTEGER",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS evaluation_runs ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
        "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS gold_chunk_ids JSONB",
        "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS gold_answer TEXT",
        "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS generated_answer TEXT",
        "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS retrieved_chunks JSONB",
        "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS metrics JSONB",
        "ALTER TABLE IF EXISTS evaluation_run_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
        """
        CREATE TABLE IF NOT EXISTS evaluation_datasets (
            id SERIAL PRIMARY KEY,
            dataset_id VARCHAR(64) NOT NULL UNIQUE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            item_count INTEGER DEFAULT 0,
            has_gold_chunks BOOLEAN DEFAULT FALSE,
            has_gold_answers BOOLEAN DEFAULT FALSE,
            build_metadata JSONB,
            created_by VARCHAR(64),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS evaluation_dataset_items (
            id SERIAL PRIMARY KEY,
            item_id VARCHAR(64) NOT NULL UNIQUE,
            dataset_id VARCHAR(64) NOT NULL REFERENCES evaluation_datasets(dataset_id) ON DELETE CASCADE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            item_index INTEGER NOT NULL,
            query_text TEXT NOT NULL,
            gold_chunk_ids JSONB,
            gold_answer TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_evaluation_dataset_items_dataset_index UNIQUE (dataset_id, item_index)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS evaluation_runs (
            id SERIAL PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            dataset_id VARCHAR(64) REFERENCES evaluation_datasets(dataset_id) ON DELETE SET NULL,
            status VARCHAR(32) DEFAULT 'running',
            retrieval_config JSONB,
            metrics JSONB,
            overall_score DOUBLE PRECISION,
            total_items INTEGER DEFAULT 0,
            completed_items INTEGER DEFAULT 0,
            started_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            created_by VARCHAR(64)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS evaluation_run_items (
            id SERIAL PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
            dataset_item_id VARCHAR(64) REFERENCES evaluation_dataset_items(item_id) ON DELETE SET NULL,
            item_index INTEGER NOT NULL,
            query_text TEXT NOT NULL,
            gold_chunk_ids JSONB,
            gold_answer TEXT,
            generated_answer TEXT,
            retrieved_chunks JSONB,
            metrics JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_evaluation_run_items_run_index UNIQUE (run_id, item_index)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id SERIAL PRIMARY KEY,
            chunk_id VARCHAR(128) NOT NULL UNIQUE,
            file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            start_char_pos INTEGER,
            end_char_pos INTEGER,
            start_token_pos INTEGER,
            end_token_pos INTEGER,
            graph_structure_indexed BOOLEAN NOT NULL DEFAULT FALSE,
            graph_indexed BOOLEAN DEFAULT FALSE,
            graph_extraction_details JSONB NOT NULL
                DEFAULT jsonb_build_object('status', 'pending', 'attempt_count', 0),
            ent_ids JSONB,
            tags JSONB,
            extraction_result JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS extraction_result JSONB",
        (
            "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS "
            "graph_structure_indexed BOOLEAN NOT NULL DEFAULT FALSE"
        ),
        (
            "ALTER TABLE IF EXISTS knowledge_chunks ADD COLUMN IF NOT EXISTS "
            "graph_extraction_details JSONB NOT NULL "
            "DEFAULT jsonb_build_object('status', 'pending', 'attempt_count', 0)"
        ),
        (
            "ALTER TABLE IF EXISTS knowledge_chunks ALTER COLUMN graph_extraction_details "
            "SET DEFAULT jsonb_build_object('status', 'pending', 'attempt_count', 0)"
        ),
        ("ALTER TABLE IF EXISTS knowledge_chunks ALTER COLUMN graph_structure_indexed SET DEFAULT FALSE"),
        """
        CREATE TABLE IF NOT EXISTS knowledge_graph_entities (
            id SERIAL PRIMARY KEY,
            entity_id VARCHAR(64) NOT NULL UNIQUE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            normalized_name VARCHAR(512) NOT NULL,
            label VARCHAR(128) NOT NULL,
            name VARCHAR(512) NOT NULL,
            attributes JSONB,
            vector_status VARCHAR(16) NOT NULL DEFAULT 'pending',
            vector_attempt_count INTEGER NOT NULL DEFAULT 0,
            vector_last_error TEXT,
            vector_next_retry_at TIMESTAMPTZ,
            vector_locked_until TIMESTAMPTZ,
            vector_lock_token VARCHAR(32),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_knowledge_graph_entities_identity UNIQUE (kb_id, normalized_name, label)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_graph_entity_mentions (
            id SERIAL PRIMARY KEY,
            entity_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_entities(entity_id) ON DELETE CASCADE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE,
            chunk_id VARCHAR(128) NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_knowledge_graph_entity_mentions_entity_chunk UNIQUE (entity_id, chunk_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_graph_triples (
            id SERIAL PRIMARY KEY,
            triple_id VARCHAR(64) NOT NULL UNIQUE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            source_entity_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_entities(entity_id) ON DELETE CASCADE,
            target_entity_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_entities(entity_id) ON DELETE CASCADE,
            relation_type VARCHAR(256) NOT NULL,
            content TEXT NOT NULL,
            vector_status VARCHAR(16) NOT NULL DEFAULT 'pending',
            vector_attempt_count INTEGER NOT NULL DEFAULT 0,
            vector_last_error TEXT,
            vector_next_retry_at TIMESTAMPTZ,
            vector_locked_until TIMESTAMPTZ,
            vector_lock_token VARCHAR(32),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_graph_triple_mentions (
            id SERIAL PRIMARY KEY,
            triple_id VARCHAR(64) NOT NULL REFERENCES knowledge_graph_triples(triple_id) ON DELETE CASCADE,
            kb_id VARCHAR(80) NOT NULL REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE,
            file_id VARCHAR(64) NOT NULL REFERENCES knowledge_files(file_id) ON DELETE CASCADE,
            chunk_id VARCHAR(128) NOT NULL REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
            text TEXT,
            extractor_type VARCHAR(128),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_knowledge_graph_triple_mentions_triple_chunk UNIQUE (triple_id, chunk_id)
        )
        """,
        "ALTER TABLE IF EXISTS knowledge_bases ALTER COLUMN kb_id TYPE VARCHAR(80)",
        "ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS vector_status VARCHAR(16)",
        (
            "ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS "
            "vector_attempt_count INTEGER NOT NULL DEFAULT 0"
        ),
        "ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS vector_last_error TEXT",
        ("ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS vector_next_retry_at TIMESTAMPTZ"),
        ("ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS vector_locked_until TIMESTAMPTZ"),
        ("ALTER TABLE IF EXISTS knowledge_graph_entities ADD COLUMN IF NOT EXISTS vector_lock_token VARCHAR(32)"),
        "ALTER TABLE IF EXISTS knowledge_graph_entities ALTER COLUMN vector_status SET DEFAULT 'pending'",
        "ALTER TABLE IF EXISTS knowledge_graph_entities ALTER COLUMN vector_status SET NOT NULL",
        ("ALTER TABLE IF EXISTS knowledge_graph_entities ALTER COLUMN vector_attempt_count SET DEFAULT 0"),
        "ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS vector_status VARCHAR(16)",
        (
            "ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS "
            "vector_attempt_count INTEGER NOT NULL DEFAULT 0"
        ),
        "ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS vector_last_error TEXT",
        ("ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS vector_next_retry_at TIMESTAMPTZ"),
        ("ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS vector_locked_until TIMESTAMPTZ"),
        "ALTER TABLE IF EXISTS knowledge_graph_triples ADD COLUMN IF NOT EXISTS vector_lock_token VARCHAR(32)",
        "ALTER TABLE IF EXISTS knowledge_graph_triples ALTER COLUMN vector_status SET DEFAULT 'pending'",
        "ALTER TABLE IF EXISTS knowledge_graph_triples ALTER COLUMN vector_status SET NOT NULL",
        ("ALTER TABLE IF EXISTS knowledge_graph_triples ALTER COLUMN vector_attempt_count SET DEFAULT 0"),
        "ALTER TABLE IF EXISTS knowledge_files ALTER COLUMN kb_id TYPE VARCHAR(80)",
        "ALTER TABLE IF EXISTS evaluation_datasets ALTER COLUMN kb_id TYPE VARCHAR(80)",
        "ALTER TABLE IF EXISTS evaluation_dataset_items ALTER COLUMN kb_id TYPE VARCHAR(80)",
        "ALTER TABLE IF EXISTS evaluation_runs ALTER COLUMN kb_id TYPE VARCHAR(80)",
        "CREATE INDEX IF NOT EXISTS idx_kb_type ON knowledge_bases(kb_type)",
        "CREATE INDEX IF NOT EXISTS idx_kb_name ON knowledge_bases(name)",
        "CREATE INDEX IF NOT EXISTS idx_kf_kb_id ON knowledge_files(kb_id)",
        "CREATE INDEX IF NOT EXISTS idx_kf_kb_filename ON knowledge_files(kb_id, filename)",
        "CREATE INDEX IF NOT EXISTS idx_kf_parent ON knowledge_files(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_kf_status ON knowledge_files(status)",
        "CREATE INDEX IF NOT EXISTS idx_kf_hash ON knowledge_files(content_hash)",
        # 虚拟目录分组索引：按路径首段聚合，避免大知识库全表扫描 + 磁盘排序
        (
            "CREATE INDEX IF NOT EXISTS idx_kf_kb_parent_segment ON knowledge_files "
            "(kb_id, parent_id, split_part(filename, '/', 1)) WHERE strpos(filename, '/') > 0"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_kf_kb_parent_flat ON knowledge_files (kb_id, parent_id) "
            "WHERE strpos(filename, '/') = 0 AND filename IS NOT NULL AND filename <> ''"
        ),
        "CREATE INDEX IF NOT EXISTS ix_evaluation_datasets_kb_id ON evaluation_datasets(kb_id)",
        (
            "CREATE INDEX IF NOT EXISTS ix_evaluation_dataset_items_dataset_index "
            "ON evaluation_dataset_items(dataset_id, item_index)"
        ),
        "CREATE INDEX IF NOT EXISTS ix_evaluation_dataset_items_kb_id ON evaluation_dataset_items(kb_id)",
        "CREATE INDEX IF NOT EXISTS ix_evaluation_runs_kb_id ON evaluation_runs(kb_id)",
        "CREATE INDEX IF NOT EXISTS ix_evaluation_runs_status ON evaluation_runs(status)",
        "CREATE INDEX IF NOT EXISTS ix_evaluation_runs_started ON evaluation_runs(started_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_evaluation_run_items_run_index ON evaluation_run_items(run_id, item_index)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_chunks_chunk_id ON knowledge_chunks(chunk_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_file_id ON knowledge_chunks(file_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_kb_id ON knowledge_chunks(kb_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_graph_indexed ON knowledge_chunks(graph_indexed)",
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_graph_structure_indexed "
            "ON knowledge_chunks(graph_structure_indexed)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_graph_extraction_status "
            "ON knowledge_chunks(kb_id, ((graph_extraction_details->>'status')))"
        ),
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_graph_entities_entity_id "
            "ON knowledge_graph_entities(entity_id)"
        ),
        "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entities_kb_id ON knowledge_graph_entities(kb_id)",
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entities_vector_pending "
            "ON knowledge_graph_entities(kb_id, vector_status, vector_next_retry_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entity_mentions_kb_id "
            "ON knowledge_graph_entity_mentions(kb_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entity_mentions_file_id "
            "ON knowledge_graph_entity_mentions(file_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_entity_mentions_chunk_id "
            "ON knowledge_graph_entity_mentions(chunk_id)"
        ),
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_graph_triples_triple_id "
            "ON knowledge_graph_triples(triple_id)"
        ),
        "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triples_kb_id ON knowledge_graph_triples(kb_id)",
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triples_vector_pending "
            "ON knowledge_graph_triples(kb_id, vector_status, vector_next_retry_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_kb_id "
            "ON knowledge_graph_triple_mentions(kb_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_file_id "
            "ON knowledge_graph_triple_mentions(file_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_knowledge_graph_triple_mentions_chunk_id "
            "ON knowledge_graph_triple_mentions(chunk_id)"
        ),
    ]

    async with manager.async_engine.begin() as conn:
        for stmt in stmts:
            await conn.execute(text(stmt))


async def ensure_business_schema(manager):
    """确保业务 schema 包含后续新增字段（运行时 schema 演进）。"""
    manager._check_initialized()
    stmts = [
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS tool_dependencies JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS mcp_dependencies JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS skill_dependencies JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS version VARCHAR(64)",
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL DEFAULT 'upload'",
        (
            "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS share_config JSONB NOT NULL "
            'DEFAULT \'{"access_level": "user", "department_ids": [], "user_uids": []}\'::jsonb'
        ),
        "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb",
        "ALTER TABLE IF EXISTS skills ALTER COLUMN share_config DROP DEFAULT",
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE IF EXISTS skills ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)",
        "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS last_viewed_run_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS app_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS mcp_servers ADD COLUMN IF NOT EXISTS env JSONB",
        """
        CREATE TABLE IF NOT EXISTS agent_envs (
            id SERIAL PRIMARY KEY,
            uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
            env JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_agent_envs_uid UNIQUE (uid)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_config (
            id SERIAL PRIMARY KEY,
            uid VARCHAR NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
            enable_memory BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_user_config_uid UNIQUE (uid)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agents (
            id SERIAL PRIMARY KEY,
            slug VARCHAR(80) NOT NULL UNIQUE,
            backend_id VARCHAR(64) NOT NULL,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            icon VARCHAR(255),
            pics JSONB NOT NULL DEFAULT '[]'::jsonb,
            config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            share_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            is_subagent BOOLEAN NOT NULL DEFAULT FALSE,
            created_by VARCHAR(64),
            updated_by VARCHAR(64),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS backend_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS share_config JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config TYPE JSONB USING share_config::jsonb",
        "ALTER TABLE IF EXISTS agents ALTER COLUMN share_config DROP DEFAULT",
        "ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS is_subagent BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE IF EXISTS user_config ADD COLUMN IF NOT EXISTS enable_memory BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE IF EXISTS api_keys ALTER COLUMN user_id SET NOT NULL",
        "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS request_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS intent_hash VARCHAR(64)",
        "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP WITHOUT TIME ZONE",
        "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS access_level VARCHAR(16) NOT NULL DEFAULT 'full'",
        "ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS app_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS user_kind VARCHAR(16) NOT NULL DEFAULT 'human'",
        "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS owner_user_id INTEGER",
        "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS app_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS end_user_id VARCHAR(128)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_public_end_user_identity "
        "ON users(owner_user_id, app_id, end_user_id)",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_users_owner_user_id'
                  AND conrelid = 'users'::regclass
            ) THEN
                ALTER TABLE users ADD CONSTRAINT fk_users_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users(id);
            END IF;
        END $$
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_users_public_end_user_shape'
                  AND conrelid = 'users'::regclass
            ) THEN
                ALTER TABLE users ADD CONSTRAINT ck_users_public_end_user_shape CHECK (
                    (user_kind = 'human' AND owner_user_id IS NULL AND app_id IS NULL AND end_user_id IS NULL)
                    OR (user_kind = 'end_user' AND owner_user_id IS NOT NULL AND app_id IS NOT NULL
                        AND end_user_id IS NOT NULL AND role = 'user')
                );
            END IF;
        END $$
        """,
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS app_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS api_key_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_app_id ON agent_runs(app_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_api_key_id ON agent_runs(api_key_id)",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_api_keys_access_level'
                  AND conrelid = 'api_keys'::regclass
            ) THEN
                ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_access_level
                CHECK (access_level IN ('full', 'agents', 'knowledge'));
            END IF;
        END $$
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_request_id ON api_keys(request_id)",
        "CREATE INDEX IF NOT EXISTS ix_api_keys_revoked_at ON api_keys(revoked_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_agents_slug ON agents(slug)",
        "CREATE INDEX IF NOT EXISTS ix_agents_backend_id ON agents(backend_id)",
        "CREATE INDEX IF NOT EXISTS ix_agents_is_subagent ON agents(is_subagent)",
        "CREATE INDEX IF NOT EXISTS ix_agents_created_by ON agents(created_by)",
        """
        CREATE TABLE IF NOT EXISTS scheduled_agent_jobs (
            id VARCHAR(64) PRIMARY KEY,
            uid VARCHAR(64) NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
            creation_request_id VARCHAR(64) NOT NULL,
            creation_intent_hash VARCHAR(64) NOT NULL,
            project_id VARCHAR(64) NOT NULL,
            agent_slug VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL,
            prompt TEXT NOT NULL,
            tool_approval_mode VARCHAR(32) NOT NULL DEFAULT 'default',
            model_spec VARCHAR(512),
            cron_expression VARCHAR(100) NOT NULL,
            timezone VARCHAR(64) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            deleted_at TIMESTAMP WITHOUT TIME ZONE,
            next_run_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT fk_scheduled_agent_jobs_project_uid
                FOREIGN KEY (project_id, uid) REFERENCES projects(id, uid) ON DELETE CASCADE,
            CONSTRAINT uq_scheduled_agent_jobs_uid_creation_request
                UNIQUE (uid, creation_request_id),
            CONSTRAINT ck_scheduled_agent_jobs_tool_approval_mode
                CHECK (tool_approval_mode IN ('default', 'always_trust'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_scheduled_agent_jobs_uid ON scheduled_agent_jobs(uid)",
        "CREATE INDEX IF NOT EXISTS ix_scheduled_agent_jobs_project_id ON scheduled_agent_jobs(project_id)",
        "CREATE INDEX IF NOT EXISTS ix_scheduled_agent_jobs_deleted_at ON scheduled_agent_jobs(deleted_at)",
        "CREATE INDEX IF NOT EXISTS ix_scheduled_agent_jobs_due ON scheduled_agent_jobs(enabled, next_run_at)",
        """
        CREATE TABLE IF NOT EXISTS scheduled_agent_runs (
            id VARCHAR(64) PRIMARY KEY,
            job_id VARCHAR(64) NOT NULL REFERENCES scheduled_agent_jobs(id) ON DELETE CASCADE,
            input_id VARCHAR(64) NOT NULL,
            thread_id VARCHAR(64) NOT NULL,
            trigger VARCHAR(16) NOT NULL DEFAULT 'scheduled',
            occurrence_key VARCHAR(128) NOT NULL,
            scheduled_for TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            project_id VARCHAR(64) NOT NULL,
            agent_slug VARCHAR(64) NOT NULL,
            conversation_title VARCHAR(255) NOT NULL,
            prompt TEXT NOT NULL,
            tool_approval_mode VARCHAR(32) NOT NULL,
            model_spec VARCHAR(512),
            status VARCHAR(32) NOT NULL DEFAULT 'dispatching',
            error_message TEXT,
            created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_scheduled_agent_runs_job_occurrence UNIQUE (job_id, occurrence_key),
            CONSTRAINT uq_scheduled_agent_runs_input UNIQUE (input_id),
            CONSTRAINT uq_scheduled_agent_runs_thread UNIQUE (thread_id)
        )
        """,
        ("CREATE INDEX IF NOT EXISTS ix_scheduled_agent_runs_job_created ON scheduled_agent_runs(job_id, created_at)"),
        ("CREATE INDEX IF NOT EXISTS ix_scheduled_agent_runs_dispatching ON scheduled_agent_runs(status, created_at)"),
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_default
        ON agents(is_default)
        WHERE is_default IS TRUE
        """,
        """
        CREATE TABLE IF NOT EXISTS config_options (
            id SERIAL PRIMARY KEY,
            key VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            params JSONB NOT NULL DEFAULT '{}'::jsonb,
            value JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by VARCHAR(100),
            updated_by VARCHAR(100),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_config_options_key ON config_options(key)",
        """
        CREATE TABLE IF NOT EXISTS model_providers (
            id SERIAL PRIMARY KEY,
            provider_id VARCHAR(100) NOT NULL UNIQUE,
            display_name VARCHAR(100) NOT NULL,
            provider_type VARCHAR(32) NOT NULL DEFAULT 'openai',
            default_protocol VARCHAR(64),
            base_url VARCHAR(500) NOT NULL,
            embedding_base_url VARCHAR(500),
            rerank_base_url VARCHAR(500),
            models_endpoint VARCHAR(200),
            embedding_models_endpoint VARCHAR(200),
            rerank_models_endpoint VARCHAR(200),
            api_key_env VARCHAR(128),
            api_key VARCHAR(500),
            capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
            enabled_models JSONB NOT NULL DEFAULT '[]'::jsonb,
            headers_json JSONB,
            extra_json JSONB,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
            include_user_uid BOOLEAN NOT NULL DEFAULT FALSE,
            created_by VARCHAR(100),
            updated_by VARCHAR(100),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subagent_threads (
            id SERIAL PRIMARY KEY,
            uid VARCHAR(64) NOT NULL,
            parent_conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            child_conversation_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
            child_thread_id VARCHAR(64) NOT NULL UNIQUE,
            subagent_slug VARCHAR(64) NOT NULL,
            created_by_run_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        *WORKDIR_PATH_SCHEMA_STATEMENTS,
        (
            "ALTER TABLE IF EXISTS model_providers ADD COLUMN IF NOT EXISTS "
            "include_user_uid BOOLEAN NOT NULL DEFAULT FALSE"
        ),
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS agent_slug VARCHAR(64)",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS conversation_thread_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS created_by_run_id VARCHAR(64)",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS subagent_thread_relation_id INTEGER",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'chat'",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS channel VARCHAR(32) NOT NULL DEFAULT 'web'",
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS external_id VARCHAR(128)",
        *AGENT_RUN_LEASE_SCHEMA_STATEMENTS,
        *AGENT_RUN_LANGFUSE_SCHEMA_STATEMENTS,
        *MESSAGE_AUDIT_SCHEMA_STATEMENTS,
        *AGENT_RUN_FACT_SCHEMA_STATEMENTS,
        *AGENT_RUN_TIMING_SCHEMA_STATEMENTS,
        (
            "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS "
            "origin_metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
        ),
        ("ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS token_usage JSONB NOT NULL DEFAULT '{}'::jsonb"),
        "ALTER TABLE IF EXISTS subagent_threads ADD COLUMN IF NOT EXISTS subagent_slug VARCHAR(64)",
        "ALTER TABLE IF EXISTS subagent_threads ADD COLUMN IF NOT EXISTS created_by_run_id VARCHAR(64)",
        *RUNTIME_SCOPE_SCHEMA_STATEMENTS,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM subagent_threads WHERE subagent_slug IS NULL) THEN
                ALTER TABLE subagent_threads ALTER COLUMN subagent_slug SET NOT NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM subagent_threads WHERE created_by_run_id IS NULL) THEN
                ALTER TABLE subagent_threads ALTER COLUMN created_by_run_id SET NOT NULL;
            END IF;
        END $$;
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_uid_created ON agent_runs(uid, created_at DESC)",
        """
        CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation_thread_created
        ON agent_runs(conversation_thread_id, created_at DESC)
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_status_updated ON agent_runs(status, updated_at)",
        """
        CREATE INDEX IF NOT EXISTS ix_agent_runs_subagent_thread_relation_id
        ON agent_runs(subagent_thread_relation_id)
        """,
        "CREATE INDEX IF NOT EXISTS ix_subagent_threads_uid ON subagent_threads(uid)",
        """
        CREATE INDEX IF NOT EXISTS ix_subagent_threads_parent_conversation
        ON subagent_threads(parent_conversation_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_subagent_threads_subagent_slug
        ON subagent_threads(subagent_slug)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_subagent_threads_created_by_run_id
        ON subagent_threads(created_by_run_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_runs_created_by_run_created
        ON agent_runs(created_by_run_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_agent_runs_subagent_lookup
        ON agent_runs(uid, conversation_thread_id, run_type, created_at DESC)
        """,
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_one_active_per_thread
        ON agent_runs(uid, agent_slug, conversation_thread_id)
        WHERE status NOT IN ({AGENT_RUN_TERMINAL_STATUS_SQL})
        """,
        "CREATE INDEX IF NOT EXISTS ix_conversations_is_pinned ON conversations(is_pinned)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_model_providers_provider_id ON model_providers(provider_id)",
        "CREATE INDEX IF NOT EXISTS ix_model_providers_is_enabled ON model_providers(is_enabled)",
        "ALTER TABLE IF EXISTS conversations ADD COLUMN IF NOT EXISTS queue_paused BOOLEAN NOT NULL DEFAULT FALSE",
        *AGENT_RUN_EXECUTION_SEQ_SCHEMA_STATEMENTS,
        "ALTER TABLE IF EXISTS agent_runs ADD COLUMN IF NOT EXISTS langfuse_observation_id VARCHAR(16)",
        *TASK_DURABLE_SCHEMA_STATEMENTS,
    ]
    async with manager.async_engine.begin() as conn:
        for stmt in stmts:
            await conn.execute(text(stmt))
