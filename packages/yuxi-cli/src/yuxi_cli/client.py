from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlencode

import httpx

from yuxi_cli.config import Remote, build_url


class ClientError(Exception):
    def __init__(self, message: str, *, error_code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass
class CLIAuthSession:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

    @property
    def authorize_path(self) -> str:
        params = urlencode({"user_code": self.user_code})
        separator = "&" if "?" in self.verification_uri else "?"
        return f"{self.verification_uri}{separator}{params}"


class YuxiClient:
    def __init__(self, remote: Remote, timeout: float = 30.0):
        self.remote = remote
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def health(self) -> dict:
        return self._request("GET", "/system/health", auth=False)

    def discovery(self) -> dict:
        return self._request("GET", "/system/discovery", auth=False)

    def me(self, api_key: str | None = None) -> dict:
        return self._request("GET", "/auth/me", api_key=api_key)

    def create_cli_session(self) -> CLIAuthSession:
        data = self._request("POST", "/auth/cli/sessions", json={}, auth=False)
        return CLIAuthSession(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            expires_in=int(data.get("expires_in") or 600),
            interval=int(data.get("interval") or 2),
        )

    def exchange_cli_token(self, device_code: str) -> dict:
        return self._request("POST", "/auth/cli/sessions/token", json={"device_code": device_code}, auth=False)

    def delete_api_key(self, api_key_id: str) -> dict:
        return self._request("DELETE", f"/user/apikey/{api_key_id}")

    def get_database(self, kb_id: str) -> dict:
        return self._request("GET", f"/knowledge/databases/{kb_id}")

    def list_databases(self) -> dict:
        return self._request("GET", "/knowledge/databases")

    def get_knowledge_base_types(self) -> dict:
        return self._request("GET", "/knowledge/types")

    def get_supported_file_types(self) -> dict:
        return self._request("GET", "/knowledge/files/supported-types")

    def knowledge_document_exists(self, kb_id: str, filename: str) -> bool:
        data = self._request(
            "GET",
            f"/knowledge/databases/{kb_id}/documents/exists",
            params={"filename": filename},
        )
        return bool(data.get("exists"))

    def upload_knowledge_file(self, kb_id: str, path: Path, *, timeout_seconds: float = 300) -> dict:
        with path.open("rb") as fp:
            return self._request(
                "POST",
                "/knowledge/files/upload",
                params={"kb_id": kb_id},
                files={"file": (path.name, fp, "application/octet-stream")},
                timeout=timeout_seconds,
            )

    def add_uploaded_documents(self, kb_id: str, items: list[str], params: dict) -> dict:
        return self._request(
            "POST",
            f"/knowledge/databases/{kb_id}/documents/add",
            json={"items": items, "params": params},
        )

    def list_external_databases(self, api_key: str | None = None) -> dict:
        """列出 external 知识库，也可使用尚未保存的 Key 验证访问。"""
        return self._request("GET", "/v1/knowledge/databases/external", api_key=api_key)

    def list_public_agents(self, api_key: str | None = None) -> dict:
        """读取 Public Agent 目录，也可验证尚未保存的 Agents Key。"""
        return self._request("GET", "/v1/agents", api_key=api_key)

    def list_agents(self) -> dict:
        """读取当前用户可调用的主 Agent。"""
        return self._request("GET", "/agent")

    def get_agent(self, agent_slug: str) -> dict:
        """按 slug 读取当前用户可见的 Agent 配置。"""
        return self._request("GET", f"/agent/{quote(agent_slug, safe='')}")

    def list_external_files(
        self,
        kb_id: str,
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int = 100,
        status: str = "all",
    ) -> dict:
        params: dict[str, Any] = {"offset": offset, "limit": limit, "status": status}
        if query:
            params["query"] = query
        return self._request("GET", f"/v1/knowledge/databases/external/{kb_id}/files", params=params)

    def retrieve_external(
        self,
        kb_id: str,
        *,
        query: str,
        file_name: str | None = None,
        options: dict | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"/v1/knowledge/databases/external/{kb_id}/retrieve",
            json={"query": query, "file_name": file_name, "options": options or {}},
        )

    def open_external_file(self, kb_id: str, file_id: str, *, offset: int = 0, limit: int = 200) -> dict:
        return self._request(
            "GET",
            f"/v1/knowledge/databases/external/{kb_id}/files/{file_id}/open",
            params={"offset": offset, "limit": limit},
        )

    def find_external_file(
        self,
        kb_id: str,
        file_id: str,
        *,
        patterns: list[str],
        use_regex: bool = False,
        case_sensitive: bool = False,
        max_windows: int = 5,
        window_size: int = 80,
    ) -> dict:
        return self._request(
            "POST",
            f"/v1/knowledge/databases/external/{kb_id}/files/{file_id}/find",
            json={
                "patterns": patterns,
                "use_regex": use_regex,
                "case_sensitive": case_sensitive,
                "max_windows": max_windows,
                "window_size": window_size,
            },
        )

    def create_agent_thread(
        self,
        *,
        agent_slug: str,
        idempotency_key: str,
    ) -> dict:
        """通过 Public API 创建新的 Agent Thread。"""
        return self._request(
            "POST",
            "/v1/agents/threads",
            json={"agent_id": agent_slug},
            headers={"Idempotency-Key": idempotency_key},
        )

    def send_agent_message(self, thread_id: str, message: str, *, idempotency_key: str) -> dict:
        """接收一条 follow-up 消息，返回持久输入回执。"""
        return self.submit_agent_event(
            thread_id,
            {
                "type": "agent.session.input.message",
                "yuxi": {"mode": "follow_up"},
                "input": [{"role": "user", "content": [{"type": "input_text", "text": message}]}],
            },
            idempotency_key=idempotency_key,
        )

    def submit_agent_event(
        self, thread_id: str, event: dict, *, idempotency_key: str
    ) -> dict:
        """提交 Thread 输入或控制事件，并保留调用方的结构化字段。"""
        return self._request(
            "POST",
            f"/v1/agents/threads/{quote(thread_id, safe='')}/events",
            json={"events": [event]},
            headers={"Idempotency-Key": idempotency_key},
        )

    def get_agent_thread(self, thread_id: str) -> dict:
        """读取 Thread 当前持久状态。"""
        return self._request("GET", f"/v1/agents/threads/{quote(thread_id, safe='')}")

    def get_agent_turn(self, thread_id: str, turn_id: str) -> dict:
        """读取 Turn 当前持久状态和明确结果。"""
        return self._request(
            "GET", f"/v1/agents/threads/{quote(thread_id, safe='')}/turns/{quote(turn_id, safe='')}"
        )

    def get_agent_thread_queue(self, thread_id: str) -> dict:
        """读取待消费 Input 及其消费关联。"""
        return self._request("GET", f"/v1/agents/threads/{quote(thread_id, safe='')}/queue")

    def get_agent_history(self, thread_id: str) -> dict:
        """读取与实时共用的公开 item 快照。"""
        return self._request("GET", f"/v1/agents/threads/{quote(thread_id, safe='')}/history")

    def get_agent_input(self, thread_id: str, input_id: str) -> dict:
        """读取 Input 的持久消费关联。"""
        return self._request(
            "GET", f"/v1/agents/threads/{quote(thread_id, safe='')}/inputs/{quote(input_id, safe='')}"
        )

    def stream_agent_thread_events(
        self, thread_id: str, *, after_cursor: str | None = None
    ) -> Iterator[dict[str, str]]:
        """订阅 Thread 中跨 Run 的结构化事件。"""
        yield from self._stream_events(
            f"/v1/agents/threads/{quote(thread_id, safe='')}/events",
            last_event_id=after_cursor,
        )

    def _stream_events(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        last_event_id: str | None = None,
    ) -> Iterator[dict[str, str]]:
        """连接远端 SSE 接口并返回解析后的事件。"""
        headers = {}
        if self.remote.api_key:
            headers["Authorization"] = f"Bearer {self.remote.api_key}"
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        url = f"{self.remote.api_base_url}{path if path.startswith('/') else f'/{path}'}"

        try:
            with self.client.stream(
                "GET",
                url,
                headers=headers,
                params=params,
                timeout=None,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    error_code, error_message = _parse_http_error(response)
                    raise ClientError(
                        error_message,
                        error_code=error_code,
                        status_code=response.status_code,
                    )
                yield from _iter_sse_events(response.iter_lines())
        except ClientError:
            raise
        except httpx.HTTPError as exc:
            raise ClientError(f"运行事件流连接失败: {exc}") from exc

    def authorize_url(self, session: CLIAuthSession) -> str:
        return build_url(self.remote.url, session.authorize_path)

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        api_key: str | None = None,
        json: Any | None = None,
        params: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        request_headers = dict(headers or {})
        token = api_key if api_key is not None else self.remote.api_key
        if auth and token:
            request_headers["Authorization"] = f"Bearer {token}"

        url = f"{self.remote.api_base_url}{path if path.startswith('/') else f'/{path}'}"
        request_kwargs: dict[str, Any] = {"headers": request_headers}
        if params is not None:
            request_kwargs["params"] = params
        if files is not None:
            request_kwargs["files"] = files
        if data is not None:
            request_kwargs["data"] = data
        if json is not None:
            request_kwargs["json"] = json
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        try:
            response = self.client.request(method, url, **request_kwargs)
        except httpx.HTTPError as exc:
            # 网络层错误（连接失败、超时等）没有 HTTP 状态码，视为可重试的瞬时错误。
            raise ClientError(f"请求远程失败: {exc}") from exc

        if response.status_code >= 400:
            error_code, error_message = _parse_http_error(response)
            raise ClientError(error_message, error_code=error_code, status_code=response.status_code)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise ClientError("远程响应不是 JSON") from exc
        if not isinstance(data, dict):
            raise ClientError("远程响应格式无效")
        return data


def _parse_http_error(response: httpx.Response) -> tuple[str | None, str]:
    """解析远程错误，返回 (机器可读 error code, 人类可读 message)。"""
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text.strip()

    if isinstance(detail, dict):
        error = detail.get("error")
        message = detail.get("message")
        if error and message:
            return str(error), f"{error}: {message}"
        if error:
            return str(error), str(error)
        if message:
            return None, str(message)
    if detail:
        return None, str(detail)
    return None, f"HTTP {response.status_code}"


def _iter_sse_events(lines: Iterator[str]) -> Iterator[dict[str, str]]:
    """解析 SSE 文本行，忽略 heartbeat，并保留 event、data 与 id。"""
    event: dict[str, str] = {}
    data_lines: list[str] = []

    for line in lines:
        if not line:
            if data_lines:
                event["data"] = "\n".join(data_lines)
                yield event
            event = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field in {"event", "id"}:
            event[field] = value

    if data_lines:
        event["data"] = "\n".join(data_lines)
        yield event


def public_output_text(items: list[dict]) -> str:
    """从公开 message item 读取明确结果正文。"""
    return "".join(
        part["text"] for item in items if item["type"] == "message" and item["role"] == "assistant"
        for part in item["content"] if part["type"] == "output_text"
    )
