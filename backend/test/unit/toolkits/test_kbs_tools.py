from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace

import pytest

from yuxi.agents.toolkits.kbs import tools
from yuxi.knowledge.base import KnowledgeBase
from yuxi.knowledge.manager import KnowledgeBaseManager
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.storage.filestore import LocalFileStore, thread_output_key


def _tool_callable(tool):
    callback = getattr(tool, "coroutine", None)
    if callback is not None:
        return callback

    callback = getattr(tool, "func", None)
    if callback is not None:
        return callback

    raise AssertionError(f"{tool.name} tool has no callable entry")


def _query_kb_callable():
    return _tool_callable(tools.query_kb)


def _find_kb_document_callable():
    return _tool_callable(tools.find_kb_document)


def _open_kb_document_callable():
    return _tool_callable(tools.open_kb_document)


def _get_mindmap_callable():
    return _tool_callable(tools.get_mindmap)


async def _run_tool(callback, **kwargs):
    result = callback(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _run_query_kb(**kwargs):
    return await _run_tool(_query_kb_callable(), **kwargs)


async def _run_find_kb_document(**kwargs):
    return await _run_tool(_find_kb_document_callable(), **kwargs)


async def _run_open_kb_document(**kwargs):
    return await _run_tool(_open_kb_document_callable(), **kwargs)


async def _run_get_mindmap(**kwargs):
    return await _run_tool(_get_mindmap_callable(), **kwargs)


def _build_test_window(content: str, offset: int = 0, limit: int = 1800) -> dict:
    lines = content.splitlines()
    start = min(max(offset, 0), len(lines))
    selected = lines[start : start + limit]
    end = start + len(selected)
    return {
        "start_line": start + 1 if selected else 0,
        "end_line": end,
        "total_lines": len(lines),
        "offset": start,
        "window_size": limit,
        "has_more_before": start > 0,
        "has_more_after": end < len(lines),
        "next_offset": end if end < len(lines) else None,
        "content": "\n".join(f"{start + idx + 1:6d}\t{line}" for idx, line in enumerate(selected)),
    }


def _patch_retrievers(monkeypatch, *, kb_type: str = "milvus", retriever=None):
    async def _not_configured(*args, **kwargs):
        del args, kwargs
        raise AssertionError("knowledge base method is not configured for this test")

    async def _fake_get_database_document_support(kb_id: str):
        return SimpleNamespace(kb_id=kb_id, name="FAQ", kb_type=kb_type), kb_type != "dify"

    manager = SimpleNamespace(
        find_file_content=_not_configured,
        open_file_content=_not_configured,
        get_database_document_support=_fake_get_database_document_support,
    )

    async def _retrieve(kb_id: str, query: str, **options):
        if kb_id != "db-1":
            raise ValueError(f"知识库资源 '{kb_id}' 不存在")
        return await (retriever or object())(query, **options)

    manager.retrieve = _retrieve
    # 复用真实 manager 的文档操作方法，使其内部走上面的 mock。
    for name in (
        "open_document",
        "find_in_document",
        "_require_kb_supports_documents",
        "database_type_supports_documents",
    ):
        setattr(manager, name, MethodType(getattr(KnowledgeBaseManager, name), manager))
    monkeypatch.setattr(tools, "_get_knowledge_base", lambda: manager)
    monkeypatch.setattr(tools, "knowledge_base", manager, raising=False)
    return manager


async def _fake_visible_kbs(runtime):
    del runtime
    return [{"kb_id": "db-1", "name": "FAQ", "kb_type": "milvus"}]


@pytest.mark.asyncio
async def test_get_mindmap_resolves_current_visible_knowledge_base(monkeypatch) -> None:
    async def fake_get_by_kb_id(_self, kb_id: str):
        assert kb_id == "db-1"
        return SimpleNamespace(name="Renamed FAQ", mindmap={"content": "Root", "children": []})

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)
    monkeypatch.setattr(KnowledgeBaseRepository, "get_by_kb_id", fake_get_by_kb_id)

    result = await _run_get_mindmap(kb_name="FAQ", runtime=SimpleNamespace(context=SimpleNamespace()))

    assert "知识库 FAQ 的思维导图结构" in result
    assert "- Root" in result


