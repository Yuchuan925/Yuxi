from __future__ import annotations

import importlib
import json
import subprocess
import sys
from contextlib import contextmanager

import pytest
from yuxi.config.app import Config
from yuxi.config.cache import RUNTIME_CONFIG_REDIS_KEY

pytestmark = pytest.mark.unit
config_cache = importlib.import_module("yuxi.config.cache")


class _FakeRedis:
    def __init__(
        self, raw: str | None = None, *, get_error: Exception | None = None, set_error: Exception | None = None
    ):
        self.raw = raw
        self.get_error = get_error
        self.set_error = set_error
        self.data: dict[str, str] = {}
        self.get_keys: list[str] = []

    def get(self, key: str) -> str | None:
        self.get_keys.append(key)
        if self.get_error:
            raise self.get_error
        return self.raw

    def set(self, key: str, value: str) -> bool:
        if self.set_error:
            raise self.set_error
        self.data[key] = value
        return True


def _patch_runtime_redis(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    @contextmanager
    def fake_sync_redis_client(*args, **kwargs):
        del args, kwargs
        yield redis

    monkeypatch.setattr(config_cache, "sync_redis_client", fake_sync_redis_client)
    monkeypatch.setattr(config_cache, "_load_postgres_snapshot", lambda _config: None)


def _import_yuxi_in_fresh_process(tmp_path, config_text: str) -> tuple[dict, subprocess.CompletedProcess[str]]:
    config_dir = tmp_path / "saves" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "base.toml").write_text(config_text, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; import yuxi; "
                "print(json.dumps({"
                "'default_ocr_engine': yuxi.config.default_ocr_engine, "
                "'default_model': yuxi.config.default_model, "
                "'factory_loaded': 'yuxi.knowledge.parser.factory' in sys.modules, "
                "'parser_loaded': 'yuxi.knowledge.parser.unified' in sys.modules, "
                "'manager_loaded': 'yuxi.knowledge.manager' in sys.modules"
                "}))"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1]), result


def test_fresh_import_loads_default_ocr_engine_without_initializing_knowledge_runtime(tmp_path):
    loaded, result = _import_yuxi_in_fresh_process(
        tmp_path,
        'default_ocr_engine = "rapid_ocr"\ndefault_model = "test-provider:after-ocr"\n',
    )

    assert loaded == {
        "default_ocr_engine": "rapid_ocr",
        "default_model": "test-provider:after-ocr",
        "factory_loaded": False,
        "parser_loaded": False,
        "manager_loaded": False,
    }
    assert "Failed to load config" not in result.stdout + result.stderr


def test_fresh_import_keeps_disable_as_default_ocr_engine(tmp_path):
    loaded, result = _import_yuxi_in_fresh_process(
        tmp_path,
        'default_ocr_engine = "disable"\n',
    )

    assert loaded["default_ocr_engine"] == "disable"
    assert "Invalid config key ignored" not in result.stdout + result.stderr


def test_fresh_import_ignores_invalid_ocr_engine_and_loads_later_config(tmp_path):
    loaded, _ = _import_yuxi_in_fresh_process(
        tmp_path,
        'default_ocr_engine = "unknown-ocr"\ndefault_model = "test-provider:after-invalid-ocr"\n',
    )

    assert loaded["default_ocr_engine"] == "rapid_ocr"
    assert loaded["default_model"] == "test-provider:after-invalid-ocr"


def test_save_runtime_snapshot_contains_only_managed_fields(tmp_path, monkeypatch: pytest.MonkeyPatch):
    redis = _FakeRedis()
    _patch_runtime_redis(monkeypatch, redis)
    cfg = Config(save_dir=str(tmp_path))

    cfg.default_model = "test-provider:new-chat"
    cfg.enable_content_guard = True
    config_cache.save_runtime_config(cfg)

    payload = json.loads(redis.data[RUNTIME_CONFIG_REDIS_KEY])
    assert payload["values"]["default_model"] == "test-provider:new-chat"
    assert payload["values"]["enable_content_guard"] is True
    assert payload["values"]["default_ocr_engine"] == "rapid_ocr"
    assert "save_dir" not in payload["values"]
    assert payload["version"] == ""
    assert payload["updated_at"] == ""


