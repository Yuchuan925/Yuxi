from __future__ import annotations

import json
import secrets
import time
import uuid
import webbrowser
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from rich.console import Console

from yuxi_cli.client import ClientError, YuxiClient, public_output_text
from yuxi_cli.config import ConfigStore

MAX_MESSAGE_BYTES = 32 * 1024
LOOKUP_FAILURE_TIMEOUT = 60
TURN_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "waiting"}


class ChatWebError(Exception):
    """CLI Web Chat 无法启动或处理请求。"""


class ChatWebServer(ThreadingHTTPServer):
    """仅监听本机并代理 Public Thread 请求的临时 HTTP 服务。"""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        client: YuxiClient,
        agent_slug: str,
        session_token: str,
    ):
        super().__init__(address, ChatRequestHandler)
        self.client = client
        self.agent_slug = agent_slug
        self.session_token = session_token

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class ChatRequestHandler(BaseHTTPRequestHandler):
    """提供单页界面，并将 Thread 事件转为浏览器可读的增量事件。"""

    server: ChatWebServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return

        template = files("yuxi_cli").joinpath("chat.html").read_text(encoding="utf-8")
        page = template.replace(
            "__SESSION_TOKEN__", json.dumps(self.server.session_token)
        ).replace("__AGENT_SLUG__", json.dumps(self.server.agent_slug))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/chat", "/api/chat/continue"}:
            self.send_error(404)
            return
        if not self._is_local_request():
            self._send_json_error(403, "请求来源无效")
            return
        if not secrets.compare_digest(
            self.headers.get("X-Yuxi-Chat-Token", ""), self.server.session_token
        ):
            self._send_json_error(403, "会话令牌无效")
            return

        try:
            payload = self._read_payload()
            thread_id = str(payload.get("thread_id") or "").strip()
            if path == "/api/chat":
                message = str(payload.get("message") or "").strip()
                if not message:
                    raise ChatWebError("消息不能为空")
                if not thread_id:
                    created = self.server.client.create_agent_thread(
                        agent_slug=self.server.agent_slug,
                        idempotency_key=str(uuid.uuid4()),
                    )
                    thread_id = str(created["thread_id"])
                accepted = self.server.client.send_agent_message(
                    thread_id, message, idempotency_key=str(uuid.uuid4())
                )
                input_id = str(accepted["input_id"])
            else:
                input_id = str(payload.get("input_id") or "").strip()
                if not thread_id or not input_id:
                    raise ChatWebError("继续队列需要 Thread 和 Input")
                received = self.server.client.get_agent_input(thread_id, input_id)
                if received.get("status") != "pending":
                    raise ChatWebError("排队输入已变化")
                if not self.server.client.get_agent_thread_queue(thread_id).get("queue_paused"):
                    raise ChatWebError("队列未暂停")
                self.server.client.submit_agent_event(
                    thread_id,
                    {"type": "yuxi.session.input.continue"},
                    idempotency_key=str(uuid.uuid4()),
                )
                accepted = {}
        except (ChatWebError, ClientError, KeyError, json.JSONDecodeError) as exc:
            self._send_json_error(400, str(exc))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._write_event({"type": "meta", "thread_id": thread_id})
            turn_id = str(accepted.get("turn_id") or "") or self._wait_for_turn(thread_id, input_id)
            if turn_id is None:
                self._write_event({"type": "queue_paused", "input_id": input_id})
                self._write_event({"type": "done", "status": "paused"})
                return
            for event in self._follow_turn(thread_id, turn_id):
                self._write_event(event)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ChatWebError, ClientError) as exc:
            self._write_event({"type": "error", "message": str(exc)})

    def _wait_for_turn(self, thread_id: str, input_id: str) -> str | None:
        """等待 Input 领取；队列暂停时把继续操作交还给用户。"""
        while True:
            received = self.server.client.get_agent_input(thread_id, input_id)
            if received.get("turn_id"):
                return str(received["turn_id"])
            if received.get("status") == "cancelled":
                raise ChatWebError("排队输入已取消")
            if self.server.client.get_agent_thread_queue(thread_id).get("queue_paused"):
                return None
            time.sleep(0.5)

    def _follow_turn(self, thread_id: str, turn_id: str) -> Iterator[dict[str, Any]]:
        """跟随跨 Run 事件，断线后以持久 Turn 快照核对终态。"""
        cursor = None
        seen_events = set()
        text = _TextItems()
        unavailable_since = None
        while True:
            turn = self.server.client.get_agent_turn(thread_id, turn_id)
            if turn.get("status") in TURN_TERMINAL_STATUSES:
                yield from _turn_result_events(turn)
                return
            try:
                for event in self.server.client.stream_agent_thread_events(
                    thread_id, after_cursor=cursor
                ):
                    cursor = event.get("id") or cursor
                    data = json.loads(event.get("data") or "{}")
                    identity = data.get("event_id")
                    if identity and identity in seen_events:
                        continue
                    if identity:
                        seen_events.add(identity)
                    if event.get("event") == "yuxi.session.resync":
                        turn = self.server.client.get_agent_turn(thread_id, turn_id)
                        if turn.get("status") in TURN_TERMINAL_STATUSES:
                            yield from _turn_result_events(turn)
                            return
                        history = self.server.client.get_agent_history(thread_id)
                        text.merge([item for item in history["items"] if item["turn_id"] == turn_id])
                        yield {"type": "snapshot", "content": text.content()}
                        continue
                    for browser_event in _browser_events(iter((event,)), turn_id=turn_id, text=text):
                        if browser_event["type"] == "done":
                            turn = self.server.client.get_agent_turn(thread_id, turn_id)
                            yield from _turn_result_events(turn)
                            return
                        yield browser_event
                unavailable_since = None
            except ClientError as exc:
                if exc.status_code is not None and exc.status_code < 500 and exc.status_code != 429:
                    raise
                unavailable_since = unavailable_since or time.monotonic()
                if time.monotonic() - unavailable_since >= LOOKUP_FAILURE_TIMEOUT:
                    raise ChatWebError(f"事件流持续不可用: {exc}") from exc
            time.sleep(0.5)

    def _is_local_request(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin == self.server.origin

    def _read_payload(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            raise ChatWebError("请求缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ChatWebError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_MESSAGE_BYTES:
            raise ChatWebError("消息过长")

        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ChatWebError("请求内容必须是 JSON 对象")
        return payload

    def _write_event(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        self.wfile.flush()

    def _send_json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _turn_result_events(turn: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """根据持久 Turn 状态输出最终结果或等待提示。"""
    status = turn.get("status")
    if status == "completed":
        yield {"type": "snapshot", "content": public_output_text(turn.get("output") or [])}
        yield {"type": "done", "status": "completed"}
    elif status == "waiting":
        yield {"type": "approval_required", "message": "等待用户操作，请在 Yuxi 网页继续"}
        yield {"type": "done", "status": "waiting"}
    elif status in {"failed", "cancelled"}:
        runs = turn.get("runs") or []
        failure = runs[-1].get("error_message") if runs else None
        yield {"type": "error", "message": str(failure or status)}
    else:
        raise ChatWebError("Turn 尚未结束，无法读取最终结果")


class _TextItems:
    """按公开 item 和内容块保留文本，完成边界阻止离线期间的旧增量重放。"""

    def __init__(self):
        """每个 Turn 独立维护展示状态。"""
        self.parts = {}
        self.indices = {}
        self.closed = set()

    def merge(self, items):
        """持久快照只覆盖已结束块，避免丢失尚未持久化的本地增量。"""
        for item in items:
            if item["type"] != "message" or item.get("role") != "assistant":
                continue
            self.indices[item["id"]] = item.get("yuxi", {}).get("output_index", 0)
            completed = set(item.get("yuxi", {}).get("completed_content_indices", []))
            for index, part in enumerate(item["content"]):
                if part["type"] != "output_text":
                    continue
                key = (item["id"], index)
                if key not in self.parts or index in completed or item["status"] != "in_progress":
                    self.parts[key] = part["text"]
                if index in completed or item["status"] != "in_progress":
                    self.closed.add(key)

    def content(self):
        """按输出索引及内容块索引派生当前正文。"""
        return "".join(self.parts[key] for key in sorted(
            self.parts, key=lambda key: (self.indices.get(key[0], 0), key[1])))


def _browser_events(
    events: Iterator[dict[str, str]], *, turn_id: str, text: _TextItems | None = None
) -> Iterator[dict[str, Any]]:
    """从目标 Turn 的事件提取文本增量和终态通知。"""
    text = text or _TextItems()
    for event in events:
        try:
            data = json.loads(event.get("data") or "{}")
        except json.JSONDecodeError as exc:
            raise ChatWebError("远端返回了无效的流事件") from exc
        if not isinstance(data, dict) or data.get("turn_id") != turn_id:
            continue
        event_type = str(data.get("type") or event.get("event") or "")
        if event_type == "agent.session.turn.item.added":
            text.merge([data["item"]])
        elif event_type == "agent.session.turn.item.done":
            text.merge([data["item"]])
            if data["item"]["type"] == "message":
                yield {"type": "snapshot", "content": text.content()}
        elif event_type.startswith("agent.session.turn.output_text."):
            key = (data["item_id"], data["content_index"])
            text.indices[data["item_id"]] = data["output_index"]
            if event_type.endswith(".delta") and key not in text.closed and data["delta"]:
                text.parts[key] = text.parts.get(key, "") + data["delta"]
                yield {"type": "delta", "content": data["delta"]}
            elif event_type.endswith(".done"):
                text.parts[key] = data["text"]
                text.closed.add(key)
                yield {"type": "snapshot", "content": text.content()}
        if event_type in {
            "agent.session.turn.completed", "yuxi.session.turn.waiting",
            "agent.session.turn.failed", "agent.session.turn.cancelled",
        }:
            yield {"type": "done", "status": event_type.rsplit(".", 1)[-1]}


def run_web_chat(
    store: ConfigStore,
    remote_name: str | None,
    agent_slug: str,
    console: Console,
    *,
    no_open: bool = False,
    open_browser: Callable[[str], bool] = webbrowser.open,
) -> None:
    """启动本地 Web Chat，直至用户按下 Ctrl+C。"""
    remote = store.load().get_remote(remote_name)
    if not remote.has_api_key:
        raise ChatWebError("当前 remote 尚未登录，请先运行 yuxi login")

    client = YuxiClient(remote)
    server = ChatWebServer(
        ("127.0.0.1", 0), client, agent_slug, secrets.token_urlsafe(24)
    )
    url = server.origin
    console.print(f"Web Chat: {url}")
    console.print("按 Ctrl+C 退出")
    if not no_open:
        open_browser(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\nWeb Chat 已关闭")
    finally:
        server.server_close()
        client.close()