@pytest.mark.asyncio
async def test_get_mindmap_rejects_knowledge_base_outside_runtime_scope(monkeypatch) -> None:
    async def no_visible_kbs(_runtime):
        return []

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", no_visible_kbs)

    result = await _run_get_mindmap(kb_name="FAQ", runtime=SimpleNamespace(context=SimpleNamespace()))

    assert result == "知识库 'FAQ' 不存在或当前会话未启用"


@pytest.mark.asyncio
async def test_query_kb_returns_search_schema_without_sandbox_paths(monkeypatch) -> None:
    async def _fake_retriever(query_text: str, **kwargs):
        assert query_text == "auth"
        assert kwargs == {}
        return KnowledgeBase.build_search_output(
            "db-1",
            [
                {
                    "content": "auth guide",
                    "metadata": {
                        "file_id": "file-1",
                        "source": "auth-guide.pdf",
                        "filepath": "/tmp/sandbox/auth-guide.pdf",
                    },
                }
            ],
        )

    _patch_retrievers(monkeypatch, retriever=_fake_retriever)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_query_kb(kb_id="db-1", query_text="auth", runtime=runtime)

    assert result["kb_id"] == "db-1"
    assert result["results"][0]["id"] == "file-1:1"
    assert result["results"][0]["kb_id"] == "db-1"
    assert result["results"][0]["file_id"] == "file-1"
    assert result["results"][0]["content"] == "auth guide"
    assert result["results"][0]["metadata"]["source"] == "auth-guide.pdf"
    assert "filepath" not in result["results"][0]["metadata"]
    assert "parsed_path" not in result["results"][0]["metadata"]


