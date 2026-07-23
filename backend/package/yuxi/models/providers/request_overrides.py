from __future__ import annotations

import json
from typing import Any

OPENAI_COMPATIBLE_REQUEST_BODY_PROVIDER_TYPES = frozenset({"openai", "openrouter"})
ALLOWED_EXTRA_BODY_FIELDS = frozenset(
    {
        "enable_thinking",
        "reasoning",
        "reasoning_effort",
        "thinking",
        "thinking_budget",
    }
)


def normalize_request_body_overrides(value: Any, *, model_id: str = "") -> dict[str, Any]:
    """Validate and normalize per-model request body overrides."""
    label = f"模型 {model_id} 的 request_body_overrides" if model_id else "request_body_overrides"
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    if not value:
        return {}

    invalid_keys = [key for key in value if not isinstance(key, str) or not key.strip()]
    if invalid_keys:
        raise ValueError(f"{label} 的字段名必须是非空字符串")

    unsupported_fields = sorted(set(value) - ALLOWED_EXTRA_BODY_FIELDS)
    if unsupported_fields:
        allowed_fields = ", ".join(sorted(ALLOWED_EXTRA_BODY_FIELDS))
        raise ValueError(
            f"{label} 包含不支持的 extra_body 字段: {', '.join(unsupported_fields)}；允许字段: {allowed_fields}"
        )

    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 只能包含合法 JSON 值") from exc

    return dict(value)
