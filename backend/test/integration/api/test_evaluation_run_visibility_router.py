"""真实 HTTP 与 PostgreSQL 的评估运行归属边界。"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.background_jobs.models import BackgroundJobRecord
from yuxi.modules.knowledge.models import EvaluationRun, KnowledgeBase

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture(autouse=True)
async def close_postgres_pool():
    """每个用例结束后释放当前事件循环的连接。"""
    yield
    await pg_manager.close()


@pytest.mark.parametrize("foreign_run", [False, True], ids=["missing", "foreign"])
async def test_evaluation_results_reject_job_fallback(test_client, admin_headers, knowledge_database, foreign_run):
    """缺失或跨库运行返回 404，同名作业不能代替评估结果。"""
    run_id = f"run_{uuid4().hex[:8]}"
    foreign_kb_id = f"pytest_visibility_{uuid4().hex}"
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(
                BackgroundJobRecord(
                    id=run_id,
                    name="private background job",
                    type="rag_evaluation",
                    status="success",
                    message="private job result",
                )
            )
            if foreign_run:
                session.add(KnowledgeBase(kb_id=foreign_kb_id, name=foreign_kb_id, kb_type="milvus"))
                await session.flush()
                session.add(EvaluationRun(run_id=run_id, kb_id=foreign_kb_id, name="private run", status="completed"))

        response = await test_client.get(
            f"/api/evaluation/databases/{knowledge_database['kb_id']}/runs/{run_id}",
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": f"Run not found for {run_id}"}
    finally:
        async with pg_manager.get_async_session_context() as session:
            await session.execute(delete(BackgroundJobRecord).where(BackgroundJobRecord.id == run_id))
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.kb_id == foreign_kb_id))
