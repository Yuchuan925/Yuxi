from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import ormsgpack
import pytest

import yuxi.services.mention_search_service as mention_service


class _FakeRedis:
    def __init__(self) -> None:
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
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shared" / "user-1" / "workspace"
    root.mkdir(parents=True)
    monkeypatch.setattr(mention_service, "user_workspace_dir", lambda _uid: root)
    return root


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()

    async def mock_get_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(mention_service, "get_redis_client", mock_get_redis)
    return redis


def test_scan_prunes_excluded_and_deep_directories(workspace: Path) -> None:
    (workspace / "main.py").write_text("main", encoding="utf-8")
    excluded = workspace / ".git"
    excluded.mkdir()
    (excluded / "config").write_text("secret", encoding="utf-8")
    deep = workspace
    for index in range(18):
        deep = deep / f"dir-{index}"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("deep", encoding="utf-8")

    names = {name for name, _path in mention_service._scan_pruned_files(workspace, 1000)}

    assert "main.py" in names
    assert "config" not in names
    assert "deep.py" not in names


def test_scan_limits_flat_directory_width(workspace: Path) -> None:
    for index in range(600):
        (workspace / f"file-{index}.py").write_text(str(index), encoding="utf-8")

    assert len(mention_service._scan_pruned_files(workspace, 1000)) == 500


@pytest.mark.asyncio
async def test_workspace_index_cache_lifecycle(workspace: Path, fake_redis: _FakeRedis) -> None:
    (workspace / "main.py").write_text("main", encoding="utf-8")

    first = await mention_service.get_or_build_workspace_index("user-1")
    cache_key = f"{mention_service.WORKSPACE_CACHE_PREFIX}user-1"
    cached = ormsgpack.unpackb(base64.b64decode(fake_redis.data[cache_key]))
    assert first == [("main.py", "main.py")]
    assert cached == [["main.py", "main.py"]]

    (workspace / "new.py").write_text("new", encoding="utf-8")
    assert len(await mention_service.get_or_build_workspace_index("user-1")) == 1

    await mention_service.invalidate_workspace_mention_cache("user-1")
    refreshed = await mention_service.get_or_build_workspace_index("user-1")
    assert {name for name, _path in refreshed} == {"main.py", "new.py"}
    assert fake_redis.delete_calls == [cache_key]


@pytest.mark.asyncio
async def test_search_workspace_files_is_case_insensitive_and_ranks_directories(
    workspace: Path,
    fake_redis: _FakeRedis,
) -> None:
    test_dir = workspace / "test"
    test_dir.mkdir()
    (test_dir / "test_auth.py").write_text("auth", encoding="utf-8")
    (test_dir / "conftest.py").write_text("conf", encoding="utf-8")
    (workspace / "MAIN.py").write_text("main", encoding="utf-8")

    results = await mention_service.search_mention_files_in_index("user-1", "test")
    main_results = await mention_service.search_mention_files_in_index("user-1", "main")

    assert [item["name"] for item in results] == ["test", "test_auth.py", "conftest.py"]
    assert results[0] == {
        "name": "test",
        "path": "/home/gem/user-data/test/",
        "is_dir": True,
        "source": "workspace",
    }
    assert main_results[0]["name"] == "MAIN.py"
    assert main_results[0]["path"] == "/home/gem/user-data/MAIN.py"


@pytest.mark.asyncio
async def test_empty_query_does_not_scan_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    async def must_not_scan(_uid: str):
        pytest.fail("空查询不得扫描 Workspace")

    monkeypatch.setattr(mention_service, "get_or_build_workspace_index", must_not_scan)

    assert await mention_service.search_mention_files_in_index("user-1", "") == []


@pytest.mark.asyncio
async def test_search_mentions_orchestrates_project_and_workspace_in_service(monkeypatch: pytest.MonkeyPatch) -> None:
    class Repository:
        def __init__(self, db):
            assert db == "db"

        async def get_conversation_by_thread_id(self, thread_id):
            assert thread_id == "thread-1"
            return SimpleNamespace(uid="user-1", status="active")

    async def viewer(**kwargs):
        assert kwargs["thread_id"] == "thread-1"
        return {
            "entries": [
                {
                    "name": "outputs",
                    "path": "/home/gem/user-data/projects/1/outputs/",
                    "is_dir": True,
                }
            ]
        }

    async def workspace_search(*, uid, query):
        assert (uid, query) == ("user-1", "out")
        return []

    monkeypatch.setattr(mention_service, "ConversationRepository", Repository)
    monkeypatch.setattr(mention_service, "search_viewer_files", viewer)
    monkeypatch.setattr(mention_service, "search_mention_files_in_index", workspace_search)

    result = await mention_service.search_mentions(
        thread_id="thread-1",
        query="out",
        sources=None,
        current_user=SimpleNamespace(uid="user-1"),
        db="db",
    )

    assert result == [
        {
            "name": "outputs",
            "path": "/home/gem/user-data/projects/1/outputs/",
            "is_dir": True,
            "source": "thread",
        }
    ]
