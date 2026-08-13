from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from concurrent.futures import Future
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import PurePosixPath
from typing import Any

from yuxi.storage.filestore import (
    get_file_store,
    thread_output_key,
    thread_skill_key,
    thread_upload_key,
    user_workspace_key,
)
from yuxi.storage.redis import create_async_redis_client, create_sync_redis_client

SYNC_ROOT = "/home/gem/.yuxi-sync"
MANIFEST_PATH = f"{SYNC_ROOT}/manifest.json"
MARKER_PATH = f"{SYNC_ROOT}/initialized"
MAX_SYNC_FILES = 1000
MAX_SYNC_BYTES = 256 * 1024 * 1024
MAX_SYNC_DEPTH = 64
SYNC_LOCK_MARGIN_SECONDS = 60
SYNC_LOCK_WAIT_SECONDS = 10
SKILLS_ROOT = "/home/gem/skills"
LOCK_RENEWAL_RATIO = 3

_locks_guard = threading.Lock()
_scope_locks: dict[str, threading.RLock] = {}
_loop_guard = threading.Lock()
_background_loop: asyncio.AbstractEventLoop | None = None
_operation_state = threading.local()
_async_held_scopes: ContextVar[tuple[Any | None, str | None, str | None]] = ContextVar(
    "yuxi_sandbox_file_held_scopes",
    default=(None, None, None),
)


def _lock_failure(message: str, errors: list[BaseException]) -> RuntimeError:
    detail = "; ".join(str(error) or type(error).__name__ for error in errors)
    return RuntimeError(f"{message}: {detail}")


class RedisLeaseGuard:
    """在线程间暴露 Redis 锁续租失败状态。"""

    def __init__(self, failure_message: str):
        self._failure_message = failure_message
        self._lock = threading.Lock()
        self._error: BaseException | None = None

    def fail(self, error: BaseException) -> None:
        """记录首个续租失败。"""
        with self._lock:
            if self._error is None:
                self._error = error

    def check(self) -> None:
        """续租已失败时阻止后续事实源写入。"""
        with self._lock:
            error = self._error
        if error is not None:
            raise _lock_failure(self._failure_message, [error])

    @property
    def error(self) -> BaseException | None:
        """返回已记录的续租失败。"""
        with self._lock:
            return self._error


@asynccontextmanager
async def renewable_async_redis_locks(
    keys: list[str],
    *,
    lease_seconds: float,
    blocking_timeout: float,
    failure_message: str,
):
    """获取一组可自动续租的异步 Redis 锁。"""
    redis = await create_async_redis_client()
    locks = []
    stop = asyncio.Event()
    renewal_errors: list[BaseException] = []
    owner_task = asyncio.current_task()
    if owner_task is None:
        raise RuntimeError("异步 Redis 锁必须在 asyncio Task 中使用")
    initial_cancelling = owner_task.cancelling()
    internal_cancel_requested = False

    async def renew() -> None:
        nonlocal internal_cancel_requested
        interval = max(0.01, lease_seconds / LOCK_RENEWAL_RATIO)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                for lock in locks:
                    if not await lock.extend(lease_seconds, replace_ttl=True):
                        raise RuntimeError("Redis 锁续租失败")
            except BaseException as exc:  # noqa: BLE001
                renewal_errors.append(exc)
                internal_cancel_requested = True
                owner_task.cancel()
                return

    renewal_task: asyncio.Task | None = None
    body_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    cleanup_cancel: asyncio.CancelledError | None = None

    async def cleanup() -> None:
        cleanup_timeout = max(1.0, min(lease_seconds, 30.0))
        stop.set()
        if renewal_task is not None:
            try:
                await asyncio.wait_for(renewal_task, timeout=cleanup_timeout)
            except BaseException as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
        cleanup_errors.extend(renewal_errors)
        for lock in reversed(locks):
            try:
                await asyncio.wait_for(lock.release(), timeout=cleanup_timeout)
            except BaseException as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
        try:
            await asyncio.wait_for(redis.aclose(), timeout=cleanup_timeout)
        except BaseException as exc:  # noqa: BLE001
            cleanup_errors.append(exc)

    try:
        for key in keys:
            lock = redis.lock(key, timeout=lease_seconds, blocking_timeout=blocking_timeout)
            if not await lock.acquire(blocking=True):
                raise RuntimeError(failure_message)
            locks.append(lock)
        renewal_task = asyncio.create_task(renew(), name="yuxi-redis-lock-renewal")
        try:
            yield
        except asyncio.CancelledError as exc:
            if internal_cancel_requested and owner_task.cancelling() == initial_cancelling + 1:
                owner_task.uncancel()
                body_error = None
            else:
                body_error = exc
        except BaseException as exc:  # noqa: BLE001
            body_error = exc
    finally:
        cleanup_task = asyncio.create_task(cleanup(), name="yuxi-redis-lock-cleanup")
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                cleanup_cancel = cleanup_cancel or exc
                owner_task.uncancel()
        try:
            cleanup_task.result()
        except BaseException as exc:  # noqa: BLE001
            cleanup_errors.append(exc)

        failure = _lock_failure(failure_message, cleanup_errors) if cleanup_errors else None
        if cleanup_cancel is not None:
            if failure is not None:
                cleanup_cancel.add_note(str(failure))
            raise cleanup_cancel
        if isinstance(body_error, asyncio.CancelledError):
            if failure is not None:
                body_error.add_note(str(failure))
            raise body_error
        if body_error is not None:
            if failure is not None:
                body_error.add_note(str(failure))
            raise body_error
        if failure is not None:
            raise failure


