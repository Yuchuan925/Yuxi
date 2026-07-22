"""OCR 方法选择、运行时配置和健康检测。"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.knowledge.parser.registry import PROCESSOR_METADATA, get_supported_extensions
from yuxi.config.options import (
    mineru_ocr_host_opts,
    mineru_official_api_opts,
    paddleocr_api_opts,
    pp_structure_v3_ocr_host_opts,
)


def get_ocr_options() -> dict[str, Any]:
    from yuxi import config

    return {
        "default_engine": config.default_ocr_engine,
        "engines": [
            {
                "engine_id": engine_id,
                "display_name": metadata["display_name"],
                "supported_extensions": get_supported_extensions(engine_id),
            }
            for engine_id, metadata in PROCESSOR_METADATA.items()
        ],
    }


def resolve_ocr_engine_id(engine_id: str | None = None) -> str:
    from yuxi import config

    resolved = str(engine_id or config.default_ocr_engine).strip() or config.default_ocr_engine
    if resolved == "disable":
        return resolved
    if resolved not in PROCESSOR_METADATA:
        raise ValueError(f"不支持的 OCR 引擎: {resolved}")
    return resolved


async def resolve_ocr_task_params(
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    resolved = dict(params or {})
    if resolved.get("ocr_engine") == "disable":
        resolved["_ocr_processor_kwargs"] = {}
        return resolved

    engine_id = resolve_ocr_engine_id(resolved.get("ocr_engine"))
    if db is None:
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as session:
            kwargs = await _build_processor_kwargs(session, engine_id)
    else:
        kwargs = await _build_processor_kwargs(db, engine_id)

    resolved["ocr_engine"] = engine_id
    resolved.pop("ocr_engine_config", None)
    resolved["_ocr_processor_kwargs"] = kwargs
    return resolved


async def check_all_ocr_health(db: AsyncSession) -> dict[str, Any]:
    """使用当前有效配置并行检查所有 OCR 方法。"""

    configured = []
    results = {}
    for engine_id in PROCESSOR_METADATA:
        try:
            kwargs = await _build_processor_kwargs(db, engine_id)
            configured.append((engine_id, kwargs))
        except Exception as exc:
            results[engine_id] = {"status": "error", "message": str(exc), "details": {}}

    async def check(engine_id: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            result = await asyncio.to_thread(DocumentProcessorFactory.check_health, engine_id, **kwargs)
        except Exception as exc:
            result = {"status": "error", "message": str(exc), "details": {}}
        return engine_id, result

    checked = await asyncio.gather(*(check(engine_id, kwargs) for engine_id, kwargs in configured))
    results.update(checked)
    return results


async def _build_processor_kwargs(db: AsyncSession, engine_id: str) -> dict[str, Any]:
    if engine_id == "mineru_ocr":
        opts = await mineru_ocr_host_opts.get(db)
        return {"server_url": opts["server_url"]} if opts["server_url"] else {}
    if engine_id == "mineru_official":
        opts = await mineru_official_api_opts.get(db)
        return {"api_key": opts["api_key"]} if opts["api_key"] else {}
    if engine_id == "pp_structure_v3_ocr":
        opts = await pp_structure_v3_ocr_host_opts.get(db)
        return {"server_url": opts["server_url"]} if opts["server_url"] else {}
    if engine_id == "deepseek_ocr":
        provider = _resolve_deepseek_provider()
        if provider is None or not provider.api_key:
            raise ValueError("siliconflow-cn 模型供应商凭证不可用")
        return {
            "api_key": provider.api_key,
            "api_url": f"{provider.base_url.rstrip('/')}/chat/completions",
        }
    if engine_id in {"paddleocr_vl_1_6", "paddleocr_pp_ocrv6"}:
        opts = await paddleocr_api_opts.get(db)
        return {key: value for key, value in opts.items() if value}
    return {}


def _resolve_deepseek_provider():
    from yuxi.models.providers.cache import model_cache

    models = model_cache.get_specs_grouped_by_provider().get("siliconflow-cn", [])
    return next((model for model in models if model.model_id == "deepseek-ai/DeepSeek-OCR"), None) or (
        models[0] if models else None
    )
