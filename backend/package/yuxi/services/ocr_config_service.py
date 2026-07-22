"""OCR 引擎配置用例层。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.parser.config_schemas import validate_default_params, validate_endpoint
from yuxi.knowledge.parser.registry import PROCESSOR_METADATA
from yuxi.repositories.ocr_config_repository import clear_default_ocr_config, get_ocr_config, list_ocr_configs
from yuxi.services.ocr_config_cache import OCRRuntimeConfig, ocr_config_cache
from yuxi.services.ocr_credential_crypto import encrypt_ocr_credential
from yuxi.storage.postgres.models_business import OCREngineConfig

VALID_CREDENTIAL_SOURCES = {None, "environment", "database", "model_provider"}


async def ensure_ocr_configs_in_db(db: AsyncSession) -> list[OCREngineConfig]:
    """补齐内置 OCR 配置，并修复由代码固定的端点与凭证策略。"""

    from yuxi import config

    records = await list_ocr_configs(db)
    existing = {record.engine_id: record for record in records}
    has_default = any(record.is_default for record in records)
    default_engine = config.default_ocr_engine if config.default_ocr_engine in PROCESSOR_METADATA else "rapid_ocr"

    for engine_id, metadata in PROCESSOR_METADATA.items():
        if engine_id in existing:
            if not metadata.get("endpoint_editable", False):
                existing[engine_id].endpoint = metadata["endpoint"]
            credential_sources = metadata.get("credential_sources", [])
            fixed_source = credential_sources[0] if len(credential_sources) == 1 else None
            if fixed_source:
                existing[engine_id].credential_source = fixed_source
                existing[engine_id].credential_ref = metadata["credential_ref"]
                existing[engine_id].credential_value = None
            continue
        record = OCREngineConfig(
            engine_id=engine_id,
            enabled=bool(metadata["enabled"]),
            is_default=not has_default and engine_id == default_engine,
            endpoint=metadata["endpoint"],
            credential_source=metadata["credential_source"],
            credential_ref=metadata["credential_ref"],
            credential_value=None,
            default_params=dict(metadata["default_params"]),
            created_by="system",
            updated_by="system",
        )
        db.add(record)
        records.append(record)

    if not any(record.is_default for record in records):
        fallback = next(record for record in records if record.engine_id == default_engine)
        fallback.is_default = True
    await db.flush()
    return records


async def rebuild_ocr_config_cache(db: AsyncSession) -> list[OCREngineConfig]:
    """从 PostgreSQL 重建本地缓存，并尽力发布脱敏 Redis 快照。"""

    records = await list_ocr_configs(db)
    try:
        ocr_config_cache.rebuild(records)
    except RuntimeError:
        # PostgreSQL 是事实来源；Redis 故障不能阻止当前进程使用已提交配置。
        ocr_config_cache.refresh_local(records)
    return records


async def sync_ocr_config_cache() -> None:
    """周期性从 PostgreSQL 刷新配置和数据库凭证。"""

    while True:
        try:
            await refresh_ocr_config_cache_from_db()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from yuxi.utils.logging_config import logger

            logger.warning(f"Failed to refresh OCR config cache from PostgreSQL: {exc}")
        await asyncio.sleep(5)


async def refresh_ocr_config_cache_from_db() -> None:
    """执行一次 PostgreSQL 到本进程缓存的同步。"""

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        records = await list_ocr_configs(session)
    ocr_config_cache.refresh_local(records)


def serialize_admin_config(record: OCREngineConfig) -> dict[str, Any]:
    """序列化管理员视图，不返回数据库密钥。"""

    metadata = PROCESSOR_METADATA[record.engine_id]
    credential_sources = metadata.get("credential_sources", [])
    credential_statuses = {source: _credential_status_for_source(record, source) for source in credential_sources}
    credential_status = (
        credential_statuses.get(record.credential_source, "missing") if record.credential_source else "not_required"
    )
    credential_refs = (
        {metadata["credential_source"]: metadata["credential_ref"]}
        if metadata["credential_source"] and metadata["credential_ref"]
        else {}
    )
    return {
        "engine_id": record.engine_id,
        "display_name": metadata["display_name"],
        "supported_extensions": metadata["supported_extensions"],
        "enabled": bool(record.enabled),
        "is_default": bool(record.is_default),
        "endpoint": record.endpoint,
        "endpoint_editable": metadata.get("endpoint_editable", False),
        "credential_source": record.credential_source,
        "credential_ref": record.credential_ref,
        "credential_sources": credential_sources,
        "credential_refs": credential_refs,
        "credential_source_fixed": len(credential_sources) == 1,
        "credential_status": credential_status,
        "credential_statuses": credential_statuses,
        "default_params": dict(record.default_params or {}),
    }


def _credential_status_for_source(record: OCREngineConfig, source: str) -> str:
    """检查一个凭证来源是否具备可用引用，不返回凭证内容。"""

    if source == "environment":
        metadata = PROCESSOR_METADATA[record.engine_id]
        env_ref = metadata["credential_ref"] if metadata["credential_source"] == source else None
        return "configured" if env_ref and os.getenv(env_ref) else "missing"
    if source == "model_provider":
        from yuxi.models.providers.cache import model_cache

        metadata = PROCESSOR_METADATA[record.engine_id]
        provider_ref = metadata["credential_ref"] if metadata["credential_source"] == source else None
        providers = model_cache.get_specs_grouped_by_provider()
        models = providers.get(provider_ref or "", [])
        return "configured" if any(model.api_key for model in models) else "missing"
    if source == "database":
        return "configured" if record.credential_value else "missing"
    return "missing"


async def get_ocr_options(db: AsyncSession) -> dict[str, Any]:
    """返回普通用户可见的脱敏 OCR 选项。"""

    records = await list_ocr_configs(db)
    return {
        "default_engine": next((record.engine_id for record in records if record.is_default), "rapid_ocr"),
        "engines": [
            {
                "engine_id": record.engine_id,
                "display_name": PROCESSOR_METADATA[record.engine_id]["display_name"],
                "enabled": bool(record.enabled) or record.engine_id == "disable",
                "supported_extensions": PROCESSOR_METADATA[record.engine_id]["supported_extensions"],
            }
            for record in records
        ],
    }


async def update_ocr_config(
    db: AsyncSession,
    engine_id: str,
    data: dict[str, Any],
    updated_by: str,
) -> OCREngineConfig | None:
    """校验并更新一个 OCR 引擎配置。"""

    if engine_id not in PROCESSOR_METADATA:
        raise ValueError(f"不支持的 OCR 引擎: {engine_id}")
    if data.get("is_default") is True:
        # 默认项有唯一索引；锁住完整配置集合可串行化并发默认切换。
        locked_records = await list_ocr_configs(db, for_update=True)
        record = next((item for item in locked_records if item.engine_id == engine_id), None)
    else:
        record = await get_ocr_config(db, engine_id, for_update=True)
    if record is None:
        return None

    metadata = PROCESSOR_METADATA[engine_id]
    enabled, is_default = _resolve_engine_state(engine_id, record, data)
    credential_source, credential_ref, credential_value = _resolve_credential_config(
        engine_id,
        metadata,
        record,
        data,
    )
    endpoint = _resolve_endpoint(engine_id, metadata, record, data)

    record.enabled = enabled
    record.endpoint = endpoint
    record.credential_source = credential_source
    record.credential_ref = credential_ref
    record.credential_value = credential_value
    record.default_params = validate_default_params(engine_id, data.get("default_params", record.default_params))
    record.updated_by = updated_by
    if is_default and not record.is_default:
        await clear_default_ocr_config(db)
        record.is_default = True
    await db.flush()
    return record


def _resolve_engine_state(
    engine_id: str,
    record: OCREngineConfig,
    data: dict[str, Any],
) -> tuple[bool, bool]:
    """合并并校验启停与默认项状态。"""

    enabled_value = data.get("enabled")
    default_value = data.get("is_default")
    enabled = bool(record.enabled if enabled_value is None else enabled_value)
    is_default = bool(record.is_default if default_value is None else default_value)
    if engine_id == "disable":
        enabled = True
    if record.is_default and data.get("is_default") is False:
        raise ValueError("请先将其他 OCR 引擎设为默认项")
    if record.is_default and not enabled and not is_default:
        raise ValueError("不能停用当前默认 OCR 引擎")
    if is_default and not enabled:
        raise ValueError("默认 OCR 引擎必须处于启用状态")
    return enabled, is_default


def _resolve_credential_config(
    engine_id: str,
    metadata: dict[str, Any],
    record: OCREngineConfig,
    data: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """解析凭证来源、引用和加密后的数据库密钥。"""

    credential_source = data.get("credential_source", record.credential_source)
    if credential_source not in VALID_CREDENTIAL_SOURCES:
        raise ValueError("credential_source 必须是 environment、database、model_provider 或 null")

    credential_sources = metadata.get("credential_sources", [])
    fixed_source = credential_sources[0] if len(credential_sources) == 1 else None
    if fixed_source and credential_source != fixed_source:
        raise ValueError(f"OCR 引擎 {engine_id} 的凭证来源固定为 {fixed_source}，不支持修改")
    if credential_source is None:
        return None, None, None
    if not credential_sources:
        raise ValueError(f"OCR 引擎 {engine_id} 不需要配置凭证")
    if credential_source == "database":
        return _resolve_database_credential(engine_id, credential_sources, record, data)
    if credential_source not in credential_sources or credential_source != metadata["credential_source"]:
        raise ValueError(f"OCR 引擎 {engine_id} 不支持该凭证来源")

    credential_ref = metadata["credential_ref"]
    submitted_ref = data.get("credential_ref")
    if submitted_ref is not None and str(submitted_ref).strip() != credential_ref:
        raise ValueError(f"OCR 引擎 {engine_id} 不允许使用该凭证引用")
    return credential_source, credential_ref, None


def _resolve_database_credential(
    engine_id: str,
    credential_sources: list[str],
    record: OCREngineConfig,
    data: dict[str, Any],
) -> tuple[str, None, str]:
    """复用已保存密文，或加密本次提交的新密钥。"""

    if "database" not in credential_sources:
        raise ValueError(f"OCR 引擎 {engine_id} 不支持数据库密钥")
    credential_value = record.credential_value
    submitted_value = data.get("credential_value")
    if submitted_value is not None:
        submitted_value = str(submitted_value).strip()
        credential_value = encrypt_ocr_credential(submitted_value) if submitted_value else None
    if not credential_value:
        raise ValueError("选择数据库密钥时必须填写密钥")
    return "database", None, credential_value


def _resolve_endpoint(
    engine_id: str,
    metadata: dict[str, Any],
    record: OCREngineConfig,
    data: dict[str, Any],
) -> str | None:
    """仅允许自托管引擎修改服务端点。"""

    if metadata.get("endpoint_editable", False):
        return validate_endpoint(data.get("endpoint", record.endpoint))
    if "endpoint" in data:
        raise ValueError(f"OCR 引擎 {engine_id} 的服务端点由系统管理，不支持修改")
    return metadata["endpoint"]


def get_default_ocr_engine() -> str:
    """返回当前默认 OCR 引擎。"""

    return ocr_config_cache.default_engine()


def is_ocr_engine_enabled(engine_id: str) -> bool:
    """判断引擎是否允许新任务使用。"""

    config = ocr_config_cache.get(engine_id)
    return bool(config and config.enabled)


def resolve_ocr_default_params(engine_id: str, *, require_enabled: bool = True) -> dict[str, Any]:
    """返回指定引擎的新任务默认参数。"""

    return dict(get_runtime_ocr_config(engine_id, require_enabled=require_enabled).default_params)


def resolve_processor_kwargs(engine_id: str) -> dict[str, Any]:
    """把运行时配置转换为解析器构造参数。"""

    runtime = get_runtime_ocr_config(engine_id)
    kwargs: dict[str, Any] = {}
    credential, provider_api_url = _resolve_runtime_credential(engine_id, runtime)

    if runtime.credential_source and not credential:
        raise ValueError(f"OCR 引擎 {engine_id} 的凭证引用不可用")

    # 各解析器的构造参数名不同，集中映射比把配置策略分散到解析器类更容易审阅。
    if engine_id == "rapid_ocr":
        kwargs["det_box_thresh"] = runtime.default_params.get("det_box_thresh", 0.3)
    elif engine_id in {"mineru_ocr", "pp_structure_v3_ocr"} and runtime.endpoint:
        kwargs["server_url"] = runtime.endpoint
    elif engine_id == "mineru_official":
        kwargs.update(api_key=credential, api_base=runtime.endpoint)
    elif engine_id == "deepseek_ocr":
        kwargs.update(api_key=credential, api_url=runtime.endpoint or provider_api_url)
    elif engine_id in {"paddleocr_vl_1_6", "paddleocr_pp_ocrv6"}:
        kwargs.update(api_token=credential, api_url=runtime.endpoint)
    return {key: value for key, value in kwargs.items() if value is not None}


def _resolve_runtime_credential(
    engine_id: str,
    runtime: OCRRuntimeConfig,
) -> tuple[str | None, str | None]:
    """解析当前进程可用的密钥和模型供应商端点。"""

    if runtime.credential_source == "environment" and runtime.credential_ref:
        return os.getenv(runtime.credential_ref), None
    if runtime.credential_source == "database":
        return runtime.credential_value, None
    if runtime.credential_source != "model_provider" or not runtime.credential_ref:
        return None, None

    from yuxi.models.providers.cache import model_cache

    models = model_cache.get_specs_grouped_by_provider().get(runtime.credential_ref, [])
    target = next((item for item in models if item.model_id == "deepseek-ai/DeepSeek-OCR"), None)
    target = target or (models[0] if models else None)
    if target is None:
        return None, None
    api_url = f"{target.base_url.rstrip('/')}/chat/completions" if engine_id == "deepseek_ocr" else None
    return target.api_key, api_url


def get_runtime_ocr_config(engine_id: str, *, require_enabled: bool = True) -> OCRRuntimeConfig:
    """读取运行时配置，并按需拒绝已停用引擎。"""

    config = ocr_config_cache.get(engine_id)
    if config is None:
        raise ValueError(f"不支持的 OCR 引擎: {engine_id}")
    if require_enabled and not config.enabled and engine_id != "disable":
        raise ValueError(f"OCR 引擎已停用: {engine_id}")
    return config
