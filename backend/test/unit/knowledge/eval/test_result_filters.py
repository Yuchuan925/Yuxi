from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.api.routers.knowledge.evaluation import get_evaluation_run_results
from yuxi.modules.knowledge.evaluation import service as evaluation_module
from yuxi.modules.knowledge.evaluation.service import EvaluationService


def make_item(item_index: int, *, score: float = 1.0, recall: float = 1.0):
    """构造最小逐题评估结果。"""
    return SimpleNamespace(
        item_index=item_index,
        query_text=f"问题 {item_index}",
        gold_chunk_ids=[],
        gold_answer="标准答案",
        generated_answer="生成答案",
        retrieved_chunks=[],
        metrics={"score": score, "recall@10": recall},
    )


class FakeEvaluationRepository:
    """提供筛选分页测试所需的内存仓储。"""

    def __init__(self, items):
        """保存测试结果列表。"""
        self.items = items

    async def get_run(self, _run_id):
        """返回固定的评估运行。"""
        return SimpleNamespace(
            run_id="run_1234abcd",
            kb_id="kb_test",
            name="筛选测试",
            status="completed",
            started_at=None,
            completed_at=None,
            total_items=len(self.items),
            completed_items=len(self.items),
            overall_score=1.0,
            retrieval_config={},
        )

    async def list_run_items(self, _run_id, offset=0, limit=100):
        """按偏移读取逐题结果。"""
        return self.items[offset : offset + limit]

    async def count_run_items(self, _run_id):
        """返回逐题结果总数。"""
        return len(self.items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_filter", "expected_indexes"),
    [
        ("all", [0, 1, 2]),
        ("answer_errors", [1]),
        ("errors_or_low_recall", [1, 2]),
    ],
)
async def test_get_run_results_filters_before_pagination(result_filter, expected_indexes):
    """筛选必须先于分页并返回筛选后的总数。"""
    service = EvaluationService.__new__(EvaluationService)
    service.eval_repo = FakeEvaluationRepository([make_item(0), make_item(1, score=0.5), make_item(2, recall=0.99)])

    result = await service.get_run_results("kb_test", "run_1234abcd", page=1, page_size=2, result_filter=result_filter)

    assert [item["item_index"] for item in result["items"]] == expected_indexes[:2]
    assert result["pagination"]["total"] == len(expected_indexes)
    assert result["pagination"]["result_filter"] == result_filter


@pytest.mark.asyncio
async def test_router_rejects_unknown_result_filter_before_service_call():
    """路由在调用服务前拒绝未知筛选值。"""
    with pytest.raises(HTTPException) as exc_info:
        await get_evaluation_run_results(
            "kb_test",
            "run_1234abcd",
            result_filter="unknown",
            current_user=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "无效的评估结果筛选条件"


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, SimpleNamespace(kb_id="other_kb")], ids=["missing", "foreign"])
async def test_run_results_reject_missing_or_foreign_run_even_with_matching_job(monkeypatch, row):
    """后台作业不能替代所属知识库的评估运行。"""

    class RunRepository:
        async def get_run(self, run_id):
            """返回缺失或其他知识库的运行。"""
            return row

    async def matching_job(job_id):
        """制造可触发旧兜底的同名后台作业。"""
        return {"id": job_id, "status": "running", "progress": 50, "message": "private job"}

    monkeypatch.setattr(evaluation_module.job_tracker, "get_job", matching_job)
    service = EvaluationService.__new__(EvaluationService)
    service.eval_repo = RunRepository()

    with pytest.raises(ValueError, match="Run not found"):
        await service.get_run_results("kb_test", "run_1234abcd")
