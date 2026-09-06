# 知识图谱：用关系检索补充文本证据

Yuxi 的知识图谱从已经入库的 Chunk 中抽取实体与关系，并在检索时提供一条可选的关系路径。图谱帮助系统发现跨段落关联，最终答案仍回到 PostgreSQL 中的 Chunk 正文和文件来源。

## 构建入口

图谱构建以已经完成索引的 Chunk 为输入：

```text
Indexed Chunk
   │
   ├─ 实体与关系抽取
   ├─ Neo4j：Chunk—MENTIONS→Entity 与实体关系
   ├─ Milvus：实体和三元组向量
   └─ PostgreSQL：图谱任务和处理状态
```

图谱构建是独立的可选动作。知识库完成文本索引后已经可以检索，管理者可以根据资料类型和任务需要再构建图谱。

## 两条检索路径

基础文本检索始终承担主路径。它可以组合向量召回、BM25 全文召回、相似度过滤和可选 Rerank。

当知识库配置开启 `use_graph_retrieval` 时，查询同时进入图谱路径：

1. 在 Milvus 中召回相关实体与三元组；
2. 将实体、三元组和基础检索结果作为种子；
3. 从 Neo4j 读取两跳子图；
4. 使用 Personalized PageRank 对相关 Chunk 排序；
5. 根据 `chunk_id` 回 PostgreSQL 读取正文；
6. 与文本结果融合后返回带来源的 Chunk。

```text
Query ── Text: vector + BM25 + rerank ───────────────┐
  │                                                   ├─ Chunk evidence
  └─ Graph: entity/triple → Neo4j → PPR → chunk_id ──┘
```

## 存储职责

- PostgreSQL 保存知识库配置、权限、文件状态、Chunk 正文和图谱处理状态。
- MinIO 保存原件、解析后的 Markdown 和解析图片。
- Milvus 保存 Chunk、实体和三元组的向量以及 BM25 字段。
- Neo4j 保存实体关系和 Chunk 关联。
- Redis 只缓存最小运行配置，不拥有图谱或检索结果。

## 权限边界

Agent 能使用的知识库取当前用户权限与 Agent 配置的交集。图谱查询遵循知识库读取权限，构建、重置和修复索引需要管理权限。工具传入的 `kb_id`、`file_id` 和文件名必须属于当前运行的可见快照。对象 URL 只定位内容，不承担授权。

## 失败与观察

图谱检索失败时，系统记录错误并继续使用文本检索结果。没有图谱结果时，需要同时检查功能开关、构建状态、实体/三元组向量和 Neo4j 数据。图谱构建跨 PostgreSQL、Milvus 和 Neo4j，没有跨存储事务；任务完成后要分别核对状态和数据。

LITE 模式不会加载知识库、图谱和 `knowledge-base` Skill。

## 对外一句话

图谱负责发现关系，Chunk 负责保存原文，检索结果始终可以回到来源证据。

## 源码定位与验证

- [知识库机制](./knowledge-base.md)
- [Milvus 知识库实现](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/knowledge/implementations/milvus.py)
- [图谱服务](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/knowledge/graphs/milvus_graph_service.py)
- [图谱向量存储](https://github.com/xerrors/Yuxi/blob/main/backend/package/yuxi/knowledge/graphs/milvus_graph_vector_store.py)
- [知识图谱配置与运维](../advanced/knowledge-base-graph.md)
