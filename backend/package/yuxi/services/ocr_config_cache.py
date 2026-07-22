"""OCR 运行时配置缓存。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from yuxi.knowledge.parser.registry import PROCESSOR_METADATA
from yuxi.services.ocr_credential_crypto import decrypt_ocr_credential
from yuxi.storage.redis import sync_redis_client
from yuxi.utils.logging_config import logger

REDIS_CACHE_KEY = "yuxi:ocr_config_cache"


@dataclass(frozen=True)
class OCRRuntimeConfig:
    """单个 OCR 引擎在当前进程中的不可变运行时配置。"""

    engine_id: str
    enabled: bool
    is_default: bool
    endpoint: str | None = None
    credential_source: str | None = None
    credential_ref: str | None = None
    credential_value: str | None = None
    default_params: dict[str, Any] = field(default_factory=dict)


class OCRConfigCache:
    """维护当前进程配置，并发布不含凭证的 Redis 快照。"""

    def __init__(self) -> None:
        """创建按需加载的进程内 OCR 配置缓存。"""

        self._local_cache: dict[str, OCRRuntimeConfig] | None = None

    def get(self, engine_id: str) -> OCRRuntimeConfig | None:
        """读取指定引擎的运行时配置。"""

        return self._load().get(engine_id)

    def default_engine(self) -> str:
        """返回当前默认 OCR 引擎。"""

        for item in self._load().values():
            if item.is_default:
                return item.engine_id
        return "rapid_ocr"

    def refresh_local(self, records: list[Any]) -> None:
        """从 PostgreSQL 记录刷新本进程缓存。"""

        self._replace_local_cache(self._build_from_records(records))

    def rebuild(self, records: list[Any]) -> None:
        """刷新本进程缓存，并发布脱敏 Redis 快照。"""

        cache = self._build_from_records(records)
        public_cache = {engine_id: {**asdict(item), "credential_value": None} for engine_id, item in cache.items()}
        # 数据库凭证只在各进程从 PostgreSQL 解密，绝不进入共享 Redis 快照。
        try:
            with sync_redis_client() as redis_client:
                redis_client.set(
                    REDIS_CACHE_KEY,
                    json.dumps(public_cache, ensure_ascii=False),
                )
        except Exception as exc:
            logger.error(f"Failed to save OCR config cache to Redis: {exc}")
            raise RuntimeError("OCR 配置未能同步到 Redis") from exc
        self._replace_local_cache(cache)

    def _load(self) -> dict[str, OCRRuntimeConfig]:
        """首次访问时读取脱敏快照，启动同步随后以 PostgreSQL 为准。"""

        if self._local_cache is not None:
            return self._local_cache
        try:
            with sync_redis_client() as redis_client:
                raw = redis_client.get(REDIS_CACHE_KEY)
            if raw:
                items = json.loads(raw)
                cache = {engine_id: OCRRuntimeConfig(**data) for engine_id, data in items.items()}
            else:
                cache = self._fallback_cache()
        except Exception as exc:
            logger.warning(f"Failed to load OCR config cache from Redis: {exc}")
            cache = self._fallback_cache()
        self._replace_local_cache(cache)
        return cache

    def _fallback_cache(self) -> dict[str, OCRRuntimeConfig]:
        """在持久化缓存不可用时构造代码内置配置。"""

        from yuxi import config

        default_engine = config.default_ocr_engine
        return {
            engine_id: OCRRuntimeConfig(
                engine_id=engine_id,
                enabled=bool(metadata["enabled"]),
                is_default=engine_id == default_engine,
                endpoint=metadata["endpoint"],
                credential_source=metadata["credential_source"],
                credential_ref=metadata["credential_ref"],
                credential_value=None,
                default_params=dict(metadata["default_params"]),
            )
            for engine_id, metadata in PROCESSOR_METADATA.items()
        }

    def _build_from_records(self, records: list[Any]) -> dict[str, OCRRuntimeConfig]:
        """把数据库记录转换为包含进程内明文凭证的运行时配置。"""

        return {
            record.engine_id: OCRRuntimeConfig(
                engine_id=record.engine_id,
                enabled=bool(record.enabled),
                is_default=bool(record.is_default),
                endpoint=record.endpoint,
                credential_source=record.credential_source,
                credential_ref=record.credential_ref,
                credential_value=(
                    decrypt_ocr_credential(record.credential_value) if record.credential_source == "database" else None
                ),
                default_params=dict(record.default_params or {}),
            )
            for record in records
        }

    def _replace_local_cache(self, cache: dict[str, OCRRuntimeConfig]) -> None:
        """替换本地配置并淘汰参数或凭证已变化的处理器实例。"""

        previous = self._local_cache or {}
        changed_engines = {
            engine_id for engine_id in set(previous) | set(cache) if previous.get(engine_id) != cache.get(engine_id)
        }
        self._local_cache = cache
        if changed_engines:
            from yuxi.knowledge.parser.factory import DocumentProcessorFactory

            # 及时释放旧模型和旧密钥，避免无界缓存及凭证轮换后继续复用旧实例。
            for engine_id in changed_engines:
                DocumentProcessorFactory.clear_cache(engine_id)


ocr_config_cache = OCRConfigCache()
