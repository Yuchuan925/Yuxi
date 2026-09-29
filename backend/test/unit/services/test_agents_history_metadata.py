"""普通历史对模型审计内部字段的边界测试。"""

from types import SimpleNamespace

from yuxi.services.agents.messages import _visible_metadata


def test_published_model_history_hides_internal_audit_metadata():
    """重放内部 metadata 时不能把模型调用上下文泄露给普通历史。"""
    message = SimpleNamespace(
        operation_id="model-call-1",
        extra_metadata={
            "source": "model",
            "langfuse_trace_id": "trace-1",
            "model_run_id": "private-run",
            "start_metadata": {"provider": "private-provider"},
            "finish_metadata": {"raw": "private-response"},
        },
    )

    assert _visible_metadata(message) == {"source": "model", "langfuse_trace_id": "trace-1"}


def test_user_history_keeps_input_metadata():
    """普通输入仍需展示 Input 归属供刷新恢复。"""
    message = SimpleNamespace(operation_id=None, extra_metadata={"input_id": "input-1", "source": "web"})

    assert _visible_metadata(message) == {"input_id": "input-1", "source": "web"}
