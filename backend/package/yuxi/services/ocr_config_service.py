"""OCR 引擎配置用例。"""

from __future__ import annotations

import os
from typing import Any

from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.parser.registry import PROCESSOR_METADATA, get_supported_extensions
from yuxi.repositories.ocr_config_repository import clear_default_ocr_config, get_ocr_config, list_ocr_configs
from yuxi.storage.postgres.models_business import OCREngineConfig


def validate_endpoint(endpoint: str | None) -> str | None:
    """校验自托管 OCR 服务的 HTTP(S) 端点。"""

    if endpoint is None or not endpoint.strip():
        return None
    return str(HttpUrl(endpoint.strip()))


async def ensure_ocr_configs_in_db(db: AsyncSession) -> list[OCREngineConfig]:
    """补齐内置 OCR 配置，并固定由系统管理的端点和凭证来源。"""

    from yuxi import config

    records = await list_ocr_configs(db)
    legacy_records = [record for record in records if record.engine_id == "disable"]
    for legacy in legacy_records:
        await db.delete(legacy)
        records.remove(legacy)
    if legacy_records:
        await db.flush()
    existing = {record.engine_id: record for record in records}
    default_engine = config.default_ocr_engine if config.default_ocr_engine in PROCESSOR_METADATA else "rapid_ocr"

    for engine_id, metadata in PROCESSOR_METADATA.items():
        if engine_id in existing:
            record = existing[engine_id]
            if not metadata.get("endpoint_editable", False):
                record.endpoint = metadata["endpoint"]
            if len(metadata.get("credential_sources", [])) == 1:
                record.credential_source = metadata["credential_source"]
            continue

        record = OCREngineConfig(
            engine_id=engine_id,
            enabled=bool(metadata["enabled"]),
            is_default=not any(item.is_default for item in records) and engine_id == default_engine,
            endpoint=metadata["endpoint"],
            credential_source=metadata["credential_source"],
            credential_value=None,
            created_by="system",
            updated_by="system",
        )
        db.add(record)
        records.append(record)

    if not any(record.is_default for record in records):
        next(record for record in records if record.engine_id == default_engine).is_default = True
    await db.flush()
    return records


