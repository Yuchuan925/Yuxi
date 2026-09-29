from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yuxi_cli.chat_web as chat_web_module
from yuxi_cli.chat_web import ChatWebError, ChatWebServer, _browser_events, run_web_chat
from yuxi_cli.config import ConfigStore


class FakeChatClient:
    def __init__(
        self, *, queued: bool = False, queue_paused: bool = False,
        stream_disconnect: bool = False, stream_resync: bool = False,
    ):
        self.queued = queued
        self.queue_paused = queue_paused
        self.stream_disconnect = stream_disconnect
        self.stream_resync = stream_resync
        self.calls = []
        self.input_reads = 0
        self.turn_reads = 0

    def create_agent_thread(self, *, agent_slug, idempotency_key):
        self.calls.append(("create", agent_slug, idempotency_key))
        return {"thread_id": "thread-1"}

    def send_agent_message(self, thread_id, message, *, idempotency_key):
        self.calls.append(("message", thread_id, message, idempotency_key))
        return {
            "input_id": "input-1",
            "turn_id": None if self.queued else "turn-1",
        }

    def get_agent_input(self, thread_id, input_id):
        self.calls.append(("input", thread_id, input_id))
        self.input_reads += 1
        return (
            {"status": "pending", "turn_id": None}
            if self.queue_paused or self.input_reads == 1
            else {"status": "consumed", "turn_id": "turn-1", "run_id": "run-1"}
        )

    def get_agent_thread_queue(self, thread_id):
        self.calls.append(("queue", thread_id))
        return {"queue_paused": self.queue_paused}

    def submit_agent_event(self, thread_id, event, *, idempotency_key):
        self.calls.append(("event", thread_id, event, idempotency_key))
        assert event == {"type": "yuxi.thread.input.continue"}
        self.queue_paused = False
        return {"status": "accepted"}

    def get_agent_turn(self, thread_id, turn_id):
        self.calls.append(("turn", thread_id, turn_id))
        self.turn_reads += 1
        if self.queued or self.turn_reads > 1:
            return {"status": "completed", "output": {"content": "最终回答"}}
        return {"status": "running", "current_run_id": "run-1"}

    def stream_agent_thread_events(self, thread_id, *, after_cursor=None):
        self.calls.append(("stream", thread_id, after_cursor))
        if self.stream_disconnect:
            return iter(())
        if self.stream_resync:
            return iter([{"event": "agent.thread.resync", "id": "cursor-1", "data": "{}"}])
        return iter([
            {
                "event": "agent.thread.run.output",
                "id": "cursor-1",
                "data": json.dumps({
                    "type": "agent.thread.run.output",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "run_id": "run-1",
                    "payload": {"chunk": {"stream_event": {
                        "type": "message_delta", "content": "部分"
                    }}},
                }),
            },
            {
                "event": "agent.thread.turn.completed",
                "id": "cursor-2",
                "data": json.dumps({
                    "type": "agent.thread.turn.completed",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "run_id": "run-1",
                    "payload": {},
                }),
            },
        ])


