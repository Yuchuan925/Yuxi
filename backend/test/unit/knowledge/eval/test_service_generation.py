from types import SimpleNamespace

import pytest

from yuxi.knowledge.eval import service as eval_service_module
from yuxi.knowledge.eval.service import EvaluationService


class FakeEvaluationRepository:
    def __init__(self):
        self.created_dataset = None
        self.updated_dataset = None

    async def create_dataset(self, payload):
        self.created_dataset = payload

    async def update_dataset(self, dataset_id, payload):
        self.updated_dataset = (dataset_id, payload)


class FakeChunkRepository:
    def __init__(self, indexed_count):
        self.indexed_count = indexed_count

    async def count_graph_indexed_by_kb_id(self, kb_id):
        return self.indexed_count


@pytest.mark.asyncio
async def test_generate_dataset_saves_generation_params(monkeypatch):
    async def fake_enqueue(**kwargs):
        return SimpleNamespace(id="task_1")

    monkeypatch.setattr(eval_service_module.tasker, "enqueue", fake_enqueue)
    service = EvaluationService()
    service.eval_repo = FakeEvaluationRepository()
    service.chunk_repo = FakeChunkRepository(indexed_count=1)

    result = await service.generate_dataset(
        kb_id="db_1",
        name="dataset",
        description="desc",
        count=2,
        neighbors_count=3,
        concurrency_count=4,
        llm_model_spec="test:model",
        generation_mode="graph_enhanced",
        graph_expand_top_k=2,
        created_by="user_1",
    )

    assert result["task_id"] == "task_1"
    params = service.eval_repo.created_dataset["build_metadata"]["params"]
    assert params["generation_mode"] == "graph_enhanced"
    assert params["graph_expand_top_k"] == 2
    updated_metadata = service.eval_repo.updated_dataset[1]["build_metadata"]
    assert updated_metadata["params"] == params


@pytest.mark.asyncio
async def test_generate_dataset_rejects_graph_mode_without_indexed_chunks():
    service = EvaluationService()
    service.eval_repo = FakeEvaluationRepository()
    service.chunk_repo = FakeChunkRepository(indexed_count=0)

    with pytest.raises(ValueError, match="尚未完成图索引"):
        await service.generate_dataset(
            kb_id="db_1",
            name="dataset",
            description="desc",
            count=2,
            neighbors_count=3,
            concurrency_count=4,
            llm_model_spec="test:model",
            generation_mode="graph_enhanced",
            graph_expand_top_k=1,
            created_by="user_1",
        )

    assert service.eval_repo.created_dataset is None
