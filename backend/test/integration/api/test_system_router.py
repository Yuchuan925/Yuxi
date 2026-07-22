"""
Integration tests for system router endpoints.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_health_endpoint_is_public(test_client):
    response = await test_client.get("/api/system/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_discovery_declares_cli_knowledge_capabilities(test_client):
    response = await test_client.get("/api/system/discovery")
    assert response.status_code == 200
    cli_capabilities = response.json()["capabilities"]["cli"]
    for capability in ("kb_list", "kb_files", "kb_query", "kb_open", "kb_find"):
        assert cli_capabilities.get(capability) is True, capability
    assert "kb_parse" not in cli_capabilities
    assert "kb_index" not in cli_capabilities


async def test_info_endpoint_is_public(test_client):
    response = await test_client.get("/api/system/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "data" in payload


async def test_config_get_requires_login_and_update_requires_admin(test_client, standard_user):
    assert (await test_client.get("/api/system/config")).status_code == 401
    user_config_response = await test_client.get("/api/system/config", headers=standard_user["headers"])
    assert user_config_response.status_code == 200, user_config_response.text

    update_response = await test_client.post(
        "/api/system/config/update",
        json={"default_ocr_engine": "rapid_ocr"},
        headers=standard_user["headers"],
    )
    assert update_response.status_code == 403


async def test_ocr_options_hide_infrastructure_and_admin_view_exposes_capabilities(
    test_client,
    standard_user,
    admin_headers,
):
    options_response = await test_client.get("/api/system/ocr/options", headers=standard_user["headers"])
    assert options_response.status_code == 200, options_response.text
    options = options_response.json()
    assert options["default_engine"]
    assert options["engines"]
    assert {"endpoint", "credential_source", "credential_ref", "default_params"}.isdisjoint(options["engines"][0])

    denied_response = await test_client.get("/api/system/ocr/configs", headers=standard_user["headers"])
    assert denied_response.status_code == 403

    configs_response = await test_client.get("/api/system/ocr/configs", headers=admin_headers)
    assert configs_response.status_code == 200, configs_response.text
    configs = configs_response.json()["configs"]
    mineru = next(item for item in configs if item["engine_id"] == "mineru_ocr")
    mineru_official = next(item for item in configs if item["engine_id"] == "mineru_official")
    deepseek = next(item for item in configs if item["engine_id"] == "deepseek_ocr")
    assert mineru["endpoint_editable"] is True
    assert mineru_official["endpoint_editable"] is False
    assert mineru_official["credential_sources"] == ["environment", "database"]
    assert mineru_official["credential_refs"] == {"environment": "MINERU_API_KEY"}
    assert deepseek["credential_sources"] == ["model_provider"]
    assert deepseek["credential_source_fixed"] is True


async def test_ocr_config_rejects_managed_fields_and_invalid_sources(test_client, admin_headers):
    managed_endpoint_response = await test_client.put(
        "/api/system/ocr/configs/mineru_official",
        json={"endpoint": "https://attacker.example/api/v4"},
        headers=admin_headers,
    )
    assert managed_endpoint_response.status_code == 400
    assert "服务端点由系统管理" in managed_endpoint_response.json()["detail"]

    missing_database_key_response = await test_client.put(
        "/api/system/ocr/configs/mineru_official",
        json={"credential_source": "database"},
        headers=admin_headers,
    )
    assert missing_database_key_response.status_code == 400
    assert "必须填写密钥" in missing_database_key_response.json()["detail"]

    fixed_deepseek_credential_response = await test_client.put(
        "/api/system/ocr/configs/deepseek_ocr",
        json={"credential_source": "database", "credential_value": "test-secret"},
        headers=admin_headers,
    )
    assert fixed_deepseek_credential_response.status_code == 400
    assert "凭证来源固定为 model_provider" in fixed_deepseek_credential_response.json()["detail"]

    unknown_health = await test_client.post(
        "/api/system/ocr/configs/not-an-engine/health",
        headers=admin_headers,
    )
    assert unknown_health.status_code == 400


async def test_ocr_config_update_reaches_independent_runtime_cache(test_client, admin_headers):
    configs_response = await test_client.get("/api/system/ocr/configs", headers=admin_headers)
    assert configs_response.status_code == 200, configs_response.text
    rapid = next(item for item in configs_response.json()["configs"] if item["engine_id"] == "rapid_ocr")
    original_params = rapid["default_params"]

    try:
        update_response = await test_client.put(
            "/api/system/ocr/configs/rapid_ocr",
            json={"default_params": {**original_params, "det_box_thresh": 0.45}},
            headers=admin_headers,
        )
        assert update_response.status_code == 200, update_response.text
        assert update_response.json()["config"]["default_params"]["det_box_thresh"] == 0.45

        from yuxi.services.ocr_config_cache import OCRConfigCache

        cross_process_config = OCRConfigCache().get("rapid_ocr")
        assert cross_process_config is not None
        assert cross_process_config.default_params["det_box_thresh"] == 0.45

        invalid_response = await test_client.put(
            "/api/system/ocr/configs/rapid_ocr",
            json={"default_params": {"unknown": True}},
            headers=admin_headers,
        )
        assert invalid_response.status_code == 400
    finally:
        restore_response = await test_client.put(
            "/api/system/ocr/configs/rapid_ocr",
            json={"default_params": original_params},
            headers=admin_headers,
        )
        assert restore_response.status_code == 200, restore_response.text


async def test_admin_can_fetch_config_and_reload_info(test_client, admin_headers):
    config_response = await test_client.get("/api/system/config", headers=admin_headers)
    assert config_response.status_code == 200, config_response.text
    assert isinstance(config_response.json(), dict)

    reload_response = await test_client.post("/api/system/info/reload", headers=admin_headers)
    assert reload_response.status_code == 200, reload_response.text
    reload_payload = reload_response.json()
    assert reload_payload["success"] is True
    assert "data" in reload_payload


async def test_sandbox_config_is_environment_only(test_client, admin_headers):
    config_response = await test_client.get("/api/system/config", headers=admin_headers)
    assert config_response.status_code == 200, config_response.text
    sandbox_fields = {
        "sandbox_provider",
        "sandbox_provisioner_url",
        "sandbox_provisioner_token",
        "sandbox_virtual_path_prefix",
        "sandbox_exec_timeout_seconds",
        "sandbox_max_output_bytes",
        "sandbox_keepalive_interval_seconds",
    }
    assert sandbox_fields.isdisjoint(config_response.json())
    assert sandbox_fields.isdisjoint(config_response.json()["_config_items"])

    update_response = await test_client.post(
        "/api/system/config",
        json={"key": "sandbox_provisioner_url", "value": "http://other:8002"},
        headers=admin_headers,
    )
    assert update_response.status_code == 400
    assert update_response.json()["detail"] == "未知配置项: sandbox_provisioner_url"


async def test_admin_can_fetch_tools_with_config_guide_field(test_client, admin_headers):
    response = await test_client.get("/api/system/tools", headers=admin_headers)
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["success"] is True
    assert isinstance(payload["data"], list)
    assert payload["data"]
    assert "config_guide" in payload["data"][0]
