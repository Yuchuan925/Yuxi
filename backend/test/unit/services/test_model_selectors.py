import pytest

from yuxi.agents.models import load_chat_model
from yuxi.models.chat import select_model
from yuxi.models.embed import OtherEmbedding, select_embedding_model
from yuxi.models.rerank import OpenAIReranker, get_reranker
from yuxi.services.model_cache import ModelInfo


def _model_info(model_type: str) -> ModelInfo:
    return ModelInfo(
        provider_id="test-provider",
        model_id=f"namespace/{model_type}-model",
        model_type=model_type,
        display_name=f"Test {model_type}",
        api_key="test-key",
        base_url="https://example.com/v1",
        provider_type="openai",
        dimension=1024 if model_type == "embedding" else None,
    )


@pytest.mark.parametrize(
    "selector,args",
    [
        (select_model, {"model_spec": "unknown-provider:namespace/model"}),
        (load_chat_model, {"fully_specified_name": "unknown-provider:namespace/model"}),
        (select_embedding_model, {"model_id": "unknown-provider:namespace/model"}),
        (get_reranker, {"model_id": "unknown-provider:namespace/model"}),
    ],
)
def test_selectors_report_unknown_unconfigured_specs(selector, args):
    with pytest.raises(ValueError, match="Unknown|未找到模型"):
        selector(**args)


def test_select_embedding_model_loads_model_from_cache(monkeypatch):
    monkeypatch.setattr(
        "yuxi.models.embed.model_cache.get_model_info",
        lambda spec: _model_info("embedding") if spec == "test-provider:namespace/embedding-model" else None,
    )

    model = select_embedding_model("test-provider:namespace/embedding-model")

    assert isinstance(model, OtherEmbedding)
    assert model.model == "namespace/embedding-model"
    assert model.dimension == 1024


def test_get_reranker_loads_model_from_cache(monkeypatch):
    monkeypatch.setattr(
        "yuxi.models.rerank.model_cache.get_model_info",
        lambda spec: _model_info("rerank") if spec == "test-provider:namespace/rerank-model" else None,
    )

    reranker = get_reranker("test-provider:namespace/rerank-model")

    assert isinstance(reranker, OpenAIReranker)
    assert reranker.model == "namespace/rerank-model"
