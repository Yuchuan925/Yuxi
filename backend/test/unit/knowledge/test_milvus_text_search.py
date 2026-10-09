import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import yuxi.modules.knowledge.implementations.milvus as milvus_module
import yuxi.modules.knowledge.graphs.milvus_graph_vector_store as graph_module
from yuxi.modules.knowledge.implementations.milvus import MilvusKB
from test.unit.plugins.test_milvus_kb import FakeCollection, FakeHit, make_kb, make_query_config


@pytest.mark.parametrize("search_mode", ["vector", "keyword", "hybrid"])
async def test_text_constraints_reach_every_retrieval_channel(search_mode):
    """每个召回通道都受必含、排除和短语约束。"""
    collection = FakeCollection()
    kb = make_kb(collection)
    await kb.aquery(
        "query",
        "db",
        config=make_query_config(),
        search_mode=search_mode,
        required_terms="Milvus LangGraph",
        excluded_terms="legacy",
        exact_phrase="知识 检索",
    )
    calls = collection.search_calls if search_mode != "hybrid" else collection.hybrid_calls[0]["reqs"]
    for call in calls:
        expression = call["expr"] if isinstance(call, dict) else call.expr
        assert 'TEXT_MATCH(content, "Milvus") and TEXT_MATCH(content, "LangGraph")' in expression
        assert 'not TEXT_MATCH(content, "legacy")' in expression
        assert 'PHRASE_MATCH(content, "知识 检索", 0)' in expression


def test_filter_literals_escape_expression_syntax():
    """用户文本保持一个字面值，不能变成另一条过滤条件。"""
    kb = object.__new__(MilvusKB)
    phrase = 'a"\\\n) or file_id != ""'
    expression, _ = kb._build_text_search({"exact_phrase": phrase}, "keyword")
    literal = expression.removeprefix("PHRASE_MATCH(content, ").removesuffix(", 0)")
    assert json.loads(literal) == phrase
    assert "\n" not in expression


@pytest.mark.parametrize(
    "options",
    [
        {"required_terms": ["bad"]},
        {"excluded_terms": 1},
        {"exact_phrase": None},
        {"highlight_results": "false"},
    ],
)
def test_invalid_text_options_fail_explicitly(options):
    """非法协议类型不得静默扩大查询范围。"""
    with pytest.raises(ValueError):
        object.__new__(MilvusKB)._build_text_search(options, "keyword")


def test_highlight_segments_preserve_full_content_and_literal_markup():
    """服务端标记被转换为文字段，正文不受高亮处理影响。"""
    hit = FakeHit("<script>source</script>完整正文", 3.5)
    hit.highlight = {"content": {"fragments": ["<img src=x>\ue000Milvus\ue001 matches"], "scores": []}}
    chunk = object.__new__(MilvusKB)._build_chunk_from_hit(hit, 3.5, True, "bm25_score")
    assert chunk["content"] == "<script>source</script>完整正文"
    assert chunk["score_type"] == "bm25"
    assert chunk["highlights"] == [
        [
            {"text": "<img src=x>", "matched": False},
            {"text": "Milvus", "matched": True},
            {"text": " matches", "matched": False},
        ]
    ]


async def test_graph_candidates_cannot_bypass_text_constraints():
    """图谱候选必须再次匹配同一集合中的文本与文件条件。"""
    collection = FakeCollection()
    collection.query = lambda **kwargs: [{"chunk_id": "allowed"}]
    kb = make_kb(collection)
    kb._retrieve_graph_chunks = AsyncMock(
        return_value=[
            {"metadata": {"chunk_id": "allowed"}, "content": "Milvus", "score": 1},
            {"metadata": {"chunk_id": "excluded"}, "content": "other", "score": 1},
        ]
    )
    chunks = await kb.aquery(
        "query",
        "db",
        config=make_query_config(),
        search_mode="keyword",
        required_terms="Milvus",
        use_graph_retrieval=True,
    )
    assert "excluded" not in [chunk["metadata"]["chunk_id"] for chunk in chunks]
    assert "allowed" in [chunk["metadata"]["chunk_id"] for chunk in chunks]
    assert all(chunk["score_type"] == "fusion" for chunk in chunks)


