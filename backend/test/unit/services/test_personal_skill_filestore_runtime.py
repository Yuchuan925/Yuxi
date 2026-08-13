from pathlib import Path

import pytest

from yuxi.agents.skills import service
from yuxi.storage.filestore import LocalFileStore, user_workspace_key


class _RedisLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Redis:
    def __init__(self):
        self.values = {}

    def lock(self, *_args, **_kwargs):
        return _RedisLock()

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **_kwargs):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)


def _write_skill(root: Path, slug: str = "demo-skill") -> Path:
    source = root / slug
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: demo\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    (source / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return source


@pytest.mark.asyncio
async def test_personal_skill_install_runtime_and_delete_use_filestore(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    redis = _Redis()
    source = _write_skill(tmp_path / "source")
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    async def get_redis():
        return redis

    monkeypatch.setattr(service, "get_file_store", lambda: store)
    monkeypatch.setattr(service, "get_async_redis_client", get_redis)
    monkeypatch.setattr(service.tempfile, "gettempdir", lambda: str(runtime))

    installed = await service.install_personal_skill_dir("user-1", source)
    listed = await service.list_personal_skills("user-1", refresh=True)
    loaded = await service.read_personal_skill_file("user-1", "demo-skill", "scripts/run.py")

    assert installed.slug == "demo-skill"
    assert [item.slug for item in listed.items] == ["demo-skill"]
    assert loaded == {"path": "scripts/run.py", "content": "print('ok')\n"}
    assert (await store.read(user_workspace_key("user-1", "agents/skills/demo-skill/SKILL.md"))).data.startswith(
        b"---"
    )

    snapshot = await service.delete_personal_skill("user-1", "demo-skill")

    assert snapshot.items == []
    assert await store.list(user_workspace_key("user-1", "agents/skills/demo-skill/.keep").removesuffix(".keep")) == []


@pytest.mark.asyncio
async def test_personal_skill_install_rejects_conflict_symlink_and_metadata(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    redis = _Redis()

    async def get_redis():
        return redis

    monkeypatch.setattr(service, "get_file_store", lambda: store)
    monkeypatch.setattr(service, "get_async_redis_client", get_redis)
    monkeypatch.setattr(service.tempfile, "gettempdir", lambda: str(tmp_path / "runtime"))

    source = _write_skill(tmp_path / "source")
    await service.install_personal_skill_dir("user-1", source)
    with pytest.raises(ValueError, match="已存在同名"):
        await service.install_personal_skill_dir("user-1", source)

    metadata_source = _write_skill(tmp_path / "metadata", "metadata-skill")
    (metadata_source / ".DS_Store").write_bytes(b"metadata")
    with pytest.raises(ValueError, match="系统元数据"):
        await service.install_personal_skill_dir("user-1", metadata_source)

    symlink_source = _write_skill(tmp_path / "symlink", "symlink-skill")
    (symlink_source / "linked").symlink_to(symlink_source / "SKILL.md")
    with pytest.raises(ValueError, match="符号链接"):
        await service.install_personal_skill_dir("user-1", symlink_source)


@pytest.mark.asyncio
async def test_personal_skill_install_compensates_when_cache_refresh_fails(tmp_path: Path, monkeypatch) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    redis = _Redis()
    source = _write_skill(tmp_path / "source")

    async def get_redis():
        return redis

    async def fail_cache(*_args, **_kwargs):
        raise RuntimeError("cache failed")

    monkeypatch.setattr(service, "get_file_store", lambda: store)
    monkeypatch.setattr(service, "get_async_redis_client", get_redis)
    monkeypatch.setattr(service, "_scan_and_cache_personal_skills", fail_cache)
    monkeypatch.setattr(service.tempfile, "gettempdir", lambda: str(tmp_path / "runtime"))

    with pytest.raises(RuntimeError, match="cache failed"):
        await service.install_personal_skill_dir("user-1", source)

    assert await store.list("users/user-1/workspace/agents/skills/demo-skill/") == []