@contextmanager
def renewable_sync_redis_locks(
    keys: list[str],
    *,
    lease_seconds: float,
    blocking_timeout: float,
    failure_message: str,
):
    """获取一组由后台线程自动续租的同步 Redis 锁。"""
    redis = create_sync_redis_client()
    locks = []
    stop = threading.Event()
    renewal_errors: list[BaseException] = []
    guard = RedisLeaseGuard(failure_message)

    def renew() -> None:
        interval = max(0.01, lease_seconds / LOCK_RENEWAL_RATIO)
        while not stop.wait(interval):
            try:
                for lock in locks:
                    if not lock.extend(lease_seconds, replace_ttl=True):
                        raise RuntimeError("Redis 锁续租失败")
            except BaseException as exc:  # noqa: BLE001
                renewal_errors.append(exc)
                guard.fail(exc)
                return

    renewal_thread: threading.Thread | None = None
    body_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        for key in keys:
            lock = redis.lock(
                key,
                timeout=lease_seconds,
                blocking_timeout=blocking_timeout,
                thread_local=False,
            )
            if not lock.acquire(blocking=True):
                raise RuntimeError(failure_message)
            locks.append(lock)
        renewal_thread = threading.Thread(target=renew, name="yuxi-redis-lock-renewal", daemon=True)
        renewal_thread.start()
        try:
            yield guard
        except BaseException as exc:  # noqa: BLE001
            body_error = exc
            raise
    finally:
        stop.set()
        if renewal_thread is not None:
            renewal_thread.join(timeout=max(1.0, lease_seconds))
            if renewal_thread.is_alive():
                cleanup_errors.append(RuntimeError("Redis 锁续租线程未能停止"))
        cleanup_errors.extend(renewal_errors)
        for lock in reversed(locks):
            try:
                lock.release()
            except BaseException as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
        try:
            redis.close()
        except BaseException as exc:  # noqa: BLE001
            cleanup_errors.append(exc)

        if cleanup_errors:
            failure = _lock_failure(failure_message, cleanup_errors)
            if body_error is not None:
                body_error.add_note(str(failure))
            else:
                raise failure


@asynccontextmanager
async def file_thread_operation_lock(file_thread_id: str, *, timeout_seconds: int = 180):
    """获取与 Sandbox 同规则的 file_thread Redis 分布式锁。"""
    async with sandbox_file_operation_lock(file_thread_id=file_thread_id, timeout_seconds=timeout_seconds):
        yield


