"""统一记录器的混合生命周期、提交边界与规整事件契约。"""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

import yuxi.modules.agents.services.message_recorder as service
from yuxi.modules.agents.services.message_recorder import RunMessageRecorder
from yuxi.modules.agents.services.openai_events import OpenAIEventAdapter


@pytest.fixture
def recorded_messages(monkeypatch):
    """以事务副本模拟提交后的消息事实，不替代 PostgreSQL 约束验证。"""
    committed = {}

    @asynccontextmanager
    async def session():
        pending = deepcopy(committed)
        yield pending
        committed.clear()
        committed.update(pending)

    class ModelRepository:
        """保存模型聚合结果，供提交后回读。"""

        def __init__(self, db):
            """借用当前事务副本。"""
            self.db = db

        async def start(self, **kwargs):
            """创建模型开始事实，重复开始保持原值。"""
            key = ("model", kwargs["operation_id"])
            created = key not in self.db
            self.db.setdefault(key, {**kwargs, "execution_status": "running"})
            return SimpleNamespace(id=1), created

        async def finish(self, **kwargs):
            """保存模型终态与完整聚合内容。"""
            self.db[("model", kwargs["operation_id"])].update(kwargs, execution_status="completed")

    class ToolRepository:
        """保存工具开始与完整结果，供公开投影独立回读。"""

        def __init__(self, db):
            """借用当前事务副本。"""
            self.db = db

        async def start(self, **kwargs):
            """创建工具开始事实，重复开始保持原值。"""
            key = ("tool", kwargs["tool_call_id"])
            created = key not in self.db
            self.db.setdefault(key, {**kwargs, "execution_status": "running"})
            return SimpleNamespace(id=2), created

        async def complete(self, **kwargs):
            """保存工具输出与完成状态。"""
            self.db[("tool", kwargs["tool_call_id"])].update(kwargs, execution_status="completed")

    monkeypatch.setattr(service.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(service, "ModelMessageAuditRepository", ModelRepository)
    monkeypatch.setattr(service, "ToolMessageAuditRepository", ToolRepository)
    return committed


def native(seq, data, *, method="messages", namespace=None, model_id="model"):
    """构造固定原生事件，不从生产实现生成预期结果。"""
    return {
        "method": method,
        "seq": seq,
        "params": {
            "timestamp": 1_777_000_123_456 + seq,
            "namespace": namespace or [],
            "data": (data, {"run_id": model_id}) if method == "messages" else data,
        },
    }


@pytest.mark.asyncio
async def test_same_recorder_keeps_interleaved_model_and_tool_states_separate(monkeypatch, recorded_messages):
    """交错模型来源与同名工具身份保持各自内容、usage 和单调耗时。"""
    clock = iter([100.0, 100.1, 100.2, 100.3, 100.4, 100.5])
    monkeypatch.setattr(service, "monotonic", lambda: next(clock))
    recorder = RunMessageRecorder(run_id="run", thread_id="thread", worker_id="worker")
    events = [
        native(0, {"event": "message-start", "id": "shared"}, namespace=["first"]),
        native(1, {"event": "message-start", "id": "second"}, namespace=["second"]),
        native(
            2,
            {"event": "content-block-delta", "delta": {"fields": {"type": "text-delta", "text": "一"}}},
            namespace=["first"],
        ),
        native(
            3, {"event": "content-block-delta", "delta": {"type": "text-delta", "text": "二"}}, namespace=["second"]
        ),
        native(4, {"event": "message-finish", "usage": {"input_tokens": 5}}, namespace=["first"]),
        native(
            5,
            {"event": "tool-started", "tool_call_id": "shared", "tool_name": "search", "input": {"q": "实际输入"}},
            method="tools",
        ),
        native(6, {"event": "message-finish", "usage": {"output_tokens": 3}}, namespace=["second"]),
        native(7, {"event": "tool-finished", "tool_call_id": "shared", "output": "工具结果"}, method="tools"),
    ]
    for event in events:
        await recorder.consume(event)

    first, second, tool = (
        recorded_messages[key] for key in [("model", "shared"), ("model", "second"), ("tool", "shared")]
    )
    assert first["content"] == "一" and first["usage"] == {"input_tokens": 5}
    assert second["content"] == "二" and second["usage"] == {"output_tokens": 3}
    assert tool["content"] == "工具结果" and tool["tool_input"] == {"q": "实际输入"}
    assert [row["duration_ms"] for row in (first, second, tool)] == [200, 300, 200]
    assert [row["sequence"] for row in (first, second, tool)] == [0, 1, 5]
    for row in (first, second, tool):
        assert row["run_id"] == "run" and row["thread_id"] == "thread" and row["worker_id"] == "worker"
        assert row["execution_status"] == "completed"


@pytest.mark.asyncio
async def test_command_result_is_committed_once_before_public_projection(monkeypatch, recorded_messages):
    """记录器提交同一结果后才返回事件，公开适配失败也保留已提交工具事实。"""
    recorder = RunMessageRecorder(run_id="run", thread_id="thread", worker_id="worker")
    await recorder.consume(
        native(0, {"event": "tool-started", "tool_call_id": "call", "tool_name": "search", "input": {}}, method="tools")
    )
    command = Command(
        update={
            "messages": [
                ToolMessage(content="相邻结果", tool_call_id="other"),
                ToolMessage(content="目标结果", tool_call_id="call"),
            ]
        }
    )
    source = native(
        1, {"event": "tool-finished", "tool_call_id": "call", "output": command}, method="tools", namespace=["tools"]
    )
    extract = service.tool_event_output
    extractions = []

    def extract_once(data):
        """只监测重复规整，最终事实另由结果回读证明。"""
        extractions.append(data["tool_call_id"])
        return extract(data)

    monkeypatch.setattr(service, "tool_event_output", extract_once)
    normalized = await recorder.consume(source)
    output = normalized["params"]["data"]["output"]
    assert source["params"]["data"]["output"] is command
    assert source["seq"] == normalized["seq"] == 1
    assert normalized["params"]["timestamp"] == source["params"]["timestamp"]
    assert normalized["params"]["namespace"] == ["tools"]
    assert output["tool_call_id"] == "call" and output["content"] == "目标结果"
    assert recorded_messages[("tool", "call")]["output"] == output
    assert recorded_messages[("tool", "call")]["execution_status"] == "completed"

    adapter = OpenAIEventAdapter(run_id="run", turn_id="turn", thread_id="thread", worker_id="worker")
    adapter.calls["call"] = ("model", "call:call", {"id": "declared-call", "yuxi": {"output_index": 0}})

    async def save_item(operation, role, key, item, content_index=None):
        """公开结果只能使用已经提交的工具输出。"""
        assert recorded_messages[("tool", "call")]["output"]["content"] == item.get("output", "目标结果")
        return {**item, "id": f"{operation}:{key}", "yuxi": {"output_index": 1}}

    monkeypatch.setattr(adapter, "_save", save_item)
    public = await adapter.consume(normalized)
    assert public[-1]["item"]["output"] == "目标结果"
    assert public[-1]["item"]["call_id"] == "call"
    assert extractions == ["call"]

    async def reject_public(*args, **kwargs):
        """模拟公开快照拒绝写入。"""
        raise ValueError("public rejected")

    monkeypatch.setattr(adapter, "_save", reject_public)
    with pytest.raises(ValueError, match="public rejected"):
        await adapter.consume(normalized)
    assert recorded_messages[("tool", "call")]["content"] == "目标结果"
    assert recorded_messages[("tool", "call")]["execution_status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,data",
    [
        ("values", {}),
        ("custom", {"event": "other"}),
        ("tools", {"event": "tool-progress"}),
        ("messages", {"event": "message-finish"}),
    ],
)
async def test_non_lifecycle_events_do_not_open_transactions(monkeypatch, method, data):
    """非持久化边界与无开始事实的模型结束不增加事务。"""

    def unexpected_session():
        """任何无关数据库访问直接暴露目标缺陷。"""
        raise AssertionError("unexpected transaction")

    monkeypatch.setattr(service.pg_manager, "get_async_session_context", unexpected_session)
    event = native(0, data, method=method)
    recorder = RunMessageRecorder(run_id="run", thread_id="thread", worker_id="worker")
    assert await recorder.consume(event) is event


@pytest.mark.asyncio
async def test_foreign_thread_does_not_create_model_fact(recorded_messages):
    """记录入口只接受当前 Thread 的模型来源。"""
    event = native(0, {"event": "message-start", "id": "foreign"})
    event["params"]["data"][1]["thread_id"] = "other"
    recorder = RunMessageRecorder(run_id="run", thread_id="thread", worker_id="worker")
    assert await recorder.consume(event) is event
    assert recorded_messages == {}
