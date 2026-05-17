from __future__ import annotations

import asyncio
import json
from typing import Any

from yuxi.knowledge.graphs.extractors import GraphExtractor, GraphExtractorFactory, normalize_extraction_result
from yuxi.knowledge.graphs.graph_utils import build_graph_payload, cypher_merge_chunk, cypher_merge_entity_mention, cypher_merge_relation, normalize_entity_name
from yuxi.knowledge.graphs.neo4j_utils import Neo4jConnectionManager, get_shared_neo4j_connection, neo4j_write, safe_neo4j_label
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_isoformat

GRAPH_CONFIG_KEY = "graph_build_config"
GRAPH_TASK_TYPE = "knowledge_graph_index"


class MilvusGraphService:
    def __init__(
        self,
        *,
        db_id: str | None = None,
        kb_repo: KnowledgeBaseRepository | None = None,
        chunk_repo: KnowledgeChunkRepository | None = None,
        neo4j_connection: Neo4jConnectionManager | None = None,
    ):
        self.db_id = db_id
        self.kb_repo = kb_repo or KnowledgeBaseRepository()
        self.chunk_repo = chunk_repo or KnowledgeChunkRepository()
        self._connection = neo4j_connection

    @property
    def connection(self) -> Neo4jConnectionManager:
        if self._connection is None:
            self._connection = get_shared_neo4j_connection()
        return self._connection

    @property
    def driver(self):
        return self.connection.driver

    async def get_status(self, db_id: str) -> dict[str, Any]:
        kb = await self._get_milvus_kb(db_id)
        params = dict(kb.additional_params or {})
        config = params.get(GRAPH_CONFIG_KEY) or {}
        total_chunks, pending_chunks, indexed_chunks = await asyncio.gather(
            self.chunk_repo.count_by_db_id(db_id),
            self.chunk_repo.count_graph_pending_by_db_id(db_id),
            self.chunk_repo.count_graph_indexed_by_db_id(db_id),
        )
        return {
            "db_id": db_id,
            "kb_type": kb.kb_type,
            "configured": bool(config),
            "locked": bool(config.get("locked")),
            "config": self._public_config(config),
            "total_chunks": total_chunks,
            "pending_chunks": pending_chunks,
            "indexed_chunks": indexed_chunks,
        }

    async def configure(
        self,
        db_id: str,
        extractor_type: str,
        extractor_options: dict[str, Any],
        created_by: str,
    ) -> dict:
        kb = await self._get_milvus_kb(db_id)
        additional_params = dict(kb.additional_params or {})
        existing_config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        if existing_config.get("locked"):
            raise ValueError("图谱抽取配置已锁定")

        GraphExtractorFactory.create(extractor_type, extractor_options)
        config = {
            "locked": True,
            "extractor_type": extractor_type,
            "extractor_options": extractor_options or {},
            "created_at": utc_isoformat(),
            "created_by": created_by,
        }
        additional_params[GRAPH_CONFIG_KEY] = config
        await self.kb_repo.update(db_id, {"additional_params": additional_params})
        return config

    async def build_pending_chunks(self, db_id: str, *, batch_size: int, context=None) -> dict[str, Any]:
        kb = await self._get_milvus_kb(db_id)
        config = self._get_locked_config(kb.additional_params or {})
        extractor = GraphExtractorFactory.create(config["extractor_type"], config.get("extractor_options") or {})
        total_pending = await self.chunk_repo.count_graph_pending_by_db_id(db_id)
        processed = 0
        failed = 0
        failed_chunk_ids: set[str] = set()

        while True:
            if context is not None:
                await context.raise_if_cancelled()
            chunks = await self.chunk_repo.list_graph_pending_by_db_id(db_id, batch_size)
            unprocessed = [c for c in chunks if c.chunk_id not in failed_chunk_ids]
            if not unprocessed:
                break

            for chunk in unprocessed:
                if context is not None:
                    await context.raise_if_cancelled()
                try:
                    extraction_result = await self._get_chunk_extraction_result(db_id, chunk, extractor)
                    await asyncio.to_thread(
                        self.write_chunk_graph,
                        db_id,
                        chunk,
                        extraction_result,
                    )
                    await self.chunk_repo.mark_graph_indexed(chunk.chunk_id)
                    processed += 1
                except Exception as exc:
                    logger.error(f"Chunk 图谱构建失败 chunk_id={chunk.chunk_id}: {exc}")
                    failed_chunk_ids.add(chunk.chunk_id)
                    failed += 1

                if context is not None:
                    completed = processed + failed
                    progress = 5.0 + min(90.0, completed / max(total_pending, 1) * 90.0)
                    await context.set_progress(progress, f"图谱构建 {completed}/{total_pending}，失败 {failed}")

        remaining = await self.chunk_repo.count_graph_pending_by_db_id(db_id)
        return {"db_id": db_id, "success": processed, "failed": failed, "remaining": remaining}

    async def _get_chunk_extraction_result(self, db_id: str, chunk, extractor: GraphExtractor) -> dict[str, Any]:
        extractor_type = extractor.extractor_type
        if chunk.extraction_result:
            return normalize_extraction_result(chunk.extraction_result, extractor_type)

        extraction_result = await extractor.extract(
            chunk.content,
            chunk_metadata={
                "db_id": db_id,
                "chunk_id": chunk.chunk_id,
                "file_id": chunk.file_id,
                "chunk_index": chunk.chunk_index,
            },
        )
        normalized_result = normalize_extraction_result(extraction_result, extractor_type)
        await self.chunk_repo.update_extraction_result(chunk.chunk_id, normalized_result)
        return normalized_result

    def write_chunk_graph(
        self,
        db_id: str,
        chunk,
        normalized_result: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        """将单个 chunk 的抽取结果写入 Neo4j。

        依次完成：Chunk 节点 → Entity 节点 + MENTIONS 边 → RELATION 边。
        """
        label = safe_neo4j_label(db_id)
        graph_payload = build_graph_payload(normalized_result)
        relation_extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
        entities = graph_payload["entities"]
        relations = graph_payload["relations"]
        entity_by_id = {entity["id"]: entity for entity in entities}
        content_preview = (chunk.content or "")[:300]

        # 预构建 Cypher 模板（同一 chunk 内复用）
        merge_chunk_cypher = cypher_merge_chunk(label)
        merge_entity_cypher = cypher_merge_entity_mention(label)
        merge_relation_cypher = cypher_merge_relation(label)

        def query(tx):
            # 1. MERGE Chunk 节点
            tx.run(
                merge_chunk_cypher,
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                db_id=db_id,
                chunk_index=chunk.chunk_index,
                content_preview=content_preview,
                start_char_pos=chunk.start_char_pos,
                end_char_pos=chunk.end_char_pos,
            )

            # 2. MERGE Entity 节点 + Chunk→Entity (MENTIONS)
            for entity in entities:
                tx.run(
                    merge_entity_cypher,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    db_id=db_id,
                    normalized_name=normalize_entity_name(entity["text"]),
                    entity_label=entity.get("label") or "Entity",
                    name=entity["text"],
                    attributes=json.dumps(entity.get("attributes") or [], ensure_ascii=False),
                )

            # 3. MERGE Entity→Entity (RELATION) 边
            for relation in relations:
                source = entity_by_id[relation["source"]]
                target = entity_by_id[relation["target"]]
                tx.run(
                    merge_relation_cypher,
                    db_id=db_id,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    source_name=normalize_entity_name(source["text"]),
                    source_label=source.get("label") or "Entity",
                    target_name=normalize_entity_name(target["text"]),
                    target_label=target.get("label") or "Entity",
                    relation_type=relation.get("label") or "RELATED_TO",
                    text=relation["text"],
                    extractor_type=relation_extractor_type,
                )

        neo4j_write(self.driver, query)
        return [entity["text"] for entity in entities], sorted({entity.get("label") or "Entity" for entity in entities})

    async def reset(self, db_id: str, *, clear_extraction_result: bool, clear_config: bool) -> dict[str, Any]:
        kb = await self._get_milvus_kb(db_id)
        await asyncio.to_thread(self.delete_graph, db_id)
        reset_chunks = await self.chunk_repo.reset_graph_state_by_db_id(db_id, clear_extraction_result)
        if clear_config:
            additional_params = dict(kb.additional_params or {})
            additional_params.pop(GRAPH_CONFIG_KEY, None)
            await self.kb_repo.update(db_id, {"additional_params": additional_params})
        return {
            "message": "图谱构建状态已重置",
            "status": "success",
            "reset_chunks": reset_chunks,
            "clear_extraction_result": clear_extraction_result,
            "clear_config": clear_config,
        }

    def delete_graph(self, db_id: str) -> None:
        label = safe_neo4j_label(db_id)

        def query(tx):
            tx.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n")

        neo4j_write(self.driver, query)

    async def query_nodes(self, db_id: str | None = None, *, keyword: str = "", max_depth: int = 1, max_nodes: int = 50) -> dict[str, Any]:
        effective_db_id = db_id or self.db_id
        if not effective_db_id:
            return {"nodes": [], "edges": []}

        label = safe_neo4j_label(effective_db_id)
        limit = max_nodes
        try:
            with self.driver.session() as session:
                result = session.run(self._build_query(label, keyword, limit, max_depth), keyword=keyword, limit=limit)
                return self._process_query_result(result, limit, effective_db_id)
        except Exception as e:
            logger.error(f"Milvus graph query failed: {e}")
            return {"nodes": [], "edges": []}

    async def get_labels(self, db_id: str | None = None) -> list[str]:
        effective_db_id = db_id or self.db_id
        if not effective_db_id:
            return []
        label = safe_neo4j_label(effective_db_id)

        cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        UNWIND labels(n) AS node_label
        WITH DISTINCT node_label
        WHERE node_label <> 'MilvusKB' AND node_label <> $db_id
        RETURN node_label
        ORDER BY node_label
        """
        try:
            records = neo4j_read(self.driver, cypher, db_id=effective_db_id)
            return [record["node_label"] for record in records]
        except Exception as e:
            logger.error(f"Failed to get Milvus graph labels: {e}")
            return []

    async def get_stats(self, db_id: str | None = None) -> dict[str, Any]:
        effective_db_id = db_id or self.db_id
        if not effective_db_id:
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}
        label = safe_neo4j_label(effective_db_id)

        stats_cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        WITH count(n) AS node_count
        OPTIONAL MATCH (:MilvusKB:`{label}`)-[r]->(:MilvusKB:`{label}`)
        RETURN node_count, count(r) AS edge_count
        """
        label_cypher = f"""
        MATCH (n:Entity:MilvusKB:`{label}`)
        WITH n.label AS entity_label, count(*) AS count
        RETURN entity_label, count
        ORDER BY count DESC
        """
        try:
            with self.driver.session() as session:
                stats = session.run(stats_cypher).single()
                label_stats = session.run(label_cypher)
                return {
                    "total_nodes": stats["node_count"] if stats else 0,
                    "total_edges": stats["edge_count"] if stats else 0,
                    "entity_types": [{"type": row["entity_label"], "count": row["count"]} for row in label_stats],
                }
        except Exception as e:
            logger.error(f"Failed to get Milvus graph stats: {e}")
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}

    async def _get_milvus_kb(self, db_id: str):
        kb = await self.kb_repo.get_by_id(db_id)
        if kb is None:
            raise ValueError(f"知识库 {db_id} 不存在")
        if (kb.kb_type or "").lower() != "milvus":
            raise ValueError("仅 Milvus 知识库支持独立图谱构建")
        return kb

    def _get_locked_config(self, additional_params: dict[str, Any]) -> dict[str, Any]:
        config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        if not config.get("locked"):
            raise ValueError("请先确认并锁定图谱抽取配置")
        if not config.get("extractor_type"):
            raise ValueError("图谱抽取配置缺少 extractor_type")
        return config

    def _public_config(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if not config:
            return None
        return {
            "locked": bool(config.get("locked")),
            "extractor_type": config.get("extractor_type"),
            "extractor_options": config.get("extractor_options") or {},
            "created_at": config.get("created_at"),
            "created_by": config.get("created_by"),
        }

    def _build_query(self, label: str, keyword: str, limit: int, max_depth: int) -> str:
        where = ""
        if keyword and keyword != "*":
            where = """
            WHERE toLower(coalesce(n.name, '')) CONTAINS toLower($keyword)
               OR toLower(coalesce(n.content_preview, '')) CONTAINS toLower($keyword)
               OR toLower(coalesce(n.chunk_id, '')) CONTAINS toLower($keyword)
            """

        if max_depth <= 0:
            return f"""
            MATCH (n:MilvusKB:`{label}`)
            {where}
            RETURN n AS h, null AS r, null AS t
            LIMIT $limit
            """

        return f"""
        MATCH (n:MilvusKB:`{label}`)
        {where}
        WITH n LIMIT $limit
        OPTIONAL MATCH (n)-[r]-(m:MilvusKB:`{label}`)
        RETURN n AS h, r AS r, m AS t
        LIMIT {limit * 10}
        """

    def _process_query_result(self, result, limit: int, db_id: str) -> dict[str, Any]:
        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        for record in result:
            for key in ("h", "t"):
                raw_node = record.get(key)
                if raw_node is None:
                    continue
                node = self._normalize_node(raw_node, db_id)
                if node and node["id"] not in node_ids:
                    nodes.append(node)
                    node_ids.add(node["id"])
            raw_edge = record.get("r")
            if raw_edge is not None:
                edge = self._normalize_edge(raw_edge)
                if edge and edge["id"] not in edge_ids:
                    edges.append(edge)
                    edge_ids.add(edge["id"])
            if len(nodes) >= limit:
                break

        return {"nodes": nodes[:limit], "edges": edges[: limit * 2]}

    def _normalize_node(self, raw_node: Any, db_id: str | None = None) -> dict[str, Any]:
        if hasattr(raw_node, "element_id"):
            node_id = raw_node.element_id
            labels = list(raw_node.labels)
            properties = dict(raw_node.items())
        elif isinstance(raw_node, dict):
            node_id = raw_node.get("id") or raw_node.get("element_id")
            labels = raw_node.get("labels", [])
            properties = raw_node.get("properties") or {k: v for k, v in raw_node.items() if k not in {"id", "labels"}}
        else:
            return {}

        effective_db_id = db_id or self.db_id
        db_label = properties.get("db_id") or effective_db_id
        filtered_labels = [label for label in labels if label not in {"MilvusKB", db_label}]
        entity_type = "Chunk" if "Chunk" in labels else properties.get("label", "Entity")
        name = properties.get("name") or properties.get("content_preview") or properties.get("chunk_id") or "Unknown"
        return {
            "id": node_id,
            "name": name,
            "original_id": node_id,
            "type": entity_type,
            "labels": filtered_labels,
            "properties": properties,
            "normalized": {
                "name": name,
                "type": entity_type,
                "source": "milvus",
            },
            "graph_type": "milvus",
        }

    def _normalize_edge(self, raw_edge: Any) -> dict[str, Any]:
        if hasattr(raw_edge, "element_id"):
            edge_id = raw_edge.element_id
            edge_type = raw_edge.type
            source_id = raw_edge.start_node.element_id
            target_id = raw_edge.end_node.element_id
            properties = dict(raw_edge.items())
            edge_type = properties.get("type") or edge_type
        elif isinstance(raw_edge, dict):
            edge_id = raw_edge.get("id")
            edge_type = raw_edge.get("type")
            source_id = raw_edge.get("source_id")
            target_id = raw_edge.get("target_id")
            properties = raw_edge.get("properties", {})
        else:
            return {}

        return {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "type": edge_type,
            "properties": properties,
            "normalized": {
                "type": edge_type,
                "direction": "directed",
            },
        }