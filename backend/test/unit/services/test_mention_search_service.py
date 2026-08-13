from __future__ import annotations

import base64

import ormsgpack
import pytest

import yuxi.services.mention_search_service as mention_service


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.expire_calls: dict[str, int] = {}
        self.delete_calls: list[str] = []

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value
        if ex is not None:
            self.expire_calls[key] = ex

    async def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        self.data.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()

    async def get_redis():
        return redis

    monkeypatch.setattr(mention_service, "get_redis_client", get_redis)
    return redis


@pytest.fixture
def fake_indexes(monkeypatch: pytest.MonkeyPatch):
    workspace_entries = [("guide.md", "guide.md"), ("test", "test/"), ("test_auth.py", "test/test_auth.py")]
    thread_entries = [
        {"name": "report.md", "path": "/home/gem/user-data/uploads/report.md", "is_dir": False},
        {"name": "reports", "path": "/home/gem/user-data/uploads/reports/", "is_dir": True},
    ]

    async def list_workspace(_uid: str):
        return workspace_entries

    async def list_thread(_thread_id: str, _path: str, *, recursive: bool = False):
        return thread_entries

    monkeypatch.setattr("yuxi.services.workspace_service.list_workspace_index_entries", list_workspace)
    monkeypatch.setattr("yuxi.services.thread_files_service.list_thread_object_entries", list_thread)
    return workspace_entries, thread_entries


@pytest.mark.asyncio
async def test_mention_indexes_use_filestore_lists_and_keep_separate_redis_caches(fake_indexes, fake_redis):
    index = await mention_service.get_or_build_file_index("thread_1", "user_1")

    assert {(name, source) for name, _path, source in index} == {
        ("report.md", "thread"),
        ("reports", "thread"),
        ("guide.md", "workspace"),
        ("test", "workspace"),
        ("test_auth.py", "workspace"),
    }
    workspace_key = f"{mention_service.WORKSPACE_CACHE_PREFIX}user_1"
    thread_key = f"{mention_service.THREAD_CACHE_PREFIX}thread_1"
    assert workspace_key in fake_redis.data
    assert thread_key in fake_redis.data
    assert ormsgpack.unpackb(base64.b64decode(fake_redis.data[thread_key]))


@pytest.mark.asyncio
async def test_mention_cache_hit_does_not_relist(fake_indexes, fake_redis, monkeypatch):
    await mention_service.get_or_build_file_index("thread_1", "user_1")

    async def fail(*_args, **_kwargs):
        raise AssertionError("cache miss")

    monkeypatch.setattr("yuxi.services.workspace_service.list_workspace_index_entries", fail)
    monkeypatch.setattr("yuxi.services.thread_files_service.list_thread_object_entries", fail)
    assert await mention_service.get_or_build_file_index("thread_1", "user_1")


@pytest.mark.asyncio
async def test_search_preserves_source_priority_and_ranking(fake_indexes, fake_redis):
    results = await mention_service.search_mention_files_in_index("thread_1", "user_1", "report")

    assert [result["source"] for result in results] == ["thread", "thread"]
    assert {result["path"] for result in results} == {
        "/home/gem/user-data/uploads/report.md",
        "/home/gem/user-data/uploads/reports/",
    }

    directory_results = await mention_service.search_mention_files_in_index("thread_1", "user_1", "test")
    assert directory_results[0]["name"] == "test"
    assert directory_results[0]["is_dir"] is True


@pytest.mark.asyncio
async def test_search_without_thread_only_returns_workspace(fake_indexes, fake_redis):
    results = await mention_service.search_mention_files_in_index(None, "user_1", "report")
    assert results == []
