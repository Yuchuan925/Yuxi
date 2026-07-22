from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.knowledge.parser.factory as factory_module
import yuxi.knowledge.parser.unified as unified
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.knowledge.utils.kb_utils import resolve_processing_params
from yuxi.services.ocr_config_cache import OCRRuntimeConfig


def test_parser_params_merge_system_defaults_before_file_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("yuxi.services.ocr_config_service.get_default_ocr_engine", lambda: "rapid_ocr")
    monkeypatch.setattr(
        "yuxi.services.ocr_config_service.resolve_ocr_default_params",
        lambda engine: {"det_box_thresh": 0.4, "zoom_x": 2.0},
    )

    engine, params = unified._resolve_ocr_engine_params({"ocr_engine_config": {"det_box_thresh": 0.6}, "zoom_y": 3.0})

    assert engine == "rapid_ocr"
    assert params["det_box_thresh"] == 0.6
    assert params["zoom_x"] == 2.0
    assert params["zoom_y"] == 3.0


def test_factory_cache_key_does_not_contain_credential():
    cache_key = DocumentProcessorFactory._build_cache_key("deepseek_ocr", {"api_key": "top-secret"})

    assert cache_key.startswith("deepseek_ocr|")
    assert "top-secret" not in cache_key


def test_clear_cache_can_target_single_engine(monkeypatch: pytest.MonkeyPatch):
    first = SimpleNamespace()
    second = SimpleNamespace()
    monkeypatch.setattr(
        factory_module,
        "_PROCESSOR_CACHE",
        {"rapid_ocr|one": first, "mineru_ocr|two": second},
    )

    DocumentProcessorFactory.clear_cache("rapid_ocr")

    assert factory_module._PROCESSOR_CACHE == {"mineru_ocr|two": second}


def test_disabled_engine_snapshot_remains_readable_but_cannot_execute(monkeypatch: pytest.MonkeyPatch):
    runtime = OCRRuntimeConfig(
        engine_id="rapid_ocr",
        enabled=False,
        is_default=False,
        default_params={"det_box_thresh": 0.4},
    )
    monkeypatch.setattr("yuxi.services.ocr_config_service.ocr_config_cache.get", lambda engine_id: runtime)

    resolved = resolve_processing_params(
        kb_additional_params=None,
        file_processing_params={
            "ocr_engine": "rapid_ocr",
            "ocr_engine_config": {"det_box_thresh": 0.6},
        },
    )

    assert resolved["ocr_engine"] == "rapid_ocr"
    assert resolved["ocr_engine_config"]["det_box_thresh"] == 0.6
    with pytest.raises(ValueError, match="OCR 引擎已停用"):
        unified._resolve_ocr_engine_params(resolved)
