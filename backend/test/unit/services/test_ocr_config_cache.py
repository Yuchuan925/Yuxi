from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import yuxi.services.ocr_config_cache as cache_module
from yuxi.services.ocr_config_cache import OCRConfigCache
from yuxi.services.ocr_credential_crypto import encrypt_ocr_credential


def test_redis_snapshot_excludes_database_credential(monkeypatch):
    storage: dict[str, str] = {}

    class FakeRedis:
        def get(self, key: str):
            return storage.get(key)

        def set(self, key: str, value: str):
            storage[key] = value

    @contextmanager
    def fake_redis_client():
        yield FakeRedis()

    monkeypatch.setattr(cache_module, "sync_redis_client", fake_redis_client)
    writer = OCRConfigCache()
    writer.rebuild(
        [
            SimpleNamespace(
                engine_id="mineru_official",
                enabled=True,
                is_default=True,
                endpoint="https://mineru.net/api/v4",
                credential_source="database",
                credential_ref=None,
                credential_value=encrypt_ocr_credential("credential-value"),
                default_params={"language": "ch"},
            )
        ]
    )

    serialized = storage[cache_module.REDIS_CACHE_KEY]
    assert json.loads(serialized)["mineru_official"]["credential_value"] is None
    assert "credential-value" not in serialized

    local = writer.get("mineru_official")
    assert local is not None
    assert local.credential_value == "credential-value"

    reader = OCRConfigCache()
    loaded = reader.get("mineru_official")
    assert loaded is not None
    assert loaded.endpoint == "https://mineru.net/api/v4"
    assert loaded.credential_value is None
    assert loaded.default_params == {"language": "ch"}
