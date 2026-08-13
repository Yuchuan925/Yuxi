from __future__ import annotations

import base64
from collections.abc import Sequence

import ormsgpack
from yuxi.services.run_queue_service import get_redis_client
from yuxi.utils.logging_config import logger
from yuxi.utils.paths import VIRTUAL_PATH_PREFIX

MENTION_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

MAX_MENTION_RESULTS = 50
MAX_ENTRIES_PER_DIR = 500
MAX_SEARCH_DEPTH = 15
CACHE_TTL = 60  # 缓存有效期 60 秒
MAX_CACHED_ENTRIES = 100000
REDIS_KEY_PREFIX = "yuxi:mention:cache:"
WORKSPACE_CACHE_PREFIX = f"{REDIS_KEY_PREFIX}workspace:"
THREAD_CACHE_PREFIX = f"{REDIS_KEY_PREFIX}thread:"
WORKSPACE_THREAD_PLACEHOLDER = "_workspace"
MENTION_SOURCES = {"workspace", "thread"}


async def _read_cached_index(redis, redis_key: str) -> list[tuple[str, str]] | None:
    cached_str = await redis.get(redis_key)
    if not cached_str:
        return None
    try:
        packed_bytes = base64.b64decode(cached_str)
        return ormsgpack.unpackb(packed_bytes)
    except Exception as e:
        logger.warning(f"Failed to unpack mention cache {redis_key}: {e}")
        return None


async def _write_cached_index(redis, redis_key: str, entries: list[tuple[str, str]]) -> None:
    try:
        packed_bytes = ormsgpack.packb(entries)
        packed_str = base64.b64encode(packed_bytes).decode("ascii")
        await redis.set(redis_key, packed_str, ex=CACHE_TTL)
    except Exception as e:
        logger.warning(f"Failed to write mention cache {redis_key}: {e}")


def _normalize_sources(sources: Sequence[str] | None, *, has_thread: bool) -> tuple[str, ...]:
    if not sources:
        return ("thread", "workspace") if has_thread else ("workspace",)

    normalized = []
    for source in sources:
        value = str(source or "").strip().lower()
        if value in MENTION_SOURCES and value not in normalized:
            normalized.append(value)

    if not has_thread:
        normalized = [source for source in normalized if source == "workspace"]
    return tuple(normalized or (["workspace"] if not has_thread else ["thread", "workspace"]))


def _filter_index_entries(entries: list[dict], virtual_prefix: str, max_entries: int) -> list[tuple[str, str]]:
    """按既有隐藏、深度、单目录和总量规则过滤 FileStore 列表。"""
    results: list[tuple[str, str]] = []
    per_directory: dict[str, int] = {}
    prefix = virtual_prefix.rstrip("/")
    for entry in entries:
        path = str(entry.get("path") or "").rstrip("/")
        if not path.startswith(f"{prefix}/"):
            continue
        relative = path[len(prefix) + 1 :]
        parts = relative.split("/")
        parent = "/".join(parts[:-1])
        directory_parts = parts if entry.get("is_dir") else parts[:-1]
        if len(parts) > MAX_SEARCH_DEPTH or any(
            part.startswith(".") or part in MENTION_EXCLUDE_DIRS for part in directory_parts
        ):
            continue
        if per_directory.get(parent, 0) >= MAX_ENTRIES_PER_DIR:
            continue
        per_directory[parent] = per_directory.get(parent, 0) + 1
        suffix = "/" if entry.get("is_dir") else ""
        virtual_relative = path[len(VIRTUAL_PATH_PREFIX.rstrip("/")) + 1 :]
        results.append((str(entry.get("name") or parts[-1]), f"{virtual_relative}{suffix}"))
        if len(results) >= max_entries:
            break
    return results


async def get_or_build_workspace_index(uid: str) -> list[tuple[str, str]]:
    redis = await get_redis_client()
    redis_key = f"{WORKSPACE_CACHE_PREFIX}{uid}"
    cached = await _read_cached_index(redis, redis_key)
    if cached is not None:
        return cached

    from yuxi.services.workspace_service import list_workspace_index_entries

    entries = await list_workspace_index_entries(uid)
    entries = _filter_index_entries(
        [
            {
                "name": name,
                "path": f"/home/gem/user-data/workspace/{path}",
                "is_dir": path.endswith("/"),
            }
            for name, path in entries
        ],
        "/home/gem/user-data/workspace",
        MAX_CACHED_ENTRIES,
    )
    await _write_cached_index(redis, redis_key, entries)
    return entries


