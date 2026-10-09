"""公开 Session 请求与生成 OpenAPI 的可执行契约。"""

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from yuxi.api.routers.public_v1.agents import public_agents_router
from yuxi.api.routers.public_v1.agents.schemas import SessionCreate, input_messages_to_domain


def test_initial_text_shorthand_has_same_domain_message_as_array():
    """字符串简写保留与消息数组相同的持久输入内容。"""
    text = SessionCreate(agent_id="agent", input="你好")
    array = SessionCreate(agent_id="agent", input=[{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}])
    assert (
        input_messages_to_domain(text.input)[0].langchain_message.content
        == input_messages_to_domain(array.input)[0].langchain_message.content
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"input": ""},
        {"input": []},
        {"agent": {"instructions": "unsupported"}},
        {"environment": {"type": "none"}},
        {"model_spec": "old-field"},
    ],
)
def test_unsupported_or_empty_creation_fields_are_rejected(extra):
    """未支持的官方配置和空输入不能被静默接受。"""
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"agent_id": "agent", **extra})


def test_openapi_session_models_and_stream_media_type():
    """生成文档明确会话、接收回执、Items 与长期 SSE 的类型。"""
    app = FastAPI()
    app.include_router(public_agents_router, prefix="/api")
    schema = app.openapi()
    paths = schema["paths"]
    assert not any("/threads" in path for path in paths)
    create = paths["/api/v1/agents/sessions"]["post"]
    assert set(create["responses"]["201"]["content"]) == {"application/json", "text/event-stream"}
    assert create["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("SessionResponse")
    events = paths["/api/v1/agents/sessions/{session_id}/events"]
    assert set(events["get"]["responses"]["200"]["content"]) == {"text/event-stream"}
    assert events["post"]["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("EventAccepted")
    assert {"401", "403", "404", "409", "422"} <= events["post"]["responses"].keys()
    items = paths["/api/v1/agents/sessions/{session_id}/items"]["get"]
    assert {param["name"] for param in items["parameters"]} >= {"session_id", "after", "limit", "order"}
    turn_items = paths["/api/v1/agents/sessions/{session_id}/turns/{turn_id}/items"]["get"]
    assert {param["name"] for param in turn_items["parameters"]} >= {"after", "limit", "order"}
    assert turn_items["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("ItemList")
    turns = paths["/api/v1/agents/sessions/{session_id}/turns"]["get"]
    assert turns["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("TurnList")
    receipt = paths["/api/v1/agents/sessions/{session_id}/receipt"]["get"]
    assert {param["name"] for param in receipt["parameters"]} >= {"idempotency_key"}
    assert receipt["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("EventAccepted")
    assert not any(path.endswith("/history") for path in paths)


@pytest.mark.parametrize("payload", [{"model_spec": "old"}, {"agent": {"model": None}}, {"title": "old"}])
def test_session_update_rejects_old_fields_and_null_model(payload):
    """旧字段和没有定义重置语义的 null 必须显式拒绝。"""
    from yuxi.api.routers.public_v1.agents.schemas import SessionUpdate

    with pytest.raises(ValidationError):
        SessionUpdate.model_validate(payload)


def test_session_receipt_does_not_override_state_or_expose_internal_snapshot():
    """失败、未读与接收事实相互独立，旧状态和私有配置不再公开。"""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from yuxi.modules.agents.services.public_resources import session_resource

    timestamp = datetime(2026, 10, 9, tzinfo=UTC)
    session = SimpleNamespace(
        thread_id="session",
        agent_id="agent",
        title="title",
        project_id="project",
        is_pinned=False,
        status="active",
        parent_thread_id=None,
        queue_paused=False,
        created_at=timestamp,
        config_snapshot={"model": "model", "tool_approval_mode": "default", "internal_secret": "never-public"},
    )
    turn = SimpleNamespace(id="turn", status="failed", current_run_id="run", result_run_id=None)
    turn.waitpoint = None
    result = session_resource(
        session,
        turn,
        None,
        0,
        timestamp,
        True,
        receipt={"thread_id": "session", "event_id": "receipt", "status": "accepted"},
    )
    assert result["status"] == "failed"
    assert result["yuxi"]["unread"] is True
    assert result["yuxi"]["archived"] is False
    assert result["yuxi"]["receipt"]["event_id"] == "receipt"
    assert not {"thread_status", "activity_status", "metadata", "config_snapshot"} & result["yuxi"].keys()
    assert result["agent"] == {"id": "agent", "model": "model"}


@pytest.mark.parametrize("kind,expected", [("cooperation", "in_progress"), ("answer", "requires_action"), ("approval", "requires_action")])
def test_public_waiting_status_expresses_the_required_action(kind, expected):
    """普通调用方从核心状态决定等待，waitpoint 保留具体等待内容。"""
    from types import SimpleNamespace
    from yuxi.modules.agents.services.public_resources import turn_status

    turn = SimpleNamespace(status="waiting", waitpoint={"kind": kind})
    assert turn_status(turn, None) == expected


def test_turn_http_core_matches_terminal_event_and_preserves_cancelling():
    """相同终态事实生成相同核心，取消清理不能提前投影终态。"""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from yuxi.modules.agents.services.events import _turn_event
    from yuxi.modules.agents.services.public_resources import turn_core

    timestamp = datetime(2026, 10, 9, tzinfo=UTC)
    turn = SimpleNamespace(
        id="turn",
        thread_id="session",
        status="completed",
        created_at=timestamp,
        finished_at=timestamp,
        result_run_id="run",
        current_run_id="run",
    )
    run = SimpleNamespace(
        id="run",
        thread_id="session",
        agent_slug="agent",
        status="completed",
        started_at=timestamp,
        error_message=None,
        error_type=None,
        input_id="input",
        created_by_run_id=None,
    )
    event = _turn_event(None, run, turn, "completed", "completed", timestamp)
    assert event["turn"] == turn_core(turn, run, started_at=timestamp)
    assert event["turn"]["id"] == "turn" and event["turn"]["session_id"] == "session"
    turn.status = "cancelling"
    assert turn_core(turn, run, started_at=timestamp)["status"] == "in_progress"
    assert turn_core(turn, run, started_at=timestamp)["completed_at"] is None
