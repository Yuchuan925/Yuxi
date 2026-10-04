import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.modules.knowledge.manager import KnowledgeBaseManager
from yuxi.modules.knowledge.read_models import KnowledgeBaseConfig

pytestmark = pytest.mark.asyncio


async def test_executor_construction_is_offloaded_from_event_loop(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    event_loop_thread = threading.get_ident()
    construction_threads: list[int] = []
    executor = object()

    def create_executor(_kb_type: str, _work_dir: str):
        construction_threads.append(threading.get_ident())
        return executor

    monkeypatch.setattr("yuxi.modules.knowledge.manager.KnowledgeBaseFactory.create", create_executor)

    assert await manager._get_or_create_kb_instance("fake") is executor
    assert construction_threads and construction_threads[0] != event_loop_thread


async def test_concurrent_executor_construction_is_deduplicated(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    construction_count = 0
    executor = object()

    def create_executor(_kb_type: str, _work_dir: str):
        nonlocal construction_count
        construction_count += 1
        return executor

    monkeypatch.setattr("yuxi.modules.knowledge.manager.KnowledgeBaseFactory.create", create_executor)

    results = await asyncio.gather(
        manager._get_or_create_kb_instance("fake"),
        manager._get_or_create_kb_instance("fake"),
    )

    assert results == [executor, executor]
    assert construction_count == 1


async def test_delete_database_commits_tombstone_before_any_external_cleanup(tmp_path, monkeypatch):
    """删除请求只提交事实；外部清理通过 outbox，不阻塞不可见提交。"""
    manager = KnowledgeBaseManager(str(tmp_path))
    repository = SimpleNamespace(mark_deleted_by_kb_id=AsyncMock(return_value=["database:kb_1:deleted"]))
    monkeypatch.setattr(
        "yuxi.modules.knowledge.repositories.files.KnowledgeFileRepository",
        lambda: repository,
    )
    executor = AsyncMock(side_effect=RuntimeError("Milvus offline"))
    monkeypatch.setattr(manager, "get_kb_executor", executor)
    result = await manager.delete_database("kb_1")
    assert result == {"message": "删除成功"}
    repository.mark_deleted_by_kb_id.assert_awaited_once_with("kb_1")
    executor.assert_not_awaited()


@pytest.mark.parametrize("refresh_stats_fails", [False, True])
async def test_parse_file_refreshes_stats_and_keeps_original_error_after_executor_failure(
    tmp_path, monkeypatch, refresh_stats_fails
):
    manager = KnowledgeBaseManager(str(tmp_path))
    refreshed = []

    class FakeExecutor:
        async def parse_file(self, *args, **kwargs):
            raise ValueError("parse failed")

    async def get_kb_config(_kb_id: str):
        return KnowledgeBaseConfig(kb_id="kb_1", kb_type="fake")

    if refresh_stats_fails:

        async def refresh_database_stats(_kb_id: str):
            raise RuntimeError("stats failed")
    else:

        async def refresh_database_stats(kb_id: str):
            refreshed.append(kb_id)
            return {}

    monkeypatch.setattr(manager, "get_kb_config", get_kb_config)
    monkeypatch.setattr(manager, "_get_or_create_kb_instance", AsyncMock(return_value=FakeExecutor()))
    monkeypatch.setattr(manager, "_refresh_database_stats", refresh_database_stats)

    with pytest.raises(ValueError, match="parse failed"):
        await manager.parse_file("kb_1", "file_1")

    if not refresh_stats_fails:
        assert refreshed == ["kb_1"]


async def test_update_query_params_delegates_persistence_to_manager(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    calls = []

    class FakeRepository:
        async def merge_query_params_options(self, kb_id: str, params: dict):
            calls.append((kb_id, params))
            return object()

    monkeypatch.setattr(
        "yuxi.modules.knowledge.repositories.bases.KnowledgeBaseRepository",
        FakeRepository,
    )

    await manager.update_kb_query_params("kb_1", {"top_k": 5})

    assert calls == [("kb_1", {"top_k": 5})]


async def test_consistency_check_delegates_type_resources_to_executor(tmp_path, monkeypatch):
    manager = KnowledgeBaseManager(str(tmp_path))
    calls = []

    class FakeExecutor:
        async def detect_data_inconsistencies(self, known_kb_ids: set[str], managed_kb_ids: set[str]):
            calls.append((known_kb_ids, managed_kb_ids))
            return {"missing_collections": [], "missing_files": []}

    class FakeRepository:
        async def get_all(self):
            return [
                SimpleNamespace(kb_id="kb_1", kb_type="milvus"),
                SimpleNamespace(kb_id="kb_2", kb_type="dify"),
            ]

    manager.kb_instances["milvus"] = FakeExecutor()
    monkeypatch.setattr(
        "yuxi.modules.knowledge.repositories.bases.KnowledgeBaseRepository",
        FakeRepository,
    )

    result = await manager.detect_data_inconsistencies()

    assert calls == [({"kb_1", "kb_2"}, {"kb_1"})]
    assert result["total_missing_collections"] == 0
    assert result["total_missing_files"] == 0
