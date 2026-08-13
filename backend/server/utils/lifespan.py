import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from yuxi import get_version
from yuxi.agents.backends.sandbox import init_sandbox_provider, shutdown_sandbox_provider
from yuxi.agents.mcp.service import ensure_builtin_mcp_servers_in_db
from yuxi.config import cache as runtime_cache
from yuxi.config import config
from yuxi.knowledge.runtime import knowledge_base
from yuxi.models.providers.service import ensure_builtin_model_providers_in_db
from yuxi.services.run_queue_service import close_queue_clients, get_redis_client
from yuxi.services.task_service import tasker
from yuxi.storage.neo4j import close_shared_neo4j_connection
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan事件管理器"""
    runtime_config_ready = False

    # 初始化数据库连接
    try:
        pg_manager.initialize()
        await pg_manager.create_tables()
        await pg_manager.ensure_business_schema()
        await pg_manager.ensure_knowledge_schema()
    except Exception as e:
        logger.error(f"Failed to initialize database during startup: {e}")

    # 确保内置 MCP 服务器定义存在于数据库
    try:
        await ensure_builtin_mcp_servers_in_db()
    except Exception as e:
        logger.error(f"Failed to ensure builtin MCP servers during startup: {e}")

    try:
        from yuxi.agents.skills.service import init_builtin_skills

        async with pg_manager.get_async_session_context() as session:
            await init_builtin_skills(session)
    except Exception as e:
        logger.error(f"Failed to initialize builtin skills during startup: {e}")

    try:
        from yuxi.repositories.agent_repository import AgentRepository

        async with pg_manager.get_async_session_context() as session:
            repository = AgentRepository(session)
            await repository.ensure_default_agent()
            await repository.ensure_general_purpose_subagent()
            await repository.ensure_web_search_subagent()
            await repository.ensure_deep_research_agents()
    except Exception as e:
        logger.error(f"Failed to ensure default agent during startup: {e}")

    # 初始化内置模型供应商配置
    try:
        async with pg_manager.get_async_session_context() as session:
            await ensure_builtin_model_providers_in_db(session)
    except Exception as e:
        logger.error(f"Failed to ensure builtin model providers during startup: {e}")

    # 初始化模型缓存（v2 模型选择使用）
    try:
        from yuxi.models.providers.cache import model_cache
        from yuxi.models.providers.service import get_all_model_providers

        async with pg_manager.get_async_session_context() as session:
            providers = await get_all_model_providers(session)
            model_cache.rebuild(providers)
    except Exception as e:
        logger.error(f"Failed to initialize model cache during startup: {e}")

    try:
        from yuxi.config.options import ensure_options_in_db, load_system_config_snapshot

        async with pg_manager.get_async_session_context() as session:
            await ensure_options_in_db(session)
            _values, version = await load_system_config_snapshot(session, config)
        runtime_cache.save_runtime_config(config, version=version, updated_at=version)
        runtime_config_ready = True
    except Exception as e:
        logger.error(f"Failed to initialize config options during startup: {e}")

    # 初始化知识库管理器
    if os.environ.get("LITE_MODE", "").lower() in ("true", "1"):
        logger.info("LITE_MODE enabled, skipping knowledge base initialization")
    else:
        try:
            await knowledge_base.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize knowledge base manager: {e}")

    # 预热 Redis（run 队列）
    try:
        redis = await get_redis_client()
        await redis.ping()
    except Exception as e:
        logger.warning(f"Run queue redis unavailable on startup: {e}")

    # 只有 PostgreSQL 事实加载成功后才允许 Redis 快照更新内存。
    if runtime_config_ready:
        config.start_runtime_sync()

    try:
        init_sandbox_provider()
    except Exception as e:
        logger.error(f"Failed to initialize sandbox provider during startup: {e}")

    if os.getenv("LANGGRAPH_CHECKPOINTER_BACKEND", "postgres").strip().lower() == "postgres":
        await pg_manager.setup_langgraph_checkpointer()

    await tasker.start()
    logger.info(f"""

░██     ░██                       ░██
 ░██   ░██
  ░██ ░██   ░██    ░██ ░██    ░██ ░██
   ░████    ░██    ░██  ░██  ░██  ░██
    ░██     ░██    ░██   ░█████   ░██
    ░██     ░██   ░███  ░██  ░██  ░██
    ░██      ░█████░██ ░██    ░██ ░██  v{get_version()}

    """)
    logger.info("Yuxi backend startup complete")
    yield
    await tasker.shutdown()
    shutdown_sandbox_provider()
    await close_queue_clients()
    close_shared_neo4j_connection()
    await pg_manager.close()
