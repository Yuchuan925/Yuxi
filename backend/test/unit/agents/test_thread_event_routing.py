from contextlib import aclosing
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk

from yuxi.modules.agents.runtime.base import BaseAgent, _collect_subagent_routes
from yuxi.modules.agents.runtime.context import BaseContext
from yuxi.shared.hashing import subagent_child_thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["configurable", "metadata", "stream_event", "meta"])
async def test_langgraph_message_does_not_route_by_nested_thread_id(container):
    """消息归属只能来自协议 metadata 的顶层字段。"""

    class Graph:
        """提供外部流的明确 namespace 与嵌套业务数据。"""

        async def astream_events(self, *_args, **_kwargs):
            """同时提供未路由和已明确路由的子线程消息。"""

            async def events():
                """嵌套字段不能将未路由消息变成父线程消息。"""
                for metadata in ({container: {"thread_id": "parent-thread"}}, {"thread_id": "child-thread"}):
                    yield {
                        "method": "messages",
                        "params": {
                            "namespace": ["child:task"],
                            "data": (AIMessageChunk(content="child"), metadata),
                        },
                    }

            return aclosing(events())

        async def aget_state(self, _config):
            """提供流终点的状态。"""
            return {}

    class Agent(BaseAgent):
        """使用真实接入转换逻辑。"""

        async def get_graph(self, **_kwargs):
            """返回测试协议流。"""
            return Graph()

    events = [
        event
        async for event in Agent().stream_messages_with_state(
            ["hello"], context=BaseContext(thread_id="parent-thread", uid="user-1")
        )
    ]
    assert "thread_id" not in events[0][1][1]
    assert events[1][1][1]["thread_id"] == "child-thread"


@pytest.mark.asyncio
async def test_subgraph_route_uses_handle_identity_instead_of_arbitrary_state():
    """子图句柄的名字与调用标识决定已有确定性路由。"""

    async def subagents():
        """非协议属性中的线程字段不能覆盖子图标识。"""
        yield SimpleNamespace(
            path=("child:task",),
            graph_name="worker",
            trigger_call_id="call-1",
            metadata={"thread_id": "unrelated-thread"},
            state={"configurable": {"thread_id": "unrelated-thread"}},
        )

    routes = {}
    await _collect_subagent_routes(SimpleNamespace(subagents=subagents()), "parent-thread", routes)

    assert routes[("child:task",)] == {
        "thread_id": subagent_child_thread_id("parent-thread", "worker", "call-1"),
        "parent_thread_id": "parent-thread",
        "subagent_slug": "worker",
        "tool_call_id": "call-1",
    }
