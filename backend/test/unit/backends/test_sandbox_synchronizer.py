from __future__ import annotations

import asyncio
import base64
import time
import uuid
from types import SimpleNamespace

import pytest
from yuxi.agents.backends.sandbox import synchronizer as sync_module
from yuxi.agents.backends.sandbox.synchronizer import SandboxFileSynchronizer
from yuxi.storage.filestore.models import ObjectStat, StoredObject


class FakeStore:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.put_calls: list[str] = []
        self.delete_calls: list[str] = []

    async def list(self, prefix: str = ""):
        return [
            ObjectStat(key=key, size=len(data), modified=None, content_type=None)
            for key, data in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    async def read(self, key: str):
        data = self.objects[key]
        return StoredObject(key=key, data=data, size=len(data), modified=None, content_type=None)

    async def put(self, key: str, data: bytes, *, content_type=None):
        self.put_calls.append(key)
        self.objects[key] = data

    async def delete(self, key: str):
        self.delete_calls.append(key)
        self.objects.pop(key, None)


class FakeClient:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.file = SimpleNamespace(
            list_path=self.list_path,
            download_file=self.download_file,
            write_file=self.write_file,
            read_file=self.read_file,
        )
        self.shell = SimpleNamespace(exec_command=self.exec_command)

    def list_path(self, *, path, **_kwargs):
        prefix = path.rstrip("/") + "/"
        entries = []
        for file_path, data in sorted(self.files.items()):
            if not file_path.startswith(prefix) or file_path == "/home/gem/.yuxi-sync/manifest.json":
                continue
            relative = file_path.removeprefix(prefix)
            if "/" not in relative:
                entries.append(SimpleNamespace(path=file_path, is_directory=False, size=len(data)))
        return SimpleNamespace(success=True, data=SimpleNamespace(files=entries), message=None)

    def download_file(self, *, path, **_kwargs):
        return iter([self.files[path]])

    def write_file(self, *, file, content, encoding, **_kwargs):
        self.files[file] = base64.b64decode(content) if encoding == "base64" else content.encode()
        return SimpleNamespace(success=True, message=None)

    def read_file(self, *, file, **_kwargs):
        return SimpleNamespace(data=SimpleNamespace(content=self.files[file].decode()))

    def exec_command(self, *, command, **_kwargs):
        if "os.unlink" in command:
            path = base64.b64decode(command.split("base64.b64decode('")[1].split("'")[0]).decode()
            self.files.pop(path, None)
        return SimpleNamespace(data=SimpleNamespace(exit_code=0, output=""))


class FakeProvider:
    def __init__(self, client: FakeClient):
        self.client = client

    def replace_skills(self, sandbox_id: str, files: dict[str, bytes]) -> None:
        assert sandbox_id == "sandbox-1"
        for path in list(self.client.files):
            if path.startswith("/home/gem/skills/"):
                del self.client.files[path]
        self.client.files.update({f"/home/gem/skills/{path}": data for path, data in files.items()})


class FakeRedisLock:
    def __init__(self, *, acquired: bool = True, extend_error: BaseException | None = None):
        self.acquired = acquired
        self.extend_error = extend_error
        self.released = False
        self.extend_calls = 0

    def acquire(self, *, blocking):
        assert blocking is True
        return self.acquired

    def release(self):
        self.released = True

    def extend(self, additional_time, *, replace_ttl):
        assert additional_time > 0
        assert replace_ttl is True
        self.extend_calls += 1
        if self.extend_error is not None:
            raise self.extend_error
        return True


class FakeRedis:
    def __init__(self, *, acquired: bool = True, extend_error: BaseException | None = None):
        self.acquired = acquired
        self.extend_error = extend_error
        self.keys: list[str] = []
        self.locks: list[FakeRedisLock] = []
        self.closed = False

    def lock(self, key, **_kwargs):
        if "thread_local" in _kwargs:
            assert _kwargs["thread_local"] is False
        self.keys.append(key)
        lock = FakeRedisLock(acquired=self.acquired, extend_error=self.extend_error)
        self.locks.append(lock)
        return lock

    def close(self):
        self.closed = True


def test_synchronizer_projects_sources_and_writes_back_deletions(monkeypatch):
    store = FakeStore(
        {
            "users/user-1/workspace/keep.txt": b"keep",
            "threads/file-1/uploads/input.txt": b"input",
            "threads/file-1/outputs/old.txt": b"old",
            "threads/skills-1/skills/SKILL.md": b"# skill",
        }
    )
    monkeypatch.setattr("yuxi.agents.backends.sandbox.synchronizer.get_file_store", lambda: store)
    client = FakeClient()
    monkeypatch.setattr(sync_module, "get_sandbox_provider", lambda: FakeProvider(client), raising=False)
    monkeypatch.setattr("yuxi.agents.backends.sandbox.provider.get_sandbox_provider", lambda: FakeProvider(client))
    synchronizer = SandboxFileSynchronizer(
        uid="user-1", file_thread_id="file-1", skills_thread_id="skills-1", sandbox_id="sandbox-1"
    )

    synchronizer.initialize(client)
    assert client.files["/home/gem/user-data/uploads/input.txt"] == b"input"
    assert client.files["/home/gem/skills/SKILL.md"] == b"# skill"
    assert not any(path.startswith("/home/gem/.yuxi-skills-rw") for path in client.files)
    assert "/home/gem/.yuxi-sync/initialized" in client.files

    del client.files["/home/gem/user-data/outputs/old.txt"]
    client.files["/home/gem/user-data/workspace/keep.txt"] = b"changed"
    sync_module._operation_state.lease_guard = sync_module.RedisLeaseGuard("test")
    try:
        synchronizer.sync_back(client)
    finally:
        sync_module._operation_state.lease_guard = None

    assert "threads/file-1/outputs/old.txt" not in store.objects
    assert store.objects["users/user-1/workspace/keep.txt"] == b"changed"
    assert store.objects["threads/file-1/uploads/input.txt"] == b"input"


def test_scan_applies_file_and_byte_limits_per_scope(monkeypatch):
    client = FakeClient()
    synchronizer = SandboxFileSynchronizer(
        uid="user-1", file_thread_id="file-1", skills_thread_id="skills-1", sandbox_id="sandbox-1"
    )
    monkeypatch.setattr(sync_module, "MAX_SYNC_BYTES", 5)

    client.files = {
        "/home/gem/user-data/workspace/a.txt": b"12345",
        "/home/gem/user-data/uploads/b.txt": b"12345",
        "/home/gem/user-data/outputs/c.txt": b"12345",
        "/home/gem/skills/d.txt": b"12345",
    }
    manifest = synchronizer._scan(client)
    assert len(manifest) == 4

    client.files["/home/gem/user-data/workspace/too-large.txt"] = b"1"
    with pytest.raises(ValueError, match="workspace.*每个 scope"):
        synchronizer._scan(client)


def test_operation_acquires_uid_then_file_thread_redis_locks(monkeypatch):
    store = FakeStore({})
    redis = FakeRedis()
    monkeypatch.setattr(sync_module, "get_file_store", lambda: store)
    monkeypatch.setattr(sync_module, "create_sync_redis_client", lambda: redis)
    monkeypatch.setattr(
        "yuxi.agents.backends.sandbox.provider.get_sandbox_provider",
        lambda: FakeProvider(FakeClient()),
    )
    synchronizer = SandboxFileSynchronizer(
        uid="user-1", file_thread_id="file-1", skills_thread_id="skills-1", sandbox_id="sandbox-1"
    )

    with synchronizer.operation(FakeClient(), timeout_seconds=180):
        pass

    assert redis.keys == [
        "yuxi:sandbox-files:uid:user-1",
        "yuxi:sandbox-files:file-thread:file-1",
    ]
    assert redis.closed is True


def test_operation_fails_explicitly_when_distributed_lock_is_unavailable(monkeypatch):
    redis = FakeRedis(acquired=False)
    monkeypatch.setattr(sync_module, "create_sync_redis_client", lambda: redis)
    synchronizer = SandboxFileSynchronizer(
        uid="user-1", file_thread_id="file-1", skills_thread_id="skills-1", sandbox_id="sandbox-1"
    )

    with pytest.raises(RuntimeError, match="Redis 分布式锁获取或续租失败"):
        with synchronizer.operation(FakeClient(), timeout_seconds=180):
            pass


@pytest.mark.asyncio
async def test_async_operation_lock_acquires_uid_then_file_thread_and_reenters(monkeypatch):
    redis = FakeAsyncRedis()
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    async with sync_module.sandbox_file_operation_lock(uid="user-1", file_thread_id="file-1"):
        async with sync_module.sandbox_file_operation_lock(uid="user-1"):
            async with sync_module.file_thread_operation_lock("file-1"):
                pass

    assert redis.keys == [
        "yuxi:sandbox-files:uid:user-1",
        "yuxi:sandbox-files:file-thread:file-1",
    ]


@pytest.mark.asyncio
async def test_async_operation_lock_rejects_uid_after_file_thread(monkeypatch):
    redis = FakeAsyncRedis()
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    async with sync_module.file_thread_operation_lock("file-1"):
        with pytest.raises(RuntimeError, match="不允许在 file_thread 锁内追加 uid 锁"):
            async with sync_module.sandbox_file_operation_lock(uid="user-1"):
                pass


async def async_value(value):
    return value


class FakeAsyncRedisLock:
    def __init__(self, *, extend_error: BaseException | None = None):
        self.extend_error = extend_error
        self.released = False
        self.extend_calls = 0

    async def acquire(self, *, blocking):
        assert blocking is True
        return True

    async def release(self):
        self.released = True

    async def extend(self, additional_time, *, replace_ttl):
        assert additional_time > 0
        assert replace_ttl is True
        self.extend_calls += 1
        if self.extend_error is not None:
            raise self.extend_error
        return True


class FakeAsyncRedis:
    def __init__(self, *, extend_error: BaseException | None = None):
        self.extend_error = extend_error
        self.keys = []
        self.locks: list[FakeAsyncRedisLock] = []

    def lock(self, key, **_kwargs):
        self.keys.append(key)
        lock = FakeAsyncRedisLock(extend_error=self.extend_error)
        self.locks.append(lock)
        return lock

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_async_renewal_extends_every_lock(monkeypatch):
    redis = FakeAsyncRedis()
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    async with sync_module.renewable_async_redis_locks(
        ["lock-a", "lock-b"],
        lease_seconds=0.06,
        blocking_timeout=0.01,
        failure_message="lock failed",
    ):
        await asyncio.sleep(0.05)

    assert [lock.extend_calls for lock in redis.locks] == [2, 2]
    assert all(lock.released for lock in redis.locks)


@pytest.mark.asyncio
async def test_async_renewal_failure_is_raised_on_normal_exit(monkeypatch):
    redis = FakeAsyncRedis(extend_error=RuntimeError("extend failed"))
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    with pytest.raises(RuntimeError, match="extend failed"):
        async with sync_module.renewable_async_redis_locks(
            ["lock-a"],
            lease_seconds=0.03,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_async_business_error_remains_primary_when_renewal_fails(monkeypatch):
    redis = FakeAsyncRedis(extend_error=RuntimeError("extend failed"))
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    with pytest.raises(ValueError, match="business failed") as exc_info:
        async with sync_module.renewable_async_redis_locks(
            ["lock-a"],
            lease_seconds=0.03,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise ValueError("business failed") from None

    assert any("extend failed" in note for note in exc_info.value.__notes__)


def test_sync_renewal_extends_every_lock(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(sync_module, "create_sync_redis_client", lambda: redis)

    with sync_module.renewable_sync_redis_locks(
        ["lock-a", "lock-b"],
        lease_seconds=0.06,
        blocking_timeout=0.01,
        failure_message="lock failed",
    ):
        time.sleep(0.05)

    assert [lock.extend_calls for lock in redis.locks] == [2, 2]
    assert all(lock.released for lock in redis.locks)


def test_sync_business_error_remains_primary_when_renewal_fails(monkeypatch):
    redis = FakeRedis(extend_error=RuntimeError("extend failed"))
    monkeypatch.setattr(sync_module, "create_sync_redis_client", lambda: redis)

    with pytest.raises(ValueError, match="business failed") as exc_info:
        with sync_module.renewable_sync_redis_locks(
            ["lock-a"],
            lease_seconds=0.03,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            time.sleep(0.02)
            raise ValueError("business failed")

    assert any("extend failed" in note for note in exc_info.value.__notes__)


def test_sync_renewal_failure_fences_sync_back_writes(monkeypatch):
    store = FakeStore({})
    redis = FakeRedis(extend_error=RuntimeError("extend failed"))
    client = FakeClient()
    synchronizer = SandboxFileSynchronizer(
        uid="user-1", file_thread_id="file-1", skills_thread_id="skills-1", sandbox_id="sandbox-1"
    )
    synchronizer._initialized = True
    monkeypatch.setattr(sync_module, "SYNC_LOCK_MARGIN_SECONDS", -0.97)
    monkeypatch.setattr(sync_module, "create_sync_redis_client", lambda: redis)
    monkeypatch.setattr(sync_module, "get_file_store", lambda: store)
    monkeypatch.setattr(synchronizer, "_read_manifest", lambda _client: {})
    monkeypatch.setattr(
        synchronizer,
        "_scan",
        lambda _client: {
            "/home/gem/user-data/outputs/report.txt": {
                "scope": "outputs",
                "key": "threads/file-1/outputs/report.txt",
                "sha256": "changed",
            }
        },
    )

    with pytest.raises(RuntimeError, match="extend failed"):
        with synchronizer.operation(client, timeout_seconds=1):
            time.sleep(0.02)
            with pytest.raises(RuntimeError, match="extend failed"):
                synchronizer.sync_back(client)

    assert store.put_calls == []
    assert store.delete_calls == []


@pytest.mark.asyncio
async def test_real_redis_lock_stays_owned_beyond_original_ttl():
    from yuxi.storage.redis import create_async_redis_client

    key = f"pytest:renewable-lock:{uuid.uuid4().hex}"
    contender = await create_async_redis_client()
    try:
        async with sync_module.renewable_async_redis_locks(
            [key],
            lease_seconds=0.3,
            blocking_timeout=0.05,
            failure_message="lock failed",
        ):
            await asyncio.sleep(0.45)
            second_lock = contender.lock(key, timeout=0.3, blocking_timeout=0.05)
            assert await second_lock.acquire(blocking=True) is False
    finally:
        await contender.delete(key)
        await contender.aclose()


def test_real_redis_sync_lock_stays_owned_beyond_original_ttl():
    from yuxi.storage.redis import create_sync_redis_client

    key = f"pytest:renewable-sync-lock:{uuid.uuid4().hex}"
    contender = create_sync_redis_client()
    try:
        with sync_module.renewable_sync_redis_locks(
            [key],
            lease_seconds=0.3,
            blocking_timeout=0.05,
            failure_message="lock failed",
        ):
            time.sleep(0.45)
            second_lock = contender.lock(key, timeout=0.3, blocking_timeout=0.05)
            assert second_lock.acquire(blocking=True) is False
    finally:
        contender.delete(key)
        contender.close()


@pytest.mark.asyncio
async def test_async_renewal_failure_cancels_body_before_later_write(monkeypatch):
    redis = FakeAsyncRedis(extend_error=RuntimeError("extend failed"))
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))
    wrote_after_failure = False

    with pytest.raises(RuntimeError, match="extend failed"):
        async with sync_module.renewable_async_redis_locks(
            ["lock-a"],
            lease_seconds=0.03,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            await asyncio.sleep(1)
            wrote_after_failure = True

    assert wrote_after_failure is False
    await asyncio.sleep(0)
    assert not any(task.get_name() == "yuxi-redis-lock-renewal" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_async_external_cancellation_remains_cancelled_error(monkeypatch):
    redis = FakeAsyncRedis()
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    async def hold_lock():
        async with sync_module.renewable_async_redis_locks(
            ["lock-a"],
            lease_seconds=0.3,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            await asyncio.sleep(1)

    task = asyncio.create_task(hold_lock())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0)
    assert not any(task.get_name() == "yuxi-redis-lock-renewal" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_async_external_cancel_during_release_finishes_cleanup(monkeypatch):
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    redis = BlockingReleaseRedis(release_started, allow_release)
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    async def use_lock():
        async with sync_module.renewable_async_redis_locks(
            ["lock-a"],
            lease_seconds=0.3,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            pass

    task = asyncio.create_task(use_lock())
    await release_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert redis.locks[0].released is True
    assert redis.closed is True


@pytest.mark.asyncio
async def test_async_external_cancel_during_aclose_finishes_cleanup(monkeypatch):
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    redis = BlockingCloseRedis(close_started, allow_close)
    monkeypatch.setattr(sync_module, "create_async_redis_client", lambda: async_value(redis))

    async def use_lock():
        async with sync_module.renewable_async_redis_locks(
            ["lock-a"],
            lease_seconds=0.3,
            blocking_timeout=0.01,
            failure_message="lock failed",
        ):
            pass

    task = asyncio.create_task(use_lock())
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert redis.locks[0].released is True
    assert redis.closed is True


class BlockingReleaseLock(FakeAsyncRedisLock):
    def __init__(self, release_started: asyncio.Event, allow_release: asyncio.Event):
        super().__init__()
        self._release_started = release_started
        self._allow_release = allow_release

    async def release(self):
        self._release_started.set()
        await self._allow_release.wait()
        await super().release()


class BlockingReleaseRedis(FakeAsyncRedis):
    def __init__(self, release_started: asyncio.Event, allow_release: asyncio.Event):
        super().__init__()
        self._release_started = release_started
        self._allow_release = allow_release
        self.closed = False

    def lock(self, key, **_kwargs):
        self.keys.append(key)
        lock = BlockingReleaseLock(self._release_started, self._allow_release)
        self.locks.append(lock)
        return lock

    async def aclose(self):
        self.closed = True


class BlockingCloseRedis(FakeAsyncRedis):
    def __init__(self, close_started: asyncio.Event, allow_close: asyncio.Event):
        super().__init__()
        self._close_started = close_started
        self._allow_close = allow_close
        self.closed = False

    async def aclose(self):
        self._close_started.set()
        await self._allow_close.wait()
        self.closed = True