@pytest.mark.parametrize("module,executor", [(milvus_module, MilvusKB), (graph_module, graph_module.MilvusGraphVectorStore)])
def test_database_operations_use_own_connection_and_fail_closed(monkeypatch, module, executor):
    """数据库切换失败传给调用方，不退回默认库。"""
    selected = {}

    def select_database(name, *, using):
        """模拟数据库选择失败。"""
        selected[using] = name
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(module.connections, "connect", lambda **kwargs: None)
    monkeypatch.setattr(module.connections, "has_connection", lambda alias: True)
    monkeypatch.setattr(module.db, "list_database", lambda *, using: ["target"] if using == "own" else [])
    monkeypatch.setattr(module.db, "using_database", select_database)
    instance = object.__new__(executor)
    instance.connection_alias = "own"
    instance.milvus_uri = "http://milvus:19530"
    instance.milvus_token = ""
    instance.milvus_db = "target"
    with pytest.raises(RuntimeError, match="database unavailable"):
        instance._init_connection()
    assert selected == {"own": "target"}


async def test_collection_load_failure_is_observable():
    """加载失败不得被当作可查询的集合。"""

    def fail_load():
        """模拟集合加载失败。"""
        raise RuntimeError("load unavailable")

    with pytest.raises(RuntimeError, match="load unavailable"):
        await object.__new__(MilvusKB)._initialize_kb_instance(SimpleNamespace(load=fail_load))


@pytest.mark.parametrize("mismatch", ["model", "dimension", "missing_embedding", "text_schema"])
def test_incompatible_collection_is_rejected_without_deletion(monkeypatch, mismatch):
    """不匹配的集合显式失败，查询入口不能删除已存在的数据。"""
    collection = SimpleNamespace(
        description="Knowledge base collection for db using model",
        schema=SimpleNamespace(fields=[SimpleNamespace(name="embedding", params={"dim": 4})]),
    )
    if mismatch == "model":
        collection.description = "different model"
    elif mismatch == "dimension":
        collection.schema.fields[0].params["dim"] = 8
    elif mismatch == "missing_embedding":
        collection.schema.fields = []
    monkeypatch.setattr(milvus_module.utility, "has_collection", lambda *args, **kwargs: True)
    monkeypatch.setattr(milvus_module, "Collection", lambda **kwargs: collection)
    monkeypatch.setattr(
        milvus_module.model_cache,
        "get_model_info",
        lambda spec: SimpleNamespace(
            model_type="embedding",
            model_id="model",
            dimension=4,
        ),
    )

    def reject_drop(*args, **kwargs):
        """错误的破坏性修复必须使测试失败。"""
        pytest.fail("collection was deleted")

    monkeypatch.setattr(milvus_module.utility, "drop_collection", reject_drop)
    kb = object.__new__(MilvusKB)
    kb.connection_alias = "own"
    kb._collection_supports_text_retrieval = lambda candidate: mismatch != "text_schema"
    kb._create_new_collection = reject_drop
    with pytest.raises(RuntimeError, match="does not match"):
        kb._create_kb_instance_sync("db", "provider:model")


async def test_hybrid_highlighting_preserves_ranking_and_scores():
    """补取高亮只能附加片段，不能用第二次搜索的分数覆盖融合结果。"""
    fusion_hits = [FakeHit("first", 0.7), FakeHit("second", 0.5)]
    highlighted_hits = [FakeHit("second", 1), FakeHit("first", 1)]
    for hit, chunk_id in zip(fusion_hits, ["a", "b"]):
        hit.entity["chunk_id"] = chunk_id
    for hit, chunk_id in zip(highlighted_hits, ["b", "a"]):
        hit.entity["chunk_id"] = chunk_id
        hit.highlight = {"content": {"fragments": [f"\ue000{hit.entity.get('content')}\ue001"], "scores": []}}
    collection = FakeCollection()
    collection.search = lambda **kwargs: [highlighted_hits]
    collection.hybrid_search = lambda **kwargs: [fusion_hits]
    chunks = await make_kb(collection).aquery("query", "db", config=make_query_config(), search_mode="hybrid")
    assert [chunk["metadata"]["chunk_id"] for chunk in chunks] == ["a", "b"]
    assert [chunk["score"] for chunk in chunks] == [0.7, 0.5]
    assert all(chunk["score_type"] == "hybrid" for chunk in chunks)
    assert chunks[0]["highlights"] == [[{"text": "first", "matched": True}]]