@pytest.mark.asyncio
async def test_query_kb_allows_dify_knowledge_base(monkeypatch) -> None:
    async def _fake_retriever(query_text: str, **kwargs):
        assert query_text == "auth"
        return KnowledgeBase.build_search_output(
            "db-1",
            [
                {
                    "content": "auth guide",
                    "score": 0.98,
                    "metadata": {
                        "file_id": "dify-doc-1",
                        "chunk_id": "dify-segment-1",
                        "source": "Dify Doc",
                    },
                }
            ],
        )

    _patch_retrievers(monkeypatch, kb_type="dify", retriever=_fake_retriever)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_query_kb(kb_id="db-1", query_text="auth", runtime=runtime)

    assert result == {
        "kb_id": "db-1",
        "results": [
            {
                "id": "dify-segment-1",
                "kb_id": "db-1",
                "file_id": "dify-doc-1",
                "content": "auth guide",
                "metadata": {
                    "file_id": "dify-doc-1",
                    "chunk_id": "dify-segment-1",
                    "source": "Dify Doc",
                    "score": 0.98,
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_query_kb_returns_plain_result_without_path_injection(monkeypatch) -> None:
    async def _fake_retriever(query_text: str, **kwargs):
        assert query_text == "auth"
        return "Milvus context"

    _patch_retrievers(monkeypatch, retriever=_fake_retriever)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_query_kb(kb_id="db-1", query_text="auth", runtime=runtime)

    assert result == "Milvus context"


@pytest.mark.asyncio
async def test_query_kb_maps_full_doc_id_and_chunk_metadata(monkeypatch) -> None:
    async def _fake_retriever(query_text: str, **kwargs):
        assert query_text == "auth"
        return KnowledgeBase.build_search_output(
            "db-1",
            [
                {
                    "content": "auth guide",
                    "full_doc_id": "file-1",
                    "chunk_id": "chunk-1",
                    "chunk_index": 3,
                }
            ],
        )

    _patch_retrievers(monkeypatch, retriever=_fake_retriever)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_query_kb(kb_id="db-1", query_text="auth", runtime=runtime)

    assert result["results"][0] == {
        "id": "chunk-1",
        "kb_id": "db-1",
        "file_id": "file-1",
        "content": "auth guide",
        "metadata": {"chunk_index": 3},
    }


@pytest.mark.asyncio
async def test_find_kb_document_returns_context_windows(monkeypatch) -> None:
    _patch_retrievers(monkeypatch)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    async def _fake_find_file_content(
        kb_id: str,
        file_id: str,
        patterns: list[str],
        *,
        use_regex: bool = False,
        case_sensitive: bool = False,
        max_windows: int = 5,
        window_size: int = 80,
    ):
        assert kb_id == "db-1"
        assert file_id == "file-1"
        assert patterns == ["token"]
        assert use_regex is False
        assert case_sensitive is False
        assert max_windows == 5
        assert window_size == 80
        return {
            "semantic": False,
            "match_mode": "keyword",
            "total_matches": 2,
            "windows": [
                {
                    "start_line": 1,
                    "end_line": 3,
                    "matched_lines": [2],
                    "content": "     1\tintro\n     2\ttoken value\n     3\toutro",
                }
            ],
        }

    monkeypatch.setattr(tools.knowledge_base, "find_file_content", _fake_find_file_content)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_find_kb_document(
        kb_id="db-1",
        file_id="file-1",
        patterns=["token"],
        runtime=runtime,
    )

    assert result == {
        "kb_id": "db-1",
        "file_id": "file-1",
        "semantic": False,
        "match_mode": "keyword",
        "total_matches": 2,
        "windows": [
            {
                "start_line": 1,
                "end_line": 3,
                "matched_lines": [2],
                "content": "     1\tintro\n     2\ttoken value\n     3\toutro",
            }
        ],
    }


@pytest.mark.asyncio
async def test_find_kb_document_rejects_dify(monkeypatch) -> None:
    _patch_retrievers(monkeypatch, kb_type="dify")
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_find_kb_document(
        kb_id="db-1",
        file_id="file-1",
        patterns=["token"],
        runtime=runtime,
    )

    assert "只支持检索" in result


@pytest.mark.asyncio
async def test_open_kb_document_reads_markdown_content_by_default_window(monkeypatch) -> None:
    lines = [f"line {index}" for index in range(1, 2001)]

    _patch_retrievers(monkeypatch)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    async def _fake_open_file_content(kb_id: str, file_id: str, offset: int = 0, limit: int = 1800):
        assert kb_id == "db-1"
        assert file_id == "file-1"
        return _build_test_window("\n".join(lines), offset=offset, limit=limit)

    monkeypatch.setattr(tools.knowledge_base, "open_file_content", _fake_open_file_content)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_open_kb_document(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert result["kb_id"] == "db-1"
    assert result["file_id"] == "file-1"
    assert result["start_line"] == 1
    assert result["end_line"] == 1800
    assert result["total_lines"] == 2000
    assert result["window_size"] == 1800
    assert result["has_more_before"] is False
    assert result["has_more_after"] is True
    assert result["next_offset"] == 1800
    assert "     1\tline 1" in result["content"]
    assert "  1800\tline 1800" in result["content"]


@pytest.mark.asyncio
async def test_open_kb_document_prefers_line_over_offset(monkeypatch) -> None:
    lines = [f"line {index}" for index in range(1, 1001)]

    _patch_retrievers(monkeypatch)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    async def _fake_open_file_content(kb_id: str, file_id: str, offset: int = 0, limit: int = 1800):
        assert kb_id == "db-1"
        assert file_id == "file-1"
        return _build_test_window("\n".join(lines), offset=offset, limit=limit)

    monkeypatch.setattr(tools.knowledge_base, "open_file_content", _fake_open_file_content)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_open_kb_document(
        kb_id="db-1",
        file_id="file-1",
        line=801,
        offset=0,
        window_size=10,
        runtime=runtime,
    )

    assert result["offset"] == 800
    assert result["start_line"] == 801
    assert result["end_line"] == 810
    assert result["has_more_before"] is True
    assert result["has_more_after"] is True
    assert result["next_offset"] == 810
    assert "   801\tline 801" in result["content"]


@pytest.mark.asyncio
async def test_open_kb_document_rejects_invisible_resource(monkeypatch) -> None:
    async def _fake_visible_kbs(runtime):
        del runtime
        return [{"kb_id": "db-2", "name": "FAQ"}]

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_open_kb_document(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert "不存在或当前会话未启用" in result


@pytest.mark.asyncio
async def test_open_kb_document_requires_markdown_content(monkeypatch) -> None:
    _patch_retrievers(monkeypatch)
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    async def _fake_open_file_content(kb_id: str, file_id: str, offset: int = 0, limit: int = 1800):
        del kb_id, file_id, offset, limit
        raise Exception("文件 file-1 没有解析后的 Markdown 内容")

    monkeypatch.setattr(tools.knowledge_base, "open_file_content", _fake_open_file_content)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_open_kb_document(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert "没有解析后的 Markdown 内容" in result


def _search_file_callable():
    return _tool_callable(tools.search_file)


async def _run_search_file(**kwargs):
    return await _run_tool(_search_file_callable(), **kwargs)


@pytest.mark.asyncio
async def test_search_file_requires_kb_name_or_query(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(runtime=runtime)

    assert "不能同时为空" in result


@pytest.mark.asyncio
async def test_search_file_returns_files_by_query(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    from types import SimpleNamespace as SN

    fake_files = [
        SN(
            file_id="file-1",
            filename="test.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=1024,
        ),
        SN(
            file_id="file-2",
            filename="test2.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=2048,
        ),
        SN(
            file_id="file-3",
            filename="other.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=512,
        ),
    ]

    async def _fake_search_files(
        self,
        *,
        kb_id,
        filename_query=None,
        statuses=None,
        offset=0,
        limit=100,
        files_only=True,
    ):
        del self, kb_id, statuses, files_only
        matches = [file for file in fake_files if (filename_query or "") in file.filename.lower()]
        return matches[offset : offset + limit], len(matches)

    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    monkeypatch.setattr(KnowledgeFileRepository, "search_files", _fake_search_files)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(query="test", runtime=runtime)

    assert result["total"] == 2
    assert len(result["files"]) == 2
    assert result["files"][0]["filename"] == "test.pdf"
    assert result["files"][1]["filename"] == "test2.pdf"


@pytest.mark.asyncio
async def test_search_file_returns_all_files_when_query_empty(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    from types import SimpleNamespace as SN

    fake_files = [
        SN(
            file_id="file-1",
            filename="test.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=1024,
        ),
        SN(
            file_id="file-2",
            filename="other.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=2048,
        ),
    ]

    async def _fake_search_files(
        self,
        *,
        kb_id,
        filename_query=None,
        statuses=None,
        offset=0,
        limit=100,
        files_only=True,
    ):
        del self, kb_id, filename_query, statuses, files_only
        return fake_files[offset : offset + limit], len(fake_files)

    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    monkeypatch.setattr(KnowledgeFileRepository, "search_files", _fake_search_files)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(kb_name="FAQ", runtime=runtime)

    assert result["total"] == 2
    assert len(result["files"]) == 2


@pytest.mark.asyncio
async def test_search_file_pagination(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    from types import SimpleNamespace as SN

    fake_files = [
        SN(
            file_id=f"file-{i}",
            filename=f"file{i}.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=1024 * i,
        )
        for i in range(10)
    ]

    async def _fake_search_files(
        self,
        *,
        kb_id,
        filename_query=None,
        statuses=None,
        offset=0,
        limit=100,
        files_only=True,
    ):
        del self, kb_id, filename_query, statuses, files_only
        return fake_files[offset : offset + limit], len(fake_files)

    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    monkeypatch.setattr(KnowledgeFileRepository, "search_files", _fake_search_files)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(kb_name="FAQ", offset=2, limit=3, runtime=runtime)

    assert result["total"] == 10
    assert len(result["files"]) == 3
    assert result["offset"] == 2
    assert result["limit"] == 3
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_search_file_rejects_invisible_kb(monkeypatch) -> None:
    async def _fake_visible_kbs(runtime):
        del runtime
        return [{"kb_id": "db-2", "name": "Other"}]

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(kb_name="FAQ", query="test", runtime=runtime)

    assert "不存在或当前会话未启用" in result


@pytest.mark.asyncio
async def test_search_file_skips_read_only_kbs(monkeypatch) -> None:
    async def _fake_visible_read_only_kbs(runtime):
        del runtime
        return [{"kb_id": "dify-1", "name": "Dify", "kb_type": "dify"}]

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_read_only_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(query="report", runtime=runtime)

    assert "只支持检索，不支持文件搜索" in result


@pytest.mark.asyncio
async def test_search_file_total_reflects_full_set_not_page(monkeypatch) -> None:
    """total/has_more 必须基于全量文件，而非按 limit/offset 截断的窗口。"""
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    from types import SimpleNamespace as SN

    fake_files = [
        SN(
            file_id=f"file-{i:02d}",
            filename=f"file{i}.pdf",
            file_type="file",
            status="indexed",
            created_at=None,
            updated_at=None,
            file_size=1024,
        )
        for i in range(50)
    ]

    async def _fake_search_files(
        self,
        *,
        kb_id,
        filename_query=None,
        statuses=None,
        offset=0,
        limit=100,
        files_only=True,
    ):
        del self, kb_id, filename_query, statuses, files_only
        return fake_files[offset : offset + limit], len(fake_files)

    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    monkeypatch.setattr(KnowledgeFileRepository, "search_files", _fake_search_files)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_search_file(kb_name="FAQ", offset=0, limit=10, runtime=runtime)

    assert result["total"] == 50
    assert len(result["files"]) == 10
    assert result["has_more"] is True


# ========== download_kb_file ==========


def _patch_download_manager(monkeypatch, *, kb_type: str = "milvus", file_download=None):
    """复用 _patch_retrievers 的 _require_kb_supports_documents 真实逻辑，并绑定真实
    get_file_download，仅 mock get_kb_executor 与底层 kb 实例的下载方法——这样
    manager 内部的只读源校验路径会被真正走到，而非被整方法替换绕过。"""
    manager = _patch_retrievers(monkeypatch, kb_type=kb_type)
    manager.get_file_download = MethodType(KnowledgeBaseManager.get_file_download, manager)

    async def fake_get_kb_executor(kb_id: str):
        del kb_id
        return SimpleNamespace(get_file_download=file_download or _async_get_file_download(b"", "file"))

    manager.get_kb_executor = fake_get_kb_executor
    return manager


def _download_kb_file_callable():
    return _tool_callable(tools.download_kb_file)


async def _run_download_kb_file(**kwargs):
    return await _run_tool(_download_kb_file_callable(), **kwargs)


@pytest.mark.asyncio
async def test_download_kb_file_writes_original_to_filestore_and_returns_virtual_path(monkeypatch, tmp_path) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)
    monkeypatch.setattr(tools, "get_file_store", lambda: store)
    monkeypatch.setattr(
        tools,
        "_get_knowledge_base",
        lambda: SimpleNamespace(get_file_download=_async_get_file_download(b"%PDF-1.4 bytes", "report.pdf")),
    )

    runtime = SimpleNamespace(context=SimpleNamespace(file_thread_id="thread-1", uid="user-1"))
    result = await _run_download_kb_file(kb_id="db-1", file_id="file-1", runtime=runtime)

    stored = await store.read(thread_output_key("thread-1", "report.pdf"))
    assert stored.data == b"%PDF-1.4 bytes"
    assert stored.content_type == "application/octet-stream"
    assert result == {
        "virtual_path": "/home/gem/user-data/outputs/report.pdf",
        "filename": "report.pdf",
        "media_type": "application/octet-stream",
        "size_bytes": len(b"%PDF-1.4 bytes"),
        "saved_as": "report.pdf",
    }


@pytest.mark.asyncio
async def test_download_kb_file_passes_save_as_argument(monkeypatch, tmp_path) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)
    monkeypatch.setattr(tools, "get_file_store", lambda: store)
    monkeypatch.setattr(
        tools,
        "_get_knowledge_base",
        lambda: SimpleNamespace(get_file_download=_async_get_file_download(b"xlsx bytes", "origin.xlsx")),
    )

    runtime = SimpleNamespace(context=SimpleNamespace(file_thread_id="thread-1", uid="user-1"))
    result = await _run_download_kb_file(kb_id="db-1", file_id="file-1", save_as="renamed.xlsx", runtime=runtime)

    assert result["saved_as"] == "renamed.xlsx"
    assert (await store.read(thread_output_key("thread-1", "renamed.xlsx"))).data == b"xlsx bytes"


@pytest.mark.asyncio
async def test_download_kb_file_locks_name_selection_and_put(monkeypatch, tmp_path) -> None:
    store = LocalFileStore(tmp_path / "filestore")
    events = []

    @asynccontextmanager
    async def lock(thread_id: str):
        events.append(("enter", thread_id))
        yield
        events.append(("exit", thread_id))

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)
    monkeypatch.setattr(tools, "get_file_store", lambda: store)
    monkeypatch.setattr(tools, "file_thread_operation_lock", lock)
    monkeypatch.setattr(
        tools,
        "_get_knowledge_base",
        lambda: SimpleNamespace(get_file_download=_async_get_file_download(b"bytes", "report.pdf")),
    )

    runtime = SimpleNamespace(context=SimpleNamespace(file_thread_id="thread-1", uid="user-1"))
    await _run_download_kb_file(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert events == [("enter", "thread-1"), ("exit", "thread-1")]


@pytest.mark.asyncio
async def test_download_kb_file_rejects_invisible_resource(monkeypatch) -> None:
    async def _visible_kbs(runtime):
        del runtime
        return [{"kb_id": "db-2", "name": "Other"}]

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _visible_kbs)

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_download_kb_file(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert "不存在或当前会话未启用" in result


@pytest.mark.asyncio
async def test_download_kb_file_rejects_readonly_knowledge_base(monkeypatch) -> None:
    """知识库层拒绝只读源下载时，工具应返回原始业务提示。"""
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)

    async def reject_readonly(*args, **kwargs):
        del args, kwargs
        raise ValueError("知识库只支持检索，不支持原文件下载")

    monkeypatch.setattr(
        tools,
        "_get_knowledge_base",
        lambda: SimpleNamespace(get_file_download=reject_readonly),
    )

    runtime = SimpleNamespace(context=SimpleNamespace(file_thread_id="thread-1", uid="user-1"))
    result = await _run_download_kb_file(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert "只支持检索" in result


@pytest.mark.asyncio
async def test_download_kb_file_requires_kb_and_file_id(monkeypatch) -> None:
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)
    runtime = SimpleNamespace(context=SimpleNamespace())

    assert "请提供 kb_id" in await _run_download_kb_file(kb_id="", file_id="file-1", runtime=runtime)
    assert "请提供 file_id" in await _run_download_kb_file(kb_id="db-1", file_id="", runtime=runtime)


@pytest.mark.asyncio
async def test_download_kb_file_missing_sandbox_context_returns_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", _fake_visible_kbs)
    monkeypatch.setattr(
        tools,
        "_get_knowledge_base",
        lambda: SimpleNamespace(get_file_download=_async_get_file_download(b"bytes", "report.pdf")),
    )

    runtime = SimpleNamespace(context=SimpleNamespace())
    result = await _run_download_kb_file(kb_id="db-1", file_id="file-1", runtime=runtime)

    assert "沙盒上下文" in result


@pytest.mark.asyncio
async def test_resolve_download_output_name_strips_directory_and_avoids_traversal(monkeypatch, tmp_path) -> None:
    """save_as 含目录或路径穿越时，必须被剥离成纯文件名并落在 outputs 下。"""
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "get_file_store", lambda: store)

    data = {"filename": "report.pdf"}
    name = await tools._resolve_download_output_name("thread-1", data, "file-1", "../../../etc/passwd")

    assert name == "passwd"
    assert "/" not in name


@pytest.mark.asyncio
async def test_resolve_download_output_name_appends_suffix_on_conflict(monkeypatch, tmp_path) -> None:
    """目标文件名已存在时，追加 _1 / _2 后缀直到不冲突。"""
    store = LocalFileStore(tmp_path / "filestore")
    monkeypatch.setattr(tools, "get_file_store", lambda: store)
    await store.put(thread_output_key("thread-1", "report.pdf"), b"existing")
    await store.put(thread_output_key("thread-1", "report_1.pdf"), b"existing")

    data = {"filename": "report.pdf"}
    name = await tools._resolve_download_output_name("thread-1", data, "file-1", None)

    assert name == "report_2.pdf"


def _async_get_file_download(content: bytes, filename: str):
    async def _impl(kb_id: str, file_id: str, variant: str = "original"):
        del kb_id, file_id, variant
        return {
            "filename": filename,
            "content": content,
            "media_type": "application/octet-stream",
        }

    return _impl
