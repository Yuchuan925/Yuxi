"""分块参数在请求、文件和知识库配置边界的拒绝与继承契约。"""

import pytest

from yuxi.modules.knowledge.chunking.ragflow_like.presets import (
    ensure_chunk_defaults_in_additional_params,
    normalize_chunk_preset_id,
    resolve_chunk_processing_params,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["qa_typo", 123, False, 0, [], {}])
@pytest.mark.parametrize("source", ["knowledge_base", "file", "request"])
def test_invalid_preset_rejected_in_every_source(value, source):
    """错误配置不能因优先级或 falsy 值变成成功默认。"""
    sources = [{}, {}, {}]
    sources[["knowledge_base", "file", "request"].index(source)] = {"chunk_preset_id": value}
    with pytest.raises(ValueError, match="chunk_preset_id"):
        resolve_chunk_processing_params(*sources)


@pytest.mark.parametrize("value", [None, "", "general", "naive", " NAIVE "])
def test_default_and_internal_alias_remain_general(value):
    """缺省和已有内部别名继续映射通用策略。"""
    assert normalize_chunk_preset_id(value) == "general"


def test_optional_file_and_request_values_inherit_knowledge_base():
    """空选择继承知识库，None 配置不覆盖有效对象。"""
    result = resolve_chunk_processing_params(
        {"chunk_preset_id": "book", "chunk_parser_config": {"chunk_token_num": 256}},
        {"chunk_preset_id": "", "chunk_parser_config": None},
        {"chunk_preset_id": None},
    )
    assert result["chunk_preset_id"] == "book"
    assert result["chunk_parser_config"] == {"chunk_token_num": 256}
    assert ensure_chunk_defaults_in_additional_params({"chunk_parser_config": None})["chunk_parser_config"] == {}


def test_request_preset_overrides_invalid_file_preset():
    """最终生效的请求配置可覆盖历史文件坏值。"""
    result = resolve_chunk_processing_params({}, {"chunk_preset_id": "typo"}, {"chunk_preset_id": "qa"})
    assert result["chunk_preset_id"] == "qa"