@asynccontextmanager
async def sandbox_file_operation_lock(
    *,
    uid: str | None = None,
    file_thread_id: str | None = None,
    timeout_seconds: int = 180,
):
    """按 uid、file_thread 固定顺序获取 Sandbox 文件分布式锁。"""
    normalized_uid = str(uid or "").strip() or None
    normalized_file_thread_id = str(file_thread_id or "").strip() or None
    if normalized_uid is None and normalized_file_thread_id is None:
        raise ValueError("uid 或 file_thread_id 至少需要提供一个")

    current_task = asyncio.current_task()
    owner_task, held_uid, held_file_thread_id = _async_held_scopes.get()
    if owner_task is not current_task:
        held_uid = None
        held_file_thread_id = None
    if held_file_thread_id is not None and normalized_uid is not None and held_uid is None:
        raise RuntimeError("Sandbox 文件锁不允许在 file_thread 锁内追加 uid 锁")
    if (normalized_uid is not None and held_uid not in {None, normalized_uid}) or (
        normalized_file_thread_id is not None
        and held_file_thread_id not in {None, normalized_file_thread_id}
    ):
        raise RuntimeError("Sandbox 文件锁不允许在同一任务中切换作用域")

    requested = [
        ("uid", normalized_uid, held_uid),
        ("file-thread", normalized_file_thread_id, held_file_thread_id),
    ]
    missing = [(scope, identity) for scope, identity, held in requested if identity is not None and held is None]
    if not missing:
        yield
        return

    lock_timeout = max(1, timeout_seconds) + SYNC_LOCK_MARGIN_SECONDS
    token = None
    async with renewable_async_redis_locks(
        [f"yuxi:sandbox-files:{scope}:{identity}" for scope, identity in missing],
        lease_seconds=lock_timeout,
        blocking_timeout=SYNC_LOCK_WAIT_SECONDS,
        failure_message="Sandbox 文件操作 Redis 分布式锁获取或续租失败",
    ):
        token = _async_held_scopes.set(
            (current_task, normalized_uid or held_uid, normalized_file_thread_id or held_file_thread_id)
        )
        try:
            yield
        finally:
            if token is not None:
                _async_held_scopes.reset(token)


def _get_background_loop() -> asyncio.AbstractEventLoop:
    """返回专用后台事件循环，避免在同步调用中嵌套 asyncio.run。"""
    global _background_loop
    with _loop_guard:
        if _background_loop is not None and _background_loop.is_running():
            return _background_loop
        ready = threading.Event()

        def run() -> None:
            global _background_loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _background_loop = loop
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, name="yuxi-filestore-loop", daemon=True).start()
        ready.wait()
        assert _background_loop is not None
        return _background_loop


def _run_async(coroutine) -> Any:
    """把一个 FileStore coroutine 提交到专用后台事件循环。"""
    future: Future[Any] = asyncio.run_coroutine_threadsafe(coroutine, _get_background_loop())
    return future.result()


def _scope_lock(uid: str, file_thread_id: str, *, timeout: float) -> contextmanager:
    """按固定 uid、file_thread_id 顺序提供进程内同步锁。"""
    with _locks_guard:
        locks = [
            _scope_locks.setdefault(f"uid:{uid}", threading.RLock()),
            _scope_locks.setdefault(f"file:{file_thread_id}", threading.RLock()),
        ]

    @contextmanager
    def acquire():
        acquired: list[threading.RLock] = []
        for lock in locks:
            if not lock.acquire(timeout=timeout):
                for held in reversed(acquired):
                    held.release()
                raise RuntimeError("Sandbox 文件操作进程锁获取超时")
            acquired.append(lock)
        try:
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    return acquire()


