"""等待取消的 checkpoint 重入单测。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from yuxi.modules.agents.services.turns import _drain_waitpoint_checkpoint

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class FakeCheckpointGraph:
    """模拟首个持久改写成功后进程失联。"""

    def __init__(self, message: AIMessage):
        """保存初始消息与待执行节点。"""
        self.message = message
        self.next = ("tools",)
        self.updates = []
        self.crash_after_patch = False

    async def aget_state(self, _config):
        """返回当前持久状态。"""
        return SimpleNamespace(next=self.next, values={"messages": [self.message]})

    async def aupdate_state(self, _config, values, *, as_node):
        """持久改写后可模拟崩溃，空改写推进节点。"""
        self.updates.append((values, as_node))
        if values.get("messages"):
            self.message = values["messages"][0]
            if self.crash_after_patch:
                raise RuntimeError("进程失联")
        else:
            self.next = ()


async def test_waitpoint_cleanup_retries_after_message_patch():
    """首次改写成功但未推进节点时，重试仍能收敛。"""
    graph = FakeCheckpointGraph(
        AIMessage(id="pending", content="", tool_calls=[{"id": "tool-1", "name": "act", "args": {}}])
    )
    graph.crash_after_patch = True
    with pytest.raises(RuntimeError, match="进程失联"):
        await _drain_waitpoint_checkpoint(graph, {})

    assert graph.message.content == "[已取消]"
    assert graph.message.tool_calls == []
    assert graph.next == ("tools",)

    graph.crash_after_patch = False
    await _drain_waitpoint_checkpoint(graph, {})

    assert graph.next == ()
    assert len(graph.updates) == 2
    assert graph.updates[-1] == ({}, "tools")


async def test_waitpoint_cleanup_rejects_unrelated_checkpoint_without_tool_calls():
    """末尾普通 AI 消息不能伪装成已经清理过的等待点。"""
    graph = FakeCheckpointGraph(AIMessage(id="ordinary", content="普通回答", tool_calls=[]))

    with pytest.raises(ValueError, match="缺少未执行的工具调用"):
        await _drain_waitpoint_checkpoint(graph, {})

    assert graph.updates == []