async def get_or_build_thread_index(thread_id: str) -> list[tuple[str, str]]:
    redis = await get_redis_client()
    redis_key = f"{THREAD_CACHE_PREFIX}{thread_id}"
    cached = await _read_cached_index(redis, redis_key)
    if cached is not None:
        return cached

    entries: list[tuple[str, str]] = []
    from yuxi.services.thread_files_service import list_thread_object_entries

    for virtual_prefix in ("/home/gem/user-data/uploads", "/home/gem/user-data/outputs"):
        needed = MAX_CACHED_ENTRIES - len(entries)
        if needed <= 0:
            break
        listed = await list_thread_object_entries(thread_id, virtual_prefix, recursive=True)
        entries.extend(_filter_index_entries(listed, virtual_prefix, needed))

    await _write_cached_index(redis, redis_key, entries)
    return entries


async def get_or_build_file_index(
    thread_id: str | None,
    uid: str,
    sources: Sequence[str] | None = None,
) -> list[tuple[str, str, str]]:
    """获取或构建当前可提及文件索引，workspace 与 thread 缓存分离。"""
    selected_sources = _normalize_sources(sources, has_thread=bool(thread_id))
    entries: list[tuple[str, str, str]] = []

    for source in selected_sources:
        if source == "thread" and thread_id:
            entries.extend(
                (name, virtual_path, "thread") for name, virtual_path in await get_or_build_thread_index(thread_id)
            )
        elif source == "workspace":
            entries.extend(
                (name, virtual_path, "workspace") for name, virtual_path in await get_or_build_workspace_index(uid)
            )

    return entries


def _rank_mention_entries(index: list[tuple[str, str, str]], query: str) -> list[dict]:
    query_lower = query.lower()
    prefix = VIRTUAL_PATH_PREFIX.rstrip("/")
    name_matched = []
    path_matched = []

    for name, virtual_path, source in index:
        name_lower = name.lower()
        path_lower = virtual_path.lower()
        is_dir = virtual_path.endswith("/")

        if query_lower in name_lower:
            if name_lower == query_lower:
                score = 1000.0
            else:
                score = 500.0
                if name_lower.startswith(query_lower):
                    score += 50.0
                if name_lower.endswith(query_lower):
                    score += 20.0
                start_idx = name_lower.find(query_lower)
                if start_idx != -1:
                    score -= min(start_idx, 30.0)
                score -= min(len(name) * 0.5, 50.0)

            name_matched.append(
                {"name": name, "path": f"{prefix}/{virtual_path}", "is_dir": is_dir, "source": source, "score": score}
            )
        elif query_lower in path_lower:
            score = 10.0 - min(len(virtual_path) * 0.1, 5.0)
            path_matched.append(
                {"name": name, "path": f"{prefix}/{virtual_path}", "is_dir": is_dir, "source": source, "score": score}
            )

    name_matched.sort(key=lambda x: -x["score"])
    path_matched.sort(key=lambda x: len(x["path"]))
    return [*name_matched, *path_matched]


async def search_mention_files_in_index(
    thread_id: str | None,
    uid: str,
    query: str,
    sources: Sequence[str] | None = None,
) -> list[dict]:
    """搜索可提及文件；未绑定 thread 时只搜索用户 workspace。"""
    if not query:
        return []

    selected_sources = _normalize_sources(sources, has_thread=bool(thread_id))
    results: list[dict] = []

    for source in selected_sources:
        source_index = await get_or_build_file_index(thread_id, uid, [source])
        source_results = _rank_mention_entries(source_index, query)
        remaining = MAX_MENTION_RESULTS - len(results)
        if remaining <= 0:
            break
        results.extend(source_results[:remaining])

    return [
        {"name": item["name"], "path": item["path"], "is_dir": item["is_dir"], "source": item["source"]}
        for item in results[:MAX_MENTION_RESULTS]
    ]


async def invalidate_mention_cache(thread_id: str) -> None:
    """清理指定 thread 的提及文件缓存。"""
    try:
        redis = await get_redis_client()
        await redis.delete(f"{THREAD_CACHE_PREFIX}{thread_id}")
        await redis.delete(f"{REDIS_KEY_PREFIX}{thread_id}")
    except Exception as e:
        logger.warning(f"Failed to invalidate mention cache for thread {thread_id}: {e}")


async def invalidate_workspace_mention_cache(uid: str) -> None:
    """清理指定用户 workspace 的提及文件缓存。"""
    try:
        redis = await get_redis_client()
        await redis.delete(f"{WORKSPACE_CACHE_PREFIX}{uid}")
    except Exception as e:
        logger.warning(f"Failed to invalidate workspace mention cache for uid {uid}: {e}")