class SandboxFileSynchronizer:
    """在临时 Sandbox 文件树和 FileStore 之间同步四个文件命名空间。"""

    def __init__(self, *, uid: str, file_thread_id: str, skills_thread_id: str, sandbox_id: str):
        self._uid = uid
        self._file_thread_id = file_thread_id
        self._skills_thread_id = skills_thread_id
        self._sandbox_id = sandbox_id
        self._initialized = False

    @contextmanager
    def operation(self, client: Any, *, timeout_seconds: int):
        """串行化一次完整 Sandbox 文件操作，禁止跨进程降级执行。"""
        active = getattr(_operation_state, "active", None)
        if active is self:
            _operation_state.depth += 1
            try:
                yield
            finally:
                _operation_state.depth -= 1
            return

        lock_timeout = max(1, timeout_seconds) + SYNC_LOCK_MARGIN_SECONDS
        with _scope_lock(self._uid, self._file_thread_id, timeout=lock_timeout):
            with renewable_sync_redis_locks(
                [
                    f"yuxi:sandbox-files:uid:{self._uid}",
                    f"yuxi:sandbox-files:file-thread:{self._file_thread_id}",
                ],
                lease_seconds=lock_timeout,
                blocking_timeout=SYNC_LOCK_WAIT_SECONDS,
                failure_message="Sandbox 文件操作 Redis 分布式锁获取或续租失败",
            ) as lease_guard:
                if not self._initialized:
                    self.initialize(client)
                _operation_state.active = self
                _operation_state.depth = 1
                _operation_state.lease_guard = lease_guard
                try:
                    yield
                finally:
                    _operation_state.active = None
                    _operation_state.depth = 0
                    _operation_state.lease_guard = None

    def initialize(self, client: Any) -> None:
        """刷新所有来源，并最后写入 manifest 与初始化 marker。"""
        with _scope_lock(self._uid, self._file_thread_id, timeout=SYNC_LOCK_MARGIN_SECONDS):
            self._ensure_roots(client)
            self._refresh_sources(client)
            manifest = self._scan(client)
            self._write_manifest(client, manifest)
            self._write_marker(client)
            self._initialized = True

    def refresh(self, client: Any, scopes: set[str] | None = None) -> None:
        """在 execute 前从 FileStore 刷新 Sandbox。"""
        if not self._initialized:
            self.initialize(client)
            return
        with _scope_lock(self._uid, self._file_thread_id, timeout=SYNC_LOCK_MARGIN_SECONDS):
            self._refresh_sources(client, scopes=scopes)
            self._write_manifest(client, self._scan(client))

    def sync_back(self, client: Any) -> None:
        """扫描 Sandbox，并精确回写 workspace/outputs 的增删改。"""
        lease_guard = getattr(_operation_state, "lease_guard", None)
        if lease_guard is None:
            raise RuntimeError("sync_back 必须在 Sandbox 文件 operation 内执行")
        lease_guard.check()
        if not self._initialized:
            self.initialize(client)
            return
        with _scope_lock(self._uid, self._file_thread_id, timeout=SYNC_LOCK_MARGIN_SECONDS):
            previous = self._read_manifest(client)
            current = self._scan(client)
            writable_scopes = {"workspace", "outputs"}
            current_writable = {
                path: item for path, item in current.items() if item["scope"] in writable_scopes
            }
            previous_writable = {
                path: item for path, item in previous.items() if item["scope"] in writable_scopes
            }
            store = get_file_store()
            for path, item in current_writable.items():
                if previous_writable.get(path, {}).get("sha256") == item["sha256"]:
                    continue
                lease_guard.check()
                _run_async(store.put(item["key"], self._download(client, path)))
            for path, item in previous_writable.items():
                if path not in current_writable:
                    lease_guard.check()
                    _run_async(store.delete(item["key"]))
            lease_guard.check()
            self._write_manifest(client, current)

    def _refresh_sources(self, client: Any, scopes: set[str] | None = None) -> None:
        sources = (
            ("workspace", user_workspace_key, self._uid, "/home/gem/user-data/workspace"),
            ("uploads", thread_upload_key, self._file_thread_id, "/home/gem/user-data/uploads"),
            ("outputs", thread_output_key, self._file_thread_id, "/home/gem/user-data/outputs"),
            ("skills", thread_skill_key, self._skills_thread_id, SKILLS_ROOT),
        )
        store = get_file_store()
        for scope, builder, identity, root in sources:
            if scopes is not None and scope not in scopes:
                continue
            prefix = builder(identity, "_").rsplit("/", 1)[0]
            objects = _run_async(store.list(f"{prefix}/"))
            if scope == "skills":
                files = {
                    stat.key.removeprefix(f"{prefix}/"): _run_async(store.read(stat.key)).data
                    for stat in objects
                    if stat.key.removeprefix(f"{prefix}/")
                }
                from .provider import get_sandbox_provider

                get_sandbox_provider().replace_skills(self._sandbox_id, files)
                continue
            expected: set[str] = set()
            for stat in objects:
                relative = stat.key.removeprefix(f"{prefix}/")
                if not relative:
                    continue
                path = f"{root}/{relative}"
                expected.add(path)
                self._upload(client, path, _run_async(store.read(stat.key)).data)
            current = self._scan(client)
            for path, item in current.items():
                if item["scope"] == scope and path not in expected:
                    self._unlink(client, path)

    @staticmethod
    def _ensure_roots(client: Any) -> None:
        """确保全新 Sandbox 已创建同步器管理的目录。"""
        result = client.shell.exec_command(
            command=(
                "mkdir -p /home/gem/user-data/workspace /home/gem/user-data/uploads "
                "/home/gem/user-data/outputs /home/gem/.yuxi-sync"
            )
        )
        if result.data.exit_code not in (0, None):
            raise RuntimeError(result.data.output or "failed to create sandbox sync roots")

    def _scan(self, client: Any) -> dict[str, dict[str, Any]]:
        roots = (
            ("workspace", "/home/gem/user-data/workspace", user_workspace_key(self._uid, "_")),
            ("uploads", "/home/gem/user-data/uploads", thread_upload_key(self._file_thread_id, "_")),
            ("outputs", "/home/gem/user-data/outputs", thread_output_key(self._file_thread_id, "_")),
            ("skills", SKILLS_ROOT, thread_skill_key(self._skills_thread_id, "_")),
        )
        manifest: dict[str, dict[str, Any]] = {}
        for scope, root, key_template in roots:
            scope_files = 0
            scope_size = 0
            self._reject_symlinks(client, root)
            result = client.file.list_path(
                path=root,
                recursive=True,
                include_size=True,
                include_permissions=True,
                max_depth=MAX_SYNC_DEPTH,
            )
            if not result.success or result.data is None:
                raise RuntimeError(result.message or f"failed to scan {root}")
            for entry in result.data.files or []:
                if entry.is_directory:
                    continue
                path = str(entry.path)
                relative = PurePosixPath(path).relative_to(PurePosixPath(root))
                if len(relative.parts) > MAX_SYNC_DEPTH:
                    raise ValueError(f"Sandbox 文件层级超过限制（最多 {MAX_SYNC_DEPTH} 层）")
                scope_files += 1
                if scope_files > MAX_SYNC_FILES:
                    raise ValueError(f"Sandbox {scope} 文件数超过限制（每个 scope 最多 {MAX_SYNC_FILES} 个文件）")
                size = entry.size
                if not isinstance(size, int) or size < 0:
                    raise ValueError(f"无法确认 Sandbox 文件大小: {path}")
                scope_size += size
                if scope_size > MAX_SYNC_BYTES:
                    raise ValueError(
                        f"Sandbox {scope} 文件总大小超过限制（每个 scope 最多 {MAX_SYNC_BYTES // 1024 // 1024} MB）"
                    )
                data = self._download(client, path)
                if len(data) != size:
                    raise ValueError(f"Sandbox 文件大小发生变化: {path}")
                manifest[path] = {
                    "scope": scope,
                    "key": f"{key_template.rsplit('/', 1)[0]}/{relative.as_posix()}",
                    "size": size,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
        return manifest

    @staticmethod
    def _download(client: Any, path: str) -> bytes:
        return b"".join(client.file.download_file(path=path))

    @staticmethod
    def _upload(client: Any, path: str, data: bytes) -> None:
        result = client.file.write_file(
            file=path,
            content=base64.b64encode(data).decode("ascii"),
            encoding="base64",
        )
        if not result.success:
            raise RuntimeError(result.message or f"failed to upload {path}")

    @staticmethod
    def _unlink(client: Any, path: str) -> None:
        encoded = base64.b64encode(path.encode()).decode("ascii")
        result = client.shell.exec_command(
            command=(
                "python3 -c \"import base64, os; p=base64.b64decode('"
                f"{encoded}').decode(); os.unlink(p) if os.path.isfile(p) else None\""
            )
        )
        if result.data.exit_code not in (0, None):
            raise RuntimeError(result.data.output or f"failed to remove {path}")

    @staticmethod
    def _reject_symlinks(client: Any, root: str) -> None:
        encoded = base64.b64encode(root.encode()).decode("ascii")
        result = client.shell.exec_command(
            command=(
                "python3 -c \"import base64, os; root=base64.b64decode('"
                f"{encoded}').decode(); print(next((os.path.join(d,n) for d,_,ns in os.walk(root, followlinks=False) "
                "for n in ns if os.path.islink(os.path.join(d,n))), ''))\""
            )
        )
        if result.data.exit_code not in (0, None):
            raise RuntimeError(result.data.output or f"failed to inspect {root}")
        if (result.data.output or "").strip():
            raise ValueError(f"Sandbox 禁止 symlink: {(result.data.output or '').strip()}")

    @staticmethod
    def _read_manifest(client: Any) -> dict[str, dict[str, Any]]:
        try:
            result = client.file.read_file(file=MANIFEST_PATH)
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc).lower() or "not found" in str(exc).lower():
                return {}
            raise
        if not result.data or not result.data.content:
            return {}
        return json.loads(result.data.content)

    @staticmethod
    def _write_manifest(client: Any, manifest: dict[str, dict[str, Any]]) -> None:
        result = client.file.write_file(
            file=MANIFEST_PATH,
            content=json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )
        if not result.success:
            raise RuntimeError(result.message or "failed to write sandbox sync manifest")

    @staticmethod
    def _write_marker(client: Any) -> None:
        result = client.file.write_file(file=MARKER_PATH, content="initialized\n", encoding="utf-8")
        if not result.success:
            raise RuntimeError(result.message or "failed to write sandbox sync marker")
