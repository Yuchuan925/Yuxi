from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.parser.config_schemas import validate_default_params, validate_endpoint
from yuxi.services import ocr_config_service
from yuxi.services.ocr_config_cache import OCRRuntimeConfig
from yuxi.services.ocr_credential_crypto import decrypt_ocr_credential, encrypt_ocr_credential
from yuxi.storage.postgres.models_business import Base, OCREngineConfig


@pytest_asyncio.fixture
async def db_session(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ocr_config_service.ocr_config_cache, "rebuild", lambda records: None)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_validate_default_params_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        validate_default_params("rapid_ocr", {"unknown": True})


def test_validate_endpoint_accepts_http_and_rejects_other_schemes():
    assert validate_endpoint("https://ocr.example.com/api") == "https://ocr.example.com/api"
    with pytest.raises(ValidationError):
        validate_endpoint("file:///tmp/ocr")


def test_database_credential_is_encrypted_even_when_value_looks_like_ciphertext(monkeypatch):
    monkeypatch.setenv("OCR_CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key")
    encrypted = encrypt_ocr_credential("fernet:v1:not-actually-encrypted")

    assert encrypted != "fernet:v1:not-actually-encrypted"
    assert decrypt_ocr_credential(encrypted) == "fernet:v1:not-actually-encrypted"