async def resolve_ocr_task_params(
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """读取一次数据库配置，只解析本次任务使用的引擎和连接参数。"""

    resolved = dict(params or {})
    if resolved.get("ocr_engine") == "disable":
        resolved["_ocr_processor_kwargs"] = {}
        return resolved

    if db is None:
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as session:
            record = await _resolve_ocr_record(session, resolved.get("ocr_engine"))
    else:
        record = await _resolve_ocr_record(db, resolved.get("ocr_engine"))

    resolved["ocr_engine"] = record.engine_id
    resolved.pop("ocr_engine_config", None)
    resolved["_ocr_processor_kwargs"] = _build_processor_kwargs(record)
    return resolved


async def resolve_ocr_engine_id(engine_id: str | None = None) -> str:
    """从 PostgreSQL 返回指定或默认的可用 OCR 引擎标识。"""

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        return (await _resolve_ocr_record(session, engine_id)).engine_id


def serialize_admin_config(record: OCREngineConfig) -> dict[str, Any]:
    """序列化管理员视图，不返回数据库密钥。"""

    metadata = PROCESSOR_METADATA[record.engine_id]
    source = record.credential_source
    return {
        "engine_id": record.engine_id,
        "display_name": metadata["display_name"],
        "supported_extensions": get_supported_extensions(record.engine_id),
        "enabled": bool(record.enabled),
        "is_default": bool(record.is_default),
        "endpoint": record.endpoint,
        "endpoint_editable": metadata.get("endpoint_editable", False),
        "credential_source": source,
        "credential_ref": metadata["credential_ref"],
        "credential_sources": metadata.get("credential_sources", []),
        "credential_status": _credential_status(record, source),
        "credential_value": None,
    }


def _credential_status(record: OCREngineConfig, source: str | None) -> str:
    """返回当前凭证来源的脱敏状态。"""

    if source == "database":
        return "configured" if record.credential_value else "missing"
    if source == "environment":
        ref = PROCESSOR_METADATA[record.engine_id]["credential_ref"]
        return "configured" if ref and os.getenv(ref) else "missing"
    return "not_required" if source is None else "missing"


async def get_ocr_options(db: AsyncSession) -> dict[str, Any]:
    """返回普通用户可见的 OCR 引擎选项。"""

    records = await list_ocr_configs(db)
    return {
        "default_engine": next((record.engine_id for record in records if record.is_default), "rapid_ocr"),
        "engines": [
            {
                "engine_id": record.engine_id,
                "display_name": PROCESSOR_METADATA[record.engine_id]["display_name"],
                "enabled": bool(record.enabled),
                "supported_extensions": get_supported_extensions(record.engine_id),
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
    """更新启用状态、默认引擎、端点和凭证来源。"""

    metadata = PROCESSOR_METADATA.get(engine_id)
    if metadata is None:
        raise ValueError(f"不支持的 OCR 引擎: {engine_id}")

    records = await list_ocr_configs(db, for_update=data.get("is_default") is True)
    record = next((item for item in records if item.engine_id == engine_id), None)
    if record is None:
        return None

    enabled, is_default = _resolve_engine_state(record, data)
    source, value = _resolve_credential(record, metadata, data)
    record.enabled = enabled
    record.credential_source = source
    record.credential_value = value
    record.endpoint = _resolve_endpoint(engine_id, metadata, record, data)
    record.updated_by = updated_by

    if is_default and not record.is_default:
        await clear_default_ocr_config(db)
        record.is_default = True
    await db.flush()
    return record


def _resolve_engine_state(record: OCREngineConfig, data: dict[str, Any]) -> tuple[bool, bool]:
    """合并并校验启停与默认项状态。"""

    enabled = bool(record.enabled if data.get("enabled") is None else data["enabled"])
    is_default = bool(record.is_default if data.get("is_default") is None else data["is_default"])
    if record.is_default and data.get("is_default") is False:
        raise ValueError("请先将其他 OCR 引擎设为默认项")
    if record.is_default and not enabled:
        raise ValueError("不能停用当前默认 OCR 引擎")
    if is_default and not enabled:
        raise ValueError("默认 OCR 引擎必须处于启用状态")
    return enabled, is_default


def _resolve_credential(
    record: OCREngineConfig,
    metadata: dict[str, Any],
    data: dict[str, Any],
) -> tuple[str | None, str | None]:
    """只允许环境变量或数据库明文凭证。"""

    sources = metadata.get("credential_sources", [])
    source = data.get("credential_source", record.credential_source)
    if source is None:
        return None, None
    if source not in {"environment", "database"} or source not in sources:
        raise ValueError("凭证来源只能是 environment 或 database")

    if source == "environment":
        return source, None

    value = record.credential_value
    if "credential_value" in data:
        value = str(data["credential_value"]).strip() or None
    if not value:
        raise ValueError("选择数据库密钥时必须填写密钥")
    return source, value


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


async def _resolve_ocr_record(db: AsyncSession, engine_id: str | None) -> OCREngineConfig:
    """读取指定引擎；未指定时读取数据库默认项。"""

    if engine_id:
        record = await get_ocr_config(db, str(engine_id).strip())
    else:
        record = next((item for item in await list_ocr_configs(db) if item.is_default), None)
    if record is None:
        raise ValueError(f"不支持的 OCR 引擎: {engine_id}")
    if not record.enabled:
        raise ValueError(f"OCR 引擎已停用: {record.engine_id}")
    return record


def _build_processor_kwargs(record: OCREngineConfig) -> dict[str, Any]:
    """把数据库配置转换为解析器构造参数。"""

    credential = _resolve_runtime_credential(record)
    if record.credential_source and not credential:
        raise ValueError(f"OCR 引擎 {record.engine_id} 的凭证不可用")

    if record.engine_id in {"mineru_ocr", "pp_structure_v3_ocr"}:
        return {"server_url": record.endpoint} if record.endpoint else {}
    if record.engine_id == "mineru_official":
        return {"api_key": credential, "api_base": record.endpoint}
    if record.engine_id == "deepseek_ocr":
        return {"api_key": credential, "api_url": record.endpoint}
    if record.engine_id in {"paddleocr_vl_1_6", "paddleocr_pp_ocrv6"}:
        return {"api_token": credential, "api_url": record.endpoint}
    return {}


def _resolve_runtime_credential(record: OCREngineConfig) -> str | None:
    """从环境变量或数据库读取当前任务凭证。"""

    if record.credential_source == "database":
        return record.credential_value
    if record.credential_source == "environment":
        ref = PROCESSOR_METADATA[record.engine_id]["credential_ref"]
        return os.getenv(ref) if ref else None
    return None
