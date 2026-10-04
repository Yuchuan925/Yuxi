"""实时和历史共用的公开 item 白名单投影。"""

from __future__ import annotations

from copy import deepcopy

from yuxi.modules.agents.models.messages import Message
from yuxi.modules.agents.models.runs import AgentRun
from yuxi.shared.datetime import format_utc_datetime


def serialize_public_items(message: Message, run: AgentRun | None, result_run_id: str | None) -> list[dict]:
    """只投影公开身份已登记的输出和原始用户输入。"""
    if message.role == "user":
        raw = (message.extra_metadata or {}).get("raw_message") or {}
        content = raw.get("content")
        if not isinstance(content, list):
            content = [{"type": "input_text", "text": message.content or ""}]
        content = [
            {"type": "input_text", "text": part["text"]}
            if part.get("type") in {"text", "input_text"}
            else {
                "type": "input_image",
                "image_url": (
                    part["image_url"]["url"] if isinstance(part.get("image_url"), dict) else part["image_url"]
                ),
            }
            for part in content
            if part.get("type") in {"text", "input_text", "image_url", "input_image"}
        ]
        return [
            {
                "type": "message",
                "id": f"input_{message.id}",
                "turn_id": message.turn_id,
                "role": "user",
                "content": content,
                "status": "completed",
                "phase": None,
                "yuxi": {
                    "run_id": message.run_id,
                    "input_id": (message.extra_metadata or {}).get("input_id"),
                    "message_id": message.id,
                    "delivery_status": message.delivery_status,
                    "created_at": format_utc_datetime(message.created_at),
                    "message_type": message.message_type,
                    "attachments": (message.extra_metadata or {}).get("attachments", []),
                },
            }
        ]
    items = deepcopy(list((message.extra_metadata or {}).get("public_items", {}).values()))
    for item in items:
        if item["type"] == "message":
            item["phase"] = (
                "final_answer"
                if run is not None and run.id == result_run_id and run.output_message_id == message.id
                else "commentary"
            )
        if run is not None and run.status in {"failed", "cancelled", "interrupted", "yielded"}:
            if item["status"] == "in_progress":
                item["status"] = "failed" if run.status == "failed" and item["type"] != "message" else "incomplete"
    return sorted(items, key=lambda item: item["yuxi"]["output_index"])


def serialize_public_run(run: AgentRun) -> dict:
    """公开快照只包含执行身份、可见状态和时间，不泄漏配置与 manifest。"""
    values = run.to_dict()
    return {
        key: values[key]
        for key in (
            "id",
            "execution_seq",
            "turn_id",
            "input_id",
            "conversation_thread_id",
            "agent_slug",
            "run_type",
            "status",
            "created_by_run_id",
            "resume_from_run_id",
            "token_usage",
            "langfuse_trace_id",
            "error_type",
            "error_message",
            "started_at",
            "prepared_at",
            "first_model_request_at",
            "first_output_at",
            "finished_at",
            "created_at",
            "updated_at",
            "timing",
        )
    }