def test_database_credential_requires_persistent_shared_key(monkeypatch):
    monkeypatch.delenv("OCR_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(ValueError, match="持久化"):
        encrypt_ocr_credential("database-secret")


@pytest.mark.asyncio
async def test_ensure_configs_creates_one_record_per_engine_with_single_default(db_session):
    records = await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    assert len(records) == 8
    assert sum(record.is_default for record in records) == 1
    assert next(record.engine_id for record in records if record.is_default) == "rapid_ocr"
    assert next(record for record in records if record.engine_id == "disable").enabled is True


@pytest.mark.asyncio
async def test_update_config_switches_default_and_validates_params(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    updated = await ocr_config_service.update_ocr_config(
        db_session,
        "mineru_ocr",
        {
            "enabled": True,
            "is_default": True,
            "endpoint": "http://mineru:30001",
            "default_params": {"timeout_seconds": 90},
        },
        "tester",
    )
    await db_session.commit()

    assert updated is not None
    assert updated.is_default is True
    assert updated.endpoint == "http://mineru:30001/"
    assert updated.default_params["timeout_seconds"] == 90
    records = await ocr_config_service.rebuild_ocr_config_cache(db_session)
    assert sum(record.is_default for record in records) == 1
    assert next(record.engine_id for record in records if record.is_default) == "mineru_ocr"


@pytest.mark.asyncio
async def test_update_config_ignores_explicit_null_boolean_fields(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    updated = await ocr_config_service.update_ocr_config(
        db_session,
        "rapid_ocr",
        {"enabled": None, "is_default": None},
        "tester",
    )

    assert updated is not None
    assert updated.enabled is True
    assert updated.is_default is True


@pytest.mark.asyncio
async def test_current_default_cannot_be_disabled_or_cleared(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="默认"):
        await ocr_config_service.update_ocr_config(
            db_session,
            "rapid_ocr",
            {"enabled": False},
            "tester",
        )

    with pytest.raises(ValueError, match="其他 OCR 引擎"):
        await ocr_config_service.update_ocr_config(
            db_session,
            "rapid_ocr",
            {"is_default": False},
            "tester",
        )


@pytest.mark.asyncio
async def test_credential_reference_is_restricted_per_engine(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="不允许使用该凭证引用"):
        await ocr_config_service.update_ocr_config(
            db_session,
            "mineru_official",
            {"credential_source": "environment", "credential_ref": "POSTGRES_PASSWORD"},
            "tester",
        )


@pytest.mark.asyncio
async def test_credential_reference_can_be_cleared(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    updated = await ocr_config_service.update_ocr_config(
        db_session,
        "mineru_official",
        {"credential_source": None, "credential_ref": None},
        "tester",
    )

    assert updated is not None
    assert updated.credential_source is None
    assert updated.credential_ref is None


@pytest.mark.asyncio
async def test_database_credential_can_be_saved_and_reused_without_returning_secret(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    updated = await ocr_config_service.update_ocr_config(
        db_session,
        "mineru_official",
        {"credential_source": "database", "credential_value": "database-secret"},
        "tester",
    )

    assert updated is not None
    assert updated.credential_source == "database"
    assert updated.credential_value != "database-secret"
    assert decrypt_ocr_credential(updated.credential_value) == "database-secret"
    serialized = ocr_config_service.serialize_admin_config(updated)
    assert serialized["credential_status"] == "configured"
    assert serialized["credential_statuses"]["database"] == "configured"
    assert "database-secret" not in str(serialized)

    reused = await ocr_config_service.update_ocr_config(
        db_session,
        "mineru_official",
        {"default_params": updated.default_params},
        "tester",
    )
    assert reused is not None
    assert reused.credential_value == updated.credential_value


@pytest.mark.asyncio
async def test_switching_to_database_credential_requires_a_key(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="必须填写密钥"):
        await ocr_config_service.update_ocr_config(
            db_session,
            "mineru_official",
            {"credential_source": "database"},
            "tester",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["environment", "database"])
async def test_deepseek_credential_source_is_fixed_to_model_provider(db_session, source):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="凭证来源固定为 model_provider"):
        await ocr_config_service.update_ocr_config(
            db_session,
            "deepseek_ocr",
            {"credential_source": source, "credential_value": "database-secret"},
            "tester",
        )


@pytest.mark.asyncio
async def test_ensure_configs_restores_deepseek_fixed_credential_source(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)
    record = await ocr_config_service.get_ocr_config(db_session, "deepseek_ocr", for_update=True)
    assert record is not None
    record.credential_source = "database"
    record.credential_ref = None
    record.credential_value = "database-secret"
    await db_session.flush()

    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    assert record.credential_source == "model_provider"
    assert record.credential_ref == "siliconflow-cn"
    assert record.credential_value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine_id",
    ["mineru_official", "deepseek_ocr", "paddleocr_vl_1_6", "paddleocr_pp_ocrv6"],
)
async def test_cloud_engine_rejects_endpoint_updates(db_session, engine_id):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="服务端点由系统管理"):
        await ocr_config_service.update_ocr_config(
            db_session,
            engine_id,
            {"endpoint": "https://attacker.example/api/v4"},
            "tester",
        )


@pytest.mark.asyncio
async def test_ensure_configs_restores_system_managed_endpoint(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)
    record = await ocr_config_service.get_ocr_config(db_session, "mineru_official", for_update=True)
    assert record is not None
    record.endpoint = "https://attacker.example/api/v4"
    await db_session.flush()

    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    assert record.endpoint == ocr_config_service.PROCESSOR_METADATA["mineru_official"]["endpoint"]


@pytest.mark.asyncio
async def test_route_commits_and_refreshes_local_cache_when_redis_publish_fails(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    from yuxi.repositories.ocr_config_repository import get_ocr_config
    from server.routers.system_router import OCREngineConfigPayload, put_ocr_engine_config

    await ocr_config_service.ensure_ocr_configs_in_db(db_session)
    await db_session.commit()
    original = await get_ocr_config(db_session, "rapid_ocr")
    assert original is not None
    original_params = dict(original.default_params)

    def fail_rebuild(records):
        raise RuntimeError("OCR 配置未能同步到 Redis")

    monkeypatch.setattr(ocr_config_service.ocr_config_cache, "rebuild", fail_rebuild)

    response = await put_ocr_engine_config(
        engine_id="rapid_ocr",
        payload=OCREngineConfigPayload(default_params={**original_params, "det_box_thresh": 0.55}),
        current_user=SimpleNamespace(username="tester"),
        db=db_session,
    )

    assert response["config"]["default_params"]["det_box_thresh"] == 0.55
    persisted = await get_ocr_config(db_session, "rapid_ocr")
    assert persisted is not None
    assert persisted.default_params["det_box_thresh"] == 0.55


@pytest.mark.asyncio
async def test_route_saves_database_credential_without_returning_secret(db_session, monkeypatch: pytest.MonkeyPatch):
    from server.routers.system_router import OCREngineConfigPayload, put_ocr_engine_config

    await ocr_config_service.ensure_ocr_configs_in_db(db_session)
    monkeypatch.setattr(ocr_config_service.ocr_config_cache, "rebuild", lambda records: None)

    response = await put_ocr_engine_config(
        engine_id="mineru_official",
        payload=OCREngineConfigPayload(
            credential_source="database",
            credential_value="database-secret",
        ),
        current_user=SimpleNamespace(username="tester"),
        db=db_session,
    )

    assert response["config"]["credential_status"] == "configured"
    assert "credential_value" not in response["config"]
    assert "database-secret" not in str(response)


def test_admin_serialization_never_contains_resolved_credential(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MINERU_API_KEY", "secret-value")
    record = OCREngineConfig(
        engine_id="mineru_official",
        enabled=True,
        is_default=False,
        credential_source="environment",
        credential_ref="MINERU_API_KEY",
        default_params={},
    )

    payload = ocr_config_service.serialize_admin_config(record)

    assert payload["credential_status"] == "configured"
    assert payload["credential_statuses"]["environment"] == "configured"
    assert payload["credential_statuses"]["database"] == "missing"
    assert "secret-value" not in str(payload)


def test_runtime_credential_reference_fails_explicitly_when_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setattr(
        ocr_config_service.ocr_config_cache,
        "get",
        lambda engine_id: OCRRuntimeConfig(
            engine_id=engine_id,
            enabled=True,
            is_default=False,
            credential_source="environment",
            credential_ref="MINERU_API_KEY",
        ),
    )

    with pytest.raises(ValueError, match="凭证引用不可用"):
        ocr_config_service.resolve_processor_kwargs("mineru_official")


def test_runtime_database_credential_is_passed_to_processor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        ocr_config_service.ocr_config_cache,
        "get",
        lambda engine_id: OCRRuntimeConfig(
            engine_id=engine_id,
            enabled=True,
            is_default=False,
            endpoint="https://mineru.net/api/v4",
            credential_source="database",
            credential_value="database-secret",
        ),
    )

    kwargs = ocr_config_service.resolve_processor_kwargs("mineru_official")

    assert kwargs["api_key"] == "database-secret"
