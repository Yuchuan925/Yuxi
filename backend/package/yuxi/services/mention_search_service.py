from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import ormsgpack
from yuxi.agents.backends.sandbox.paths import user_workspace_dir
from yuxi.agents.backends.sandbox.paths import validate_thread_id
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.run_queue_service import get_redis_client
from yuxi.services.viewer_filesystem_service import search_viewer_files
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


class MentionThreadNotFoundError(LookupError):
    """当前用户不可见指定 mention thread。"""


class InvalidMentionThreadError(ValueError):
    """mention thread id 不满足运行时 identity 约束。"""


def _scan_pruned_files(root: Path, max_entries: int) -> list[tuple[str, str]]:
    """
    同步扫描磁盘文件目录并进行多重限额剪枝保护 (防止大文件仓库卡死)
    """
    results: list[tuple[str, str]] = []
    if not root.exists():
        return results

    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        # 1. 剪枝黑名单和隐藏目录 (直接在 dirnames 中修改，阻止 os.walk 深入)
        dirnames[:] = [d for d in dirnames if d not in MENTION_EXCLUDE_DIRS and not d.startswith(".")]

        # 2. 深度保护：限制最大搜索深度（root 本身为第 0 层，第 15 层时 rel.parts 长度恰好为 15）
        try:
            rel = Path(dirpath).relative_to(root)
            if len(rel.parts) >= MAX_SEARCH_DEPTH:
                dirnames.clear()
                continue
        except Exception:
            pass

        # 3. 宽度与全局限额保护下的合格“子目录实体”收集
        for dirname in dirnames:
            full_dir_path = Path(dirpath) / dirname
            rel_dir_path = full_dir_path.relative_to(root).as_posix()

            # 使用以 '/' 结尾的虚拟相对路径，代表这是一个目录
            virtual_dir_path = f"{rel_dir_path}/"
            results.append((dirname, virtual_dir_path))

            if len(results) >= max_entries:
                return results

        # 4. 宽度限额保护：单层目录限制最多只读取 500 个文件，防止扁平超宽目录卡死
        scan_filenames = filenames[:MAX_ENTRIES_PER_DIR]
        for filename in scan_filenames:
            full_path = Path(dirpath) / filename
            # 计算相对于根路径的相对路径
            rel_path = full_path.relative_to(root).as_posix()

            # 存为紧凑型元组 (filename, relative_path)
            results.append((filename, rel_path))

            # 5. 全局上限保护：如果总文件数已达上限，熔断退出
            if len(results) >= max_entries:
                return results

    return results


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


async def get_or_build_workspace_index(uid: str) -> list[tuple[str, str]]:
    redis = await get_redis_client()
    redis_key = f"{WORKSPACE_CACHE_PREFIX}{uid}"
    cached = await _read_cached_index(redis, redis_key)
    if cached is not None:
        return cached

    scan_results = await asyncio.to_thread(
        _scan_pruned_files,
        user_workspace_dir(uid),
        MAX_CACHED_ENTRIES,
    )
    entries = [(name, f"workspace/{rel_path}") for name, rel_path in scan_results]
    await _write_cached_index(redis, redis_key, entries)
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
    uid: str,
    query: str,
) -> list[dict]:
    """搜索用户 Workspace 中可提及文件。"""
    if not query:
        return []

    source_index = [(name, virtual_path, "workspace") for name, virtual_path in await get_or_build_workspace_index(uid)]
    results = _rank_mention_entries(source_index, query)

    return [
        {"name": item["name"], "path": item["path"], "is_dir": item["is_dir"], "source": item["source"]}
        for item in results[:MAX_MENTION_RESULTS]
    ]


async def search_mentions(
    *,
    thread_id: str | None,
    query: str,
    sources: str | None,
    current_user,
    db,
) -> list[dict]:
    """编排当前 Project 与用户 Workspace 的 mention 搜索。"""
    uid = str(current_user.uid)
    effective_thread_id: str | None = None
    if thread_id:
        conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
        if conversation:
            if conversation.uid != uid or conversation.status == "deleted":
                raise MentionThreadNotFoundError("对话线程不存在")
            effective_thread_id = thread_id
        else:
            try:
                validate_thread_id(thread_id)
            except ValueError as exc:
                raise InvalidMentionThreadError("非法的 thread_id 格式") from exc

    source_list = [item.strip().lower() for item in sources.split(",")] if sources else None
    selected_sources = source_list or (["thread", "workspace"] if effective_thread_id else ["workspace"])
    results: list[dict] = []
    if "thread" in selected_sources and effective_thread_id:
        project_results = await search_viewer_files(
            thread_id=effective_thread_id,
            query=query,
            current_user=current_user,
            db=db,
        )
        results.extend(
            {
                "name": item["name"],
                "path": item["path"],
                "is_dir": bool(item.get("is_dir")),
                "source": "thread",
            }
            for item in project_results.get("entries") or []
        )
    if "workspace" in selected_sources and len(results) < MAX_MENTION_RESULTS:
        results.extend(await search_mention_files_in_index(uid=uid, query=query))
    return results[:MAX_MENTION_RESULTS]


async def invalidate_workspace_mention_cache(uid: str) -> None:
    """清理指定用户 workspace 的提及文件缓存。"""
    try:
        redis = await get_redis_client()
        await redis.delete(f"{WORKSPACE_CACHE_PREFIX}{uid}")
    except Exception as e:
        logger.warning(f"Failed to invalidate workspace mention cache for uid {uid}: {e}")
