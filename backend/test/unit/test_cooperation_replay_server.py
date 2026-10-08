"""协作重放器的摘要协议与普通请求拒绝边界。"""

from http.server import ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from test.support.cooperation_replay_server import CooperationReplayHandler

pytestmark = pytest.mark.unit


@pytest.fixture()
def replay_url():
    """启动仅接受测试请求的本地 HTTP 重放器。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), CooperationReplayHandler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def test_summary_request_returns_nonstream_completion(replay_url):
    """主动压缩的明确摘要场景无需 tools，返回真实 JSON completion。"""
    response = httpx.post(
        replay_url,
        headers={"Authorization": "Bearer ci-replay-key"},
        json={
            "model": "deterministic-chat",
            "stream": False,
            "messages": [{"role": "user", "content": "COOPERATION_SUMMARY_FIXTURE\n<message>history</message>"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "COOPERATION_SUMMARY_OK"
    assert body["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize(
    "authorization,stream,content,error",
    [
        ("Bearer wrong-key", False, "COOPERATION_SUMMARY_FIXTURE\nhistory", "authorization"),
        ("Bearer ci-replay-key", True, "COOPERATION_SUMMARY_FIXTURE\nhistory", "uniform tools missing"),
        ("Bearer ci-replay-key", False, "ordinary model request", "model contract"),
    ],
)
def test_summary_branch_does_not_accept_invalid_normal_requests(replay_url, authorization, stream, content, error):
    """摘要 fixture 不放宽鉴权或普通工具调用的契约。"""
    response = httpx.post(
        replay_url,
        headers={"Authorization": authorization},
        json={
            "model": "deterministic-chat",
            "stream": stream,
            "tools": [],
            "messages": [{"role": "user", "content": content}],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == error