def test_base_toml_is_compatibility_read_only(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "base.toml"
    config_file.write_text(
        'default_model = "test-provider:file-chat"\nenable_reranker = true\ndefault_agent_id = "ChatbotAgent"\n',
        encoding="utf-8",
    )

    cfg = Config(save_dir=str(tmp_path))

    assert cfg.default_model == "test-provider:file-chat"
    assert not hasattr(cfg, "enable_reranker")
    assert not hasattr(cfg, "default_agent_id")

    assert config_file.read_text(encoding="utf-8").endswith('default_agent_id = "ChatbotAgent"\n')


def test_save_dir_from_base_toml_is_ignored(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "base.toml").write_text(
        f'save_dir = "{tmp_path / "from-file"}"\ndefault_model = "test-provider:file-chat"\n',
        encoding="utf-8",
    )

    cfg = Config(save_dir=str(tmp_path))

    assert cfg.save_dir == str(tmp_path)
    assert cfg.default_model == "test-provider:file-chat"

def test_refresh_loads_public_config_from_redis(tmp_path, monkeypatch: pytest.MonkeyPatch):
    redis_save_dir = str(tmp_path / "redis-save")
    payload = {
        "default_model": "test-provider:worker-chat",
        "save_dir": redis_save_dir,
        "default_ocr_engine": "mineru_ocr",
        "enable_reranker": True,
    }
    redis = _FakeRedis(raw=json.dumps(payload))
    _patch_runtime_redis(monkeypatch, redis)
    cfg = Config(save_dir=str(tmp_path))
    cfg.default_model = "test-provider:old-chat"

    cfg.refresh()

    assert cfg.default_model == "test-provider:worker-chat"
    assert cfg.default_ocr_engine == "mineru_ocr"
    assert cfg.save_dir == str(tmp_path)
    assert str(cfg._config_file) == str(tmp_path / "config" / "base.toml")
    # 快照里的非运行时字段和未知键不会被写回
    assert not hasattr(cfg, "enable_reranker")
    assert redis.get_keys == [RUNTIME_CONFIG_REDIS_KEY]


def test_refresh_keeps_memory_value_when_redis_unavailable(tmp_path, monkeypatch: pytest.MonkeyPatch):
    redis = _FakeRedis(get_error=RuntimeError("redis unavailable"))
    _patch_runtime_redis(monkeypatch, redis)
    cfg = Config(save_dir=str(tmp_path))
    cfg.default_model = "test-provider:local-chat"

    cfg.refresh()

    assert cfg.default_model == "test-provider:local-chat"
    assert redis.get_keys == [RUNTIME_CONFIG_REDIS_KEY]


def test_refresh_recovers_redis_miss_from_newer_postgres_snapshot(tmp_path, monkeypatch: pytest.MonkeyPatch):
    redis = _FakeRedis(raw=None)
    _patch_runtime_redis(monkeypatch, redis)
    cfg = Config(save_dir=str(tmp_path))
    cfg.default_model = "test-provider:old-chat"
    monkeypatch.setattr(
        config_cache,
        "_load_postgres_snapshot",
        lambda _config: {
            "version": "2026-08-13T01:00:00",
            "updated_at": "2026-08-13T01:00:00",
            "values": {"default_model": "test-provider:postgres-chat"},
        },
    )

    cfg.refresh()

    assert cfg.default_model == "test-provider:postgres-chat"
    published = json.loads(redis.data[RUNTIME_CONFIG_REDIS_KEY])
    assert published["version"] == "2026-08-13T01:00:00"
    assert published["values"]["default_model"] == "test-provider:postgres-chat"


def test_refresh_replaces_stale_redis_snapshot_from_postgres(tmp_path, monkeypatch: pytest.MonkeyPatch):
    redis = _FakeRedis(
        raw=json.dumps(
            {
                "version": "2026-08-13T00:00:00",
                "updated_at": "2026-08-13T00:00:00",
                "values": {"default_model": "test-provider:stale-chat"},
            }
        )
    )
    _patch_runtime_redis(monkeypatch, redis)
    cfg = Config(save_dir=str(tmp_path))
    monkeypatch.setattr(
        config_cache,
        "_load_postgres_snapshot",
        lambda _config: {
            "version": "2026-08-13T02:00:00",
            "updated_at": "2026-08-13T02:00:00",
            "values": {"default_model": "test-provider:fresh-chat"},
        },
    )

    cfg.refresh()

    assert cfg.default_model == "test-provider:fresh-chat"


def test_runtime_snapshot_failure_does_not_write_base_toml(tmp_path, monkeypatch: pytest.MonkeyPatch):
    redis = _FakeRedis(set_error=RuntimeError("redis unavailable"))
    _patch_runtime_redis(monkeypatch, redis)
    cfg = Config(save_dir=str(tmp_path))
    cfg.default_model = "test-provider:file-chat"

    config_cache.save_runtime_config(cfg)

    assert not (tmp_path / "config" / "base.toml").exists()


def test_start_runtime_sync_is_idempotent(tmp_path):
    cfg = Config(save_dir=str(tmp_path))

    cfg.start_runtime_sync(interval=3600)
    thread = cfg._runtime_sync_thread
    cfg.start_runtime_sync(interval=3600)

    assert cfg._runtime_sync_thread is thread
    assert thread.daemon is True


def test_update_ignores_readonly_save_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _patch_runtime_redis(monkeypatch, _FakeRedis())
    cfg = Config(save_dir=str(tmp_path))

    cfg.update(
        {
            "save_dir": str(tmp_path / "other-save"),
            "default_model": "test-provider:updated-chat",
            "default_ocr_engine": "mineru_ocr",
        }
    )

    assert cfg.save_dir == str(tmp_path)
    assert cfg.default_model == "test-provider:updated-chat"
    assert cfg.default_ocr_engine == "mineru_ocr"


def test_update_rejects_unknown_default_ocr_engine(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _patch_runtime_redis(monkeypatch, _FakeRedis())
    cfg = Config(save_dir=str(tmp_path))

    with pytest.raises(ValueError, match="不支持的默认 OCR 引擎"):
        cfg.update({"default_ocr_engine": "not_an_ocr_engine"})


def test_normalize_updates_does_not_modify_memory(tmp_path):
    cfg = Config(save_dir=str(tmp_path))

    normalized = cfg.normalize_updates({"default_model": "test-provider:persisted-chat"})

    assert normalized == {"default_model": "test-provider:persisted-chat"}
    assert cfg.default_model != "test-provider:persisted-chat"


def test_dump_config_hides_save_dir(tmp_path):
    cfg = Config(save_dir=str(tmp_path))

    dumped = cfg.dump_config()

    assert "save_dir" not in dumped
    assert "save_dir" not in dumped["_config_items"]


def test_resolve_chat_model_spec_reads_runtime_refreshed_default(tmp_path, monkeypatch):
    from yuxi.agents import models

    payload = {"default_model": "test-provider:resolved-chat"}
    _patch_runtime_redis(monkeypatch, _FakeRedis(raw=json.dumps(payload)))
    cfg = Config(save_dir=str(tmp_path))
    cfg.default_model = "test-provider:old-chat"
    cfg.refresh()
    monkeypatch.setattr(models, "sys_config", cfg)

    assert models.resolve_chat_model_spec(None) == "test-provider:resolved-chat"
