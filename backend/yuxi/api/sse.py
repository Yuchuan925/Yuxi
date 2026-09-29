"""Shared helpers and timing settings for server-sent event streams."""

from __future__ import annotations

import json


def format_sse(data: dict, event: str, event_id: str | None = None) -> str:
    lines = [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False)}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append("")
    return "\n".join(lines) + "\n"


def format_heartbeat() -> str:
    return ": heartbeat\n\n"
