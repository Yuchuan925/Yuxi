from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path


def _load_context_module():
    context_path = Path(__file__).resolve().parents[3] / "package/yuxi/agents/context.py"
    previous_yuxi = sys.modules.get("yuxi")
    sys.modules["yuxi"] = types.SimpleNamespace(config=types.SimpleNamespace(default_model="test:model"))
    try:
        spec = importlib.util.spec_from_file_location("test_yuxi_agents_context", context_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_yuxi is None:
            sys.modules.pop("yuxi", None)
        else:
            sys.modules["yuxi"] = previous_yuxi


context_module = _load_context_module()
BaseContext = context_module.BaseContext
filter_config_by_role = context_module.filter_config_by_role


@dataclass
class SuperAdminOnlyContext(BaseContext):
    secret_setting: str = field(default="hidden", metadata={"name": "Secret", "auth": "superadmin"})


def test_get_configurable_items_filters_admin_fields_for_user():
    items = BaseContext.get_configurable_items(user_role="user")

    assert "system_prompt" in items
    assert "summary_threshold" not in items


def test_get_configurable_items_allows_admin_and_superadmin_fields():
    admin_items = BaseContext.get_configurable_items(user_role="admin")
    superadmin_items = SuperAdminOnlyContext.get_configurable_items(user_role="superadmin")

    assert "summary_threshold" in admin_items
    assert "secret_setting" in superadmin_items


def test_filter_config_by_role_removes_unauthorized_context_values():
    config_json = {
        "context": {
            "system_prompt": "visible",
            "summary_threshold": 10,
            "secret_setting": "nope",
        },
        "other": {"keep": True},
    }

    filtered = filter_config_by_role(config_json, "user", context_schema=SuperAdminOnlyContext)

    assert filtered == {"context": {"system_prompt": "visible"}, "other": {"keep": True}}
    assert config_json["context"]["summary_threshold"] == 10


def test_filter_config_by_role_keeps_admin_context_values_for_admin():
    filtered = filter_config_by_role(
        {"context": {"summary_threshold": 10, "secret_setting": "nope"}},
        "admin",
        context_schema=SuperAdminOnlyContext,
    )

    assert filtered == {"context": {"summary_threshold": 10}}
