from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import ocr_config_service
from yuxi.storage.postgres.models_business import Base


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_configs_creates_seven_engines(db_session):
    records = await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    assert len(records) == 7
    assert sum(record.is_default for record in records) == 1
    assert next(record.engine_id for record in records if record.is_default) == "rapid_ocr"


@pytest.mark.asyncio
async def test_update_config_changes_default_and_endpoint(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    updated = await ocr_config_service.update_ocr_config(
        db_session,
        "mineru_ocr",
        {"enabled": True, "is_default": True, "endpoint": "http://mineru:30001"},
        "tester",
    )

    assert updated.is_default is True
    assert updated.endpoint == "http://mineru:30001/"


@pytest.mark.asyncio
async def test_task_resolution_reads_database_and_drops_parameter_override(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    resolved = await ocr_config_service.resolve_ocr_task_params(
        {"ocr_engine": "rapid_ocr", "ocr_engine_config": {"det_box_thresh": 0.6}}, db_session
    )

    assert resolved["ocr_engine"] == "rapid_ocr"
    assert "ocr_engine_config" not in resolved
    assert resolved["_ocr_processor_kwargs"] == {}


@pytest.mark.asyncio
async def test_database_credential_is_saved_as_plaintext_and_not_serialized(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    updated = await ocr_config_service.update_ocr_config(
        db_session,
        "mineru_official",
        {"credential_source": "database", "credential_value": "database-secret"},
        "tester",
    )

    assert updated.credential_value == "database-secret"
    assert ocr_config_service.serialize_admin_config(updated)["credential_status"] == "configured"
    assert "database-secret" not in str(ocr_config_service.serialize_admin_config(updated))


@pytest.mark.asyncio
async def test_environment_and_database_are_only_supported_credential_sources(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="environment 或 database"):
        await ocr_config_service.update_ocr_config(
            db_session,
            "deepseek_ocr",
            {"credential_source": "model_provider"},
            "tester",
        )


@pytest.mark.asyncio
async def test_current_default_cannot_be_disabled_or_cleared(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)

    with pytest.raises(ValueError, match="默认"):
        await ocr_config_service.update_ocr_config(db_session, "rapid_ocr", {"enabled": False}, "tester")
    with pytest.raises(ValueError, match="其他 OCR 引擎"):
        await ocr_config_service.update_ocr_config(db_session, "rapid_ocr", {"is_default": False}, "tester")


@pytest.mark.asyncio
async def test_disable_is_removed_from_legacy_database_records(db_session):
    await ocr_config_service.ensure_ocr_configs_in_db(db_session)
    records = await ocr_config_service.list_ocr_configs(db_session)
    legacy = next(record for record in records if record.engine_id == "rapid_ocr")
    legacy.engine_id = "disable"
    await db_session.flush()

    records = await ocr_config_service.ensure_ocr_configs_in_db(db_session)
    assert all(record.engine_id != "disable" for record in records)