@contextmanager
def running_server(client):
    server = ChatWebServer(("127.0.0.1", 0), client, "default-chatbot", "session-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def send_chat(server, body, *, token="session-secret", origin=None, path="/api/chat"):
    connection = http.client.HTTPConnection(*server.server_address[:2])
    headers = {
        "Content-Type": "application/json",
        "X-Yuxi-Chat-Token": token,
    }
    if origin is not None:
        headers["Origin"] = origin
    connection.request("POST", path, body=json.dumps(body), headers=headers)
    response = connection.getresponse()
    status, raw = response.status, response.read()
    connection.close()
    return status, raw


def test_local_chat_uses_public_thread_and_turn_result():
    client = FakeChatClient()
    with running_server(client) as server:
        status, raw = send_chat(server, {"message": "你好"})
    assert status == 200
    events = [json.loads(line) for line in raw.splitlines()]
    assert events == [
        {"type": "meta", "thread_id": "thread-1"},
        {"type": "delta", "content": "部分"},
        {"type": "snapshot", "content": "最终回答"},
        {"type": "done", "status": "completed"},
    ]
    assert [call[0] for call in client.calls] == [
        "create", "message", "turn", "stream", "turn"
    ]
    assert client.calls[1][2] == "你好"


def test_queued_input_resolves_from_durable_input_before_turn(monkeypatch):
    monkeypatch.setattr(chat_web_module.time, "sleep", lambda _: None)
    client = FakeChatClient(queued=True)
    with running_server(client) as server:
        status, raw = send_chat(server, {"message": "排队"})
    assert status == 200
    events = [json.loads(line) for line in raw.splitlines()]
    assert events[-2:] == [
        {"type": "snapshot", "content": "最终回答"},
        {"type": "done", "status": "completed"},
    ]
    assert client.input_reads == 2
    assert not any(call[0] == "stream" for call in client.calls)


def test_paused_queue_returns_continue_action_and_resumes_same_input():
    """失败后已接收的 Input 不再无限等待，也不重复发送消息。"""
    client = FakeChatClient(queued=True, queue_paused=True)
    with running_server(client) as server:
        status, raw = send_chat(server, {"message": "排队"})
        assert status == 200
        assert [json.loads(line) for line in raw.splitlines()] == [
            {"type": "meta", "thread_id": "thread-1"},
            {"type": "queue_paused", "input_id": "input-1"},
            {"type": "done", "status": "paused"},
        ]
        status, raw = send_chat(
            server, {"thread_id": "thread-1", "input_id": "input-1"},
            path="/api/chat/continue",
        )
    assert status == 200
    assert [json.loads(line) for line in raw.splitlines()][-2:] == [
        {"type": "snapshot", "content": "最终回答"},
        {"type": "done", "status": "completed"},
    ]
    assert [call[0] for call in client.calls].count("message") == 1
    assert [call[0] for call in client.calls].count("event") == 1


def test_continue_queue_rejects_untrusted_request():
    """继续控制事件仍受本地会话令牌保护。"""
    client = FakeChatClient(queued=True, queue_paused=True)
    with running_server(client) as server:
        status, raw = send_chat(
            server, {"thread_id": "thread-1", "input_id": "input-1"},
            token="wrong", path="/api/chat/continue",
        )
    assert status == 403
    assert "令牌" in json.loads(raw)["error"]
    assert not any(call[0] == "event" for call in client.calls)


def test_disconnected_thread_stream_rechecks_turn_result(monkeypatch):
    monkeypatch.setattr(chat_web_module.time, "sleep", lambda _: None)
    client = FakeChatClient(stream_disconnect=True)
    with running_server(client) as server:
        status, raw = send_chat(server, {"message": "断线"})
    assert status == 200
    events = [json.loads(line) for line in raw.splitlines()]
    assert events[-2:] == [
        {"type": "snapshot", "content": "最终回答"},
        {"type": "done", "status": "completed"},
    ]
    assert any(call[0] == "stream" for call in client.calls)


def test_expired_cursor_rechecks_turn_before_waiting_for_more_events():
    client = FakeChatClient(stream_resync=True)
    with running_server(client) as server:
        status, raw = send_chat(server, {"message": "恢复"})
    assert status == 200
    events = [json.loads(line) for line in raw.splitlines()]
    assert events[-2:] == [
        {"type": "snapshot", "content": "最终回答"},
        {"type": "done", "status": "completed"},
    ]
    assert client.turn_reads == 2


def test_browser_events_ignore_other_turn_and_report_target_completion():
    events = iter([
        {"event": "agent.thread.turn.completed", "data": json.dumps({
            "type": "agent.thread.turn.completed", "turn_id": "other", "payload": {}
        })},
        {"event": "agent.thread.turn.completed", "data": json.dumps({
            "type": "agent.thread.turn.completed", "turn_id": "target", "payload": {}
        })},
    ])
    assert list(_browser_events(events, turn_id="target")) == [
        {"type": "done", "status": "completed"}
    ]


@pytest.mark.parametrize(
    ("token", "origin", "expected"),
    [
        ("wrong", None, "会话令牌无效"),
        ("session-secret", "https://attacker.example", "请求来源无效"),
    ],
)
def test_local_chat_rejects_untrusted_requests(token, origin, expected):
    with running_server(FakeChatClient()) as server:
        status, raw = send_chat(server, {"message": "你好"}, token=token, origin=origin)
    assert status == 403
    assert expected in json.loads(raw)["error"]


def test_local_chat_rejects_invalid_json():
    with running_server(FakeChatClient()) as server:
        connection = http.client.HTTPConnection(*server.server_address[:2])
        connection.request("POST", "/api/chat", body="{broken", headers={
            "Content-Type": "application/json",
            "X-Yuxi-Chat-Token": "session-secret",
        })
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
    assert status == 400


def test_run_web_chat_requires_login(tmp_path):
    store = ConfigStore(tmp_path / "config.toml")
    with pytest.raises(ChatWebError, match="尚未登录"):
        run_web_chat(store, None, "default-chatbot", console=None, no_open=True)


def test_run_web_chat_opens_browser_and_closes_resources(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.toml")
    config = store.load()
    config.get_remote("local").api_key = "yxkey_local"
    store.save(config)
    calls = []

    class FakeClient:
        def __init__(self, remote):
            calls.append(("client", remote.name))

        def close(self):
            calls.append(("client_closed",))

    class FakeServer:
        def __init__(self, address, client, agent_slug, session_token):
            assert address == ("127.0.0.1", 0)
            assert agent_slug == "default-chatbot"
            assert session_token
            self.origin = "http://127.0.0.1:12345"

        def serve_forever(self):
            calls.append(("serve",))

        def server_close(self):
            calls.append(("server_closed",))

    monkeypatch.setattr(chat_web_module, "YuxiClient", FakeClient)
    monkeypatch.setattr(chat_web_module, "ChatWebServer", FakeServer)
    run_web_chat(
        store, None, "default-chatbot",
        console=SimpleNamespace(print=lambda *_args: None),
        open_browser=lambda url: calls.append(("open", url)),
    )
    assert calls == [
        ("client", "local"),
        ("open", "http://127.0.0.1:12345"),
        ("serve",),
        ("server_closed",),
        ("client_closed",),
    ]
