"""普通历史仅投影公开 item，不暴露模型审计和配置。"""
from datetime import datetime
from types import SimpleNamespace
from yuxi.modules.agents.services.public_items import serialize_public_items


def test_published_model_history_hides_internal_audit_metadata():
    """未进入用户输出链路的模型审计没有公开 item。"""
    message = SimpleNamespace(role="assistant", extra_metadata={
        "start_metadata": {"prompt": "private"}, "finish_metadata": {"raw": "private"}
    })
    assert serialize_public_items(message, None, None) == []


def test_user_history_keeps_input_identity_and_hides_internal_metadata():
    """用户输入保留回执关联，内部 metadata 不随 item 返回。"""
    message = SimpleNamespace(id=1, role="user", run_id=None, turn_id=None,
        content="hello", delivery_status="queued", created_at=datetime(2026, 9, 30), message_type="text",
        extra_metadata={"input_id": "input-1", "source": "web", "private": "secret"})
    item, = serialize_public_items(message, None, None)
    assert item["yuxi"]["input_id"] == "input-1"
    assert item["content"] == [{"type": "input_text", "text": "hello"}]
    assert "secret" not in str(item)
