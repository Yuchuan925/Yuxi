"""真实 worker、PostgreSQL、MinIO 与 Milvus 3 的检索闭环。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
import pymilvus
from pymilvus import Collection, connections, utility

from test.e2e.e2e_helpers import wait_model_provider_cache
from yuxi.bootstrap.models import load_models
from yuxi.infrastructure.postgres.manager import pg_manager
from yuxi.modules.knowledge.repositories.chunks import KnowledgeChunkRepository
from yuxi.modules.knowledge.repositories.files import KnowledgeFileRepository
from yuxi.modules.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.modules.knowledge.graphs.graph_utils import graph_entity_collection_name, graph_triple_collection_name

load_models()
pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _wait_task(client, headers, task_id):
    """等待真实 worker 的任务终态，失败直接暴露持久结果。"""
    async with asyncio.timeout(120):
        while True:
            response = await client.get(f"/api/tasks/{task_id}", headers=headers)
            assert response.status_code == 200, response.text
            task = response.json()["task"]
            if task["status"] in {"success", "failed", "cancelled"}:
                assert task["status"] == "success", task
                return
            await asyncio.sleep(0.2)


async def test_worker_index_and_all_search_modes_use_text_constraints(e2e_client, e2e_headers):
    """索引后回读正文，验证三通道、中文、短语、高亮、重建和删除。"""

    class EmbeddingReplay(BaseHTTPRequestHandler):
        """提供固定四维向量，让文本约束拥有独立的检索 oracle。"""

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            inputs = payload["input"]
            if isinstance(inputs, str):
                inputs = [inputs]
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [1, 0, 0, 0]} for i in range(len(inputs))
                    ],
                    "model": "replay-embedding",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    replay = ThreadingHTTPServer(("0.0.0.0", 0), EmbeddingReplay)
    thread = threading.Thread(target=replay.serve_forever, daemon=True)
    thread.start()
    provider_id = f"ci-milvus-{uuid4().hex[:8]}"
    kb_id = None
    provider_created = False
    alias = provider_id
    pg_manager.initialize()
    try:
        created = await e2e_client.post(
            "/api/system/model-providers",
            headers=e2e_headers,
            json={
                "provider_id": provider_id,
                "display_name": "Milvus E2E embedding",
                "provider_type": "openai",
                "base_url": f"http://api:{replay.server_port}/v1",
                "embedding_base_url": f"http://api:{replay.server_port}/v1/embeddings",
                "api_key": "ci-replay-key",
                "capabilities": ["embedding"],
                "is_enabled": True,
                "enabled_models": [
                    {"id": "replay-embedding", "display_name": "Replay embedding", "type": "embedding", "dimension": 4}
                ],
            },
        )
        assert created.status_code == 200, created.text
        provider_created = True
        await wait_model_provider_cache()
        created = await e2e_client.post(
            "/api/knowledge/databases",
            headers=e2e_headers,
            json={
                "database_name": f"pytest_milvus3_{uuid4().hex}",
                "description": "Milvus text E2E",
                "kb_type": "milvus",
                "embedding_model_spec": f"{provider_id}:replay-embedding",
                "additional_params": {},
            },
        )
        assert created.status_code == 200, created.text
        kb_id = created.json()["kb_id"]
        samples = {
            "match.md": "Milvus vector database production reference.",
            "reversed.md": "Milvus database vector production reference.",
            "excluded.md": "Milvus vector database legacy reference.",
            "unrelated.md": "PostgreSQL transactional storage reference.",
            "chinese.md": "知识库支持关键词检索以及混合检索。",
        }
        file_ids = {}
        for name, text in samples.items():
            uploaded = await e2e_client.post(
                f"/api/knowledge/files/upload?kb_id={kb_id}",
                headers=e2e_headers,
                files={"file": (name, text.encode(), "text/markdown")},
            )
            assert uploaded.status_code == 200, uploaded.text
            info = uploaded.json()
            added = await e2e_client.post(
                f"/api/knowledge/databases/{kb_id}/documents/add",
                headers=e2e_headers,
                json={
                    "items": [info["file_path"]],
                    "params": {
                        "content_hashes": {info["file_path"]: info["content_hash"]},
                        "file_sizes": {info["file_path"]: info["size"]},
                    },
                },
            )
            assert added.status_code == 200, added.text
            file_ids[name] = added.json()["items"][0]["file_id"]

        async def index_documents():
            """通过公共入口触发索引并等待 owning worker 的终态。"""
            indexed = await e2e_client.post(
                f"/api/knowledge/databases/{kb_id}/documents/index",
                headers=e2e_headers,
                json={"file_ids": list(file_ids.values()), "params": {}},
            )
            assert indexed.status_code == 200, indexed.text
            await _wait_task(e2e_client, e2e_headers, indexed.json()["task_id"])

        parsed = await e2e_client.post(
            f"/api/knowledge/databases/{kb_id}/documents/parse",
            headers=e2e_headers,
            json={"file_ids": list(file_ids.values()), "params": {}},
        )
        assert parsed.status_code == 200, parsed.text
        await _wait_task(e2e_client, e2e_headers, parsed.json()["task_id"])
        await index_documents()
        repository = KnowledgeChunkRepository()
        for name, file_id in file_ids.items():
            record = await KnowledgeFileRepository().get_by_file_id(file_id)
            assert record.status == "indexed", (name, record.status)
            [chunk] = await repository.list_by_file_id(file_id)
            assert chunk.content == samples[name]
        connections.connect(
            alias=alias,
            uri=os.environ["MILVUS_URI"],
            token=os.getenv("MILVUS_TOKEN", ""),
            db_name=os.getenv("MILVUS_DB", "yuxi"),
        )
        collection = Collection(kb_id, using=alias)
        assert pymilvus.__version__ == "3.0.2"
        assert utility.get_server_version(using=alias) == "3.0.2"
        collection.load()
        stored = collection.query(expr='id != ""', output_fields=["content", "file_id"], consistency_level="Strong")
        assert {row["content"] for row in stored} == set(samples.values())

        graph_store = MilvusGraphVectorStore()
        for record_type, records in (
            ("entity", [{"id": "e1", "content": "Milvus vector database"}]),
            ("triple", [{"id": "t1", "content": "Milvus supports retrieval", "source_id": "e1", "target_id": "e2"}]),
        ):
            await graph_store.upsert_graph_records(
                kb_id=kb_id,
                embedding_model_spec=f"{provider_id}:replay-embedding",
                record_type=record_type,
                records=records,
            )
        [entity] = await graph_store.search_entities(
            kb_id=kb_id,
            query_text="Milvus",
            embedding_model_spec=f"{provider_id}:replay-embedding",
            top_k=10,
        )
        [triple] = await graph_store.search_triples(
            kb_id=kb_id,
            query_text="Milvus",
            embedding_model_spec=f"{provider_id}:replay-embedding",
            top_k=10,
        )
        assert entity["id"] == "e1" and entity["content"] == "Milvus vector database"
        assert triple["id"] == "t1" and triple["source_id"] == "e1" and triple["target_id"] == "e2"
        assert all(
            utility.has_collection(name, using=alias)
            for name in (
                graph_entity_collection_name(kb_id),
                graph_triple_collection_name(kb_id),
            )
        )

        async def query(mode, **options):
            """从 HTTP 返回读取真实服务端检索结果。"""
            response = await e2e_client.post(
                f"/api/knowledge/databases/{kb_id}/query-test",
                headers=e2e_headers,
                json={
                    "query": options.pop("query", "Milvus"),
                    "meta": {
                        "search_mode": mode,
                        "final_top_k": 10,
                        "similarity_threshold": 0,
                        "use_reranker": False,
                        "use_graph_retrieval": False,
                        **options,
                    },
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert len(await query("vector")) == 5
        for mode in ("vector", "keyword", "hybrid"):
            [result] = await query(
                mode, required_terms="Milvus", excluded_terms="legacy", exact_phrase="vector database"
            )
            assert result["metadata"]["file_id"] == file_ids["match.md"]
            assert result["content"] == samples["match.md"]
            assert result["score_type"] == {"vector": "cosine", "keyword": "bm25", "hybrid": "hybrid"}[mode]
            assert any(segment["matched"] for fragment in result["highlights"] for segment in fragment)
            external = await e2e_client.post(
                f"/api/v1/knowledge/databases/external/{kb_id}/retrieve",
                headers=e2e_headers,
                json={
                    "query": "Milvus",
                    "options": {
                        "search_mode": mode,
                        "required_terms": "Milvus",
                        "excluded_terms": "legacy",
                        "exact_phrase": "vector database",
                        "use_graph_retrieval": False,
                        "use_reranker": False,
                    },
                },
            )
            assert external.status_code == 200, external.text
            [public_result] = external.json()["results"]
            assert public_result["file_id"] == file_ids["match.md"]
            assert public_result["content"] == result["content"]
            assert public_result["metadata"]["score_type"] == result["score_type"]
            assert public_result["metadata"]["highlights"] == result["highlights"]
            assert await query(mode, file_name="unrelated", required_terms="Milvus") == []
            [by_file] = await query(mode, file_name="match.md")
            assert by_file["metadata"]["file_id"] == file_ids["match.md"]
        [chinese] = await query("keyword", query="关键词")
        assert chinese["content"] == samples["chinese.md"]
        assert any(segment["matched"] for fragment in chinese["highlights"] for segment in fragment)
        assert await query("keyword", exact_phrase='absent") or file_id != "') == []
        for result in await query("hybrid", highlight_results=False):
            assert "highlights" not in result
            assert result["content"] in samples.values()
        for options in ({"required_terms": ["bad"]}, {"search_mode": "unsupported"}):
            invalid = await e2e_client.post(
                f"/api/knowledge/databases/{kb_id}/query-test",
                headers=e2e_headers,
                json={"query": "Milvus", "meta": options},
            )
            assert invalid.status_code == 400, invalid.text

        await index_documents()
        assert len(await repository.list_by_kb_id(kb_id)) == 5
        assert len(collection.query(expr='id != ""', output_fields=["file_id"], consistency_level="Strong")) == 5
        removed_file = file_ids["match.md"]
        deleted = await e2e_client.delete(
            f"/api/knowledge/databases/{kb_id}/documents/{removed_file}", headers=e2e_headers
        )
        assert deleted.status_code == 200, deleted.text
        assert await repository.list_by_file_id(removed_file) == []
        assert await KnowledgeFileRepository().get_by_file_id(removed_file) is None
        assert (
            collection.query(expr=f'file_id == "{removed_file}"', output_fields=["file_id"], consistency_level="Strong")
            == []
        )
        deleted = await e2e_client.delete(f"/api/knowledge/databases/{kb_id}", headers=e2e_headers)
        assert deleted.status_code == 200, deleted.text
        assert not utility.has_collection(kb_id, using=alias)
        assert not utility.has_collection(graph_entity_collection_name(kb_id), using=alias)
        assert not utility.has_collection(graph_triple_collection_name(kb_id), using=alias)
        assert await repository.list_by_kb_id(kb_id) == []
        kb_id = None
    finally:
        if kb_id:
            await e2e_client.delete(f"/api/knowledge/databases/{kb_id}", headers=e2e_headers)
        if provider_created:
            await e2e_client.delete(f"/api/system/model-providers/{provider_id}", headers=e2e_headers)
        connections.disconnect(alias)
        await asyncio.to_thread(replay.shutdown)
        thread.join(timeout=5)
        replay.server_close()
        await pg_manager.close()
