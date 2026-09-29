from __future__ import annotations

import httpx
import pytest
from yuxi_cli.client import ClientError, YuxiClient, _iter_sse_events
from yuxi_cli.config import Remote


def _patched_client(monkeypatch):
    client = YuxiClient(Remote(name="local", url="http://localhost:5173", api_key="yxkey_test"))
    calls: list[dict] = []

    def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return {"ok": True, "method": method, "path": path}

    monkeypatch.setattr(client, "_request", fake_request)
    return client, calls


def test_create_thread_and_follow_up_use_public_api(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.create_agent_thread(agent_slug="default-chatbot", idempotency_key="create-key")
        client.send_agent_message(
            "thread-1", "你好", idempotency_key="message-key"
        )
        client.get_agent_input("thread-1", "input-1")
        client.get_agent_turn("thread-1", "turn-1")
    finally:
        client.close()

    assert [call["path"] for call in calls] == [
        "/v1/agents/threads",
        "/v1/agents/threads/thread-1/events",
        "/v1/agents/threads/thread-1/inputs/input-1",
        "/v1/agents/threads/thread-1/turns/turn-1",
    ]
    assert calls[0]["json"] == {"agent_id": "default-chatbot"}
    assert calls[0]["headers"] == {"Idempotency-Key": "create-key"}
    assert calls[1]["headers"] == {"Idempotency-Key": "message-key"}
    assert calls[1]["json"] == {"events": [{
        "type": "agent.thread.input.message",
        "mode": "follow_up",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
    }]}


def test_structured_resume_and_cancel_use_public_thread_events(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    resume = {
        "type": "yuxi.thread.input.resume",
        "turn_id": "turn-1",
        "waitpoint_id": "waitpoint-1",
        "response": {"type": "answer", "answers": [{"question_id": "q1", "answer": "可以"}]},
    }
    cancel = {"type": "yuxi.thread.input.cancel", "turn_id": "turn-1"}
    try:
        client.submit_agent_event("thread-1", resume, idempotency_key="resume-key")
        client.submit_agent_event("thread-1", cancel, idempotency_key="cancel-key")
    finally:
        client.close()

    assert [call["path"] for call in calls] == [
        "/v1/agents/threads/thread-1/events",
        "/v1/agents/threads/thread-1/events",
    ]
    assert calls[0]["json"] == {"events": [resume]}
    assert calls[0]["headers"] == {"Idempotency-Key": "resume-key"}
    assert calls[1]["json"] == {"events": [cancel]}
    assert calls[1]["headers"] == {"Idempotency-Key": "cancel-key"}


def test_iter_sse_events_supports_multiline_data_and_ignores_heartbeat():
    lines = iter(
        [
            ": heartbeat",
            "",
            "id: 1-0",
            "event: messages",
            'data: {"payload":',
            'data: {"status":"loading"}}',
            "",
        ]
    )

    assert list(_iter_sse_events(lines)) == [
        {
            "id": "1-0",
            "event": "messages",
            "data": '{"payload":\n{"status":"loading"}}',
        }
    ]


def test_stream_agent_thread_events_sends_auth_and_cursor():
    remote = Remote(name="local", url="http://localhost:5173", api_key="yxkey_test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/agents/threads/thread-1/events"
        assert request.headers["Authorization"] == "Bearer yxkey_test"
        assert request.headers["Last-Event-ID"] == "cursor-4"
        return httpx.Response(
            200,
            text='event: agent.thread.turn.completed\nid: cursor-5\ndata: {"turn_id":"turn-1"}\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    client = YuxiClient(remote)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        events = list(client.stream_agent_thread_events("thread-1", after_cursor="cursor-4"))
    finally:
        client.close()

    assert events == [{
        "event": "agent.thread.turn.completed", "id": "cursor-5", "data": '{"turn_id":"turn-1"}'
    }]


def test_list_external_databases_uses_external_path(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.list_external_databases()
    finally:
        client.close()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["path"] == "/v1/knowledge/databases/external"


def test_list_public_agents_uses_public_path(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.list_public_agents(api_key="yxkey_agents")
    finally:
        client.close()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["path"] == "/v1/agents"
    assert calls[-1]["api_key"] == "yxkey_agents"


def test_list_agents_uses_visible_agent_path(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.list_agents()
    finally:
        client.close()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["path"] == "/agent"


def test_get_agent_uses_slug_path(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.get_agent("research-agent")
    finally:
        client.close()
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["path"] == "/agent/research-agent"


def test_get_agent_keeps_slug_in_one_path_segment():
    remote = Remote(name="local", url="http://localhost:5173", api_key="yxkey_test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/api/agent/..%2Fsystem%2Finfo%3Ffull%3Dtrue"
        return httpx.Response(404, json={"detail": "智能体不存在"})

    client = YuxiClient(remote)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ClientError, match="智能体不存在"):
            client.get_agent("../system/info?full=true")
    finally:
        client.close()


def test_get_agent_preserves_server_not_found_response():
    remote = Remote(name="local", url="http://localhost:5173", api_key="yxkey_test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent/hidden-agent"
        assert request.headers["Authorization"] == "Bearer yxkey_test"
        return httpx.Response(404, json={"detail": "智能体不存在"})

    client = YuxiClient(remote)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ClientError, match="智能体不存在") as exc_info:
            client.get_agent("hidden-agent")
    finally:
        client.close()

    assert exc_info.value.status_code == 404


def test_list_external_files_passes_query_params(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.list_external_files("kb_1", query="report", offset=10, limit=50, status="indexed")
    finally:
        client.close()
    call = calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/v1/knowledge/databases/external/kb_1/files"
    params = call["params"]
    assert params["query"] == "report"
    assert params["offset"] == 10
    assert params["limit"] == 50
    assert params["status"] == "indexed"


def test_retrieve_external_posts_json_body(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.retrieve_external("kb_1", query="hello", file_name="a.md", options={"final_top_k": 5})
    finally:
        client.close()
    call = calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/v1/knowledge/databases/external/kb_1/retrieve"
    assert call["json"] == {"query": "hello", "file_name": "a.md", "options": {"final_top_k": 5}}


def test_open_external_file_passes_offset_limit(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.open_external_file("kb_1", "file_1", offset=20, limit=80)
    finally:
        client.close()
    call = calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/v1/knowledge/databases/external/kb_1/files/file_1/open"
    assert call["params"] == {"offset": 20, "limit": 80}


def test_find_external_file_posts_patterns(monkeypatch):
    client, calls = _patched_client(monkeypatch)
    try:
        client.find_external_file(
            "kb_1",
            "file_1",
            patterns=["foo", "bar"],
            use_regex=True,
            case_sensitive=True,
            max_windows=3,
            window_size=40,
        )
    finally:
        client.close()
    call = calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/v1/knowledge/databases/external/kb_1/files/file_1/find"
    assert call["json"]["patterns"] == ["foo", "bar"]
    assert call["json"]["use_regex"] is True
    assert call["json"]["case_sensitive"] is True
    assert call["json"]["max_windows"] == 3
    assert call["json"]["window_size"] == 40
