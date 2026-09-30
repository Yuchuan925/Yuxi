"""测试执行器中的等待点恢复与中断投影。"""

import json
from types import SimpleNamespace

import pytest

from yuxi.modules.agents.services.execution import (
    _build_ask_user_question_payload,
    _build_tool_approval_payload,
    stream_agent_resume,
)
from test.unit.agent_context_fixtures import prepared_execution
import yuxi.modules.agents.services.execution as svc
from yuxi.modules.agents.services.execution import RunExecutionResult
from yuxi.modules.agents.services.tracing import LangfuseRunContext


def _chunk(event):
    """展开执行终结结果，保留原始结构化增量。"""
    return event.chunk if isinstance(event, RunExecutionResult) else event


class _FakeSession:
    def __init__(self):
        self.commit_count = 0

    async def commit(self):
        self.commit_count += 1


async def _resolve_test_workdir(**_kwargs):
    """返回测试 Conversation 的 Project Workdir。"""

    return "projects/11111111-1111-4111-8111-111111111111"


def test_build_tool_approval_payload_rejects_mismatched_lists():
    assert _build_tool_approval_payload({"action_requests": [{}], "review_configs": []}, "thread-1") is None


def test_question_projection_preserves_checkpoint_structure_and_ids():
    """服务层只投影标准等待点，不重新规范化或生成回答键。"""
    questions = [
        {
            "question_id": "destination",
            "question": "你想去哪个城市？",
            "options": [],
            "multi_select": False,
            "allow_other": True,
        },
        {
            "question_id": "style",
            "question": "选择风格",
            "options": [{"label": "简洁", "value": "simple", "description": "简洁布局"}],
            "multi_select": True,
            "allow_other": False,
            "operation": "确认风格",
        },
    ]
    result = svc.build_pending_interrupt_payload(
        SimpleNamespace(value={"questions": questions, "source": "ask_user_question"}), "thread-1"
    )
    assert result == {
        "status": "ask_user_question_required",
        "questions": questions,
        "source": "ask_user_question",
        "thread_id": "thread-1",
    }
    assert result["questions"] is questions


@pytest.mark.parametrize("payload", [{}, {"questions": []}, {"questions": None}, {"questions": "[]"}])
def test_question_projection_rejects_missing_questions_instead_of_inventing_placeholder(payload):
    """无效中断不能伪装为可回答的占位题。"""
    with pytest.raises(ValueError, match="缺少标准 questions"):
        _build_ask_user_question_payload(payload, "thread-1")


def test_approval_projection_keeps_approval_protocol():
    """提问契约收敛不影响工具审批。"""
    approval = {
        "action_requests": [{"name": "execute", "args": {"command": "pwd"}}],
        "review_configs": [{"action_name": "execute", "allowed_decisions": ["approve", "reject"]}],
    }
    result = svc.build_pending_interrupt_payload(approval, "thread-1")
    assert result == {"status": "human_approval_required", "approval": approval, "thread_id": "thread-1"}


@pytest.mark.asyncio
async def test_stream_agent_resume_init_does_not_render_resume_input():
    stream = stream_agent_resume(
        prepared_execution=prepared_execution(),
        thread_id="thread-1",
        resume_input={"language": "python"},
        meta={"turn_id": "turn-1", "run_id": "run-1", "worker_id": "worker-1"},
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    first_chunk = _chunk(await stream.__anext__())
    await stream.aclose()

    assert first_chunk["status"] == "init"
    assert "msg" not in first_chunk
    assert "Resume with input" not in json.dumps(first_chunk, ensure_ascii=False)


@pytest.mark.asyncio
async def test_stream_agent_resume_commits_before_stream_and_routes_subagent_chunks(monkeypatch):
    db = _FakeSession()
    lifecycle: list[str] = []
    calls: dict[str, object] = {}

    class FakeContext:
        def __init__(self):
            self.thread_id = None
            self.uid = None

        def update(self, values):
            for key, value in values.items():
                setattr(self, key, value)

        def model_dump(self):
            return {"thread_id": self.thread_id, "uid": self.uid}

    class FakeAgent:
        context_schema = FakeContext

        async def stream_resume_with_state(self, resume_command, input_context=None, **kwargs):
            await kwargs.pop("on_prepared")()
            assert db.commit_count == 1
            assert lifecycle[-1] == "prepared"
            lifecycle.append("streaming")
            kwargs.pop("context")
            calls["stream_kwargs"] = kwargs
            yield (
                "messages",
                (
                    {"content": "child token", "id": "msg-child"},
                    {"namespace": ["task:1"], "thread_id": "child-thread"},
                ),
            )
            yield "checkpoint", SimpleNamespace(values={})

        async def get_graph(self, context=None):
            class FakeGraph:
                async def aget_state(self, _config):
                    return SimpleNamespace(values={})

            return FakeGraph()

    async def fake_resolve_agent_runtime(**_kwargs):
        return (
            SimpleNamespace(slug="main-agent", name="主智能体", backend_id="ChatbotAgent"),
            FakeAgent(),
            prepared_execution().context,
            SimpleNamespace(
                id=1,
                uid="user-1",
                status="active",
                project_id="11111111-1111-4111-8111-111111111111",
                extra_metadata={"attachments": []},
            ),
        )

    async def fake_save_messages_from_langgraph_state(**_kwargs):
        return None

    async def fake_check_and_handle_interrupts(*_args, **_kwargs):
        if False:
            yield None

    monkeypatch.setattr(svc, "_resolve_agent_runtime", fake_resolve_agent_runtime)
    monkeypatch.setattr(svc, "resolve_conversation_workdir_path", _resolve_test_workdir)
    monkeypatch.setattr(
        svc,
        "_build_langfuse_run_context",
        lambda **_kwargs: LangfuseRunContext(),
    )
    monkeypatch.setattr(svc, "check_and_handle_interrupts", fake_check_and_handle_interrupts)
    monkeypatch.setattr(svc, "save_messages_from_langgraph_state", fake_save_messages_from_langgraph_state)

    class FakeConversationRepository:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, _thread_id):
            return SimpleNamespace(
                id=1,
                uid="user-1",
                status="active",
                project_id="11111111-1111-4111-8111-111111111111",
                extra_metadata={"attachments": []},
            )

        async def get_attachments(self, _conversation_id):
            return []

    monkeypatch.setattr(svc, "ConversationRepository", FakeConversationRepository)

    class UnexpectedSandboxBackend:
        def __init__(self, **_kwargs):
            raise AssertionError("Resume 流不应在执行前构造 Sandbox Backend")

        def ensure_available(self):
            raise AssertionError("Resume 流不应预创建 Sandbox")

    monkeypatch.setattr(svc, "ProvisionerSandboxBackend", UnexpectedSandboxBackend, raising=False)
    monkeypatch.setattr(
        svc,
        "get_user_skills_root_dir",
        lambda _uid: (_ for _ in ()).throw(AssertionError("Resume 流不应物化 Skill 投影根")),
        raising=False,
    )
    monkeypatch.setattr(svc, "flush_langfuse", lambda: None)

    async def on_prepared() -> None:
        assert db.commit_count == 1
        lifecycle.append("prepared")

    stream = stream_agent_resume(
        prepared_execution=prepared_execution(),
        thread_id="parent-thread",
        resume_input={"ok": True},
        meta={"turn_id": "turn-1", "run_id": "run-1", "worker_id": "worker-1"},
        current_user=SimpleNamespace(uid="user-1"),
        db=db,
        on_prepared=on_prepared,
    )

    chunks = []
    loading = None
    async for raw in stream:
        chunk = _chunk(raw)
        chunks.append(chunk)
        if chunk.get("status") == "loading":
            loading = chunk
        if chunk.get("status") == "finished":
            break
    await stream.aclose()

    assert loading is not None
    assert loading["thread_id"] == "child-thread"
    assert loading["response"] == "child token"
    assert loading["stream_event"]["thread_id"] == "child-thread"
    finished = chunks[-1]
    assert finished["status"] == "finished"
    assert finished["meta"]["agent_slug"] == "main-agent"
    assert "agent_id" not in finished["meta"]
    assert lifecycle == ["prepared", "streaming"]
    assert calls["stream_kwargs"] == {
        "callbacks": [],
        "metadata": {},
        "tags": [],
        "run_name": "主智能体",
    }

    async def fail_output_persistence(**_kwargs):
        raise ValueError("output binding rejected")

    db.commit_count = 0
    monkeypatch.setattr(svc, "save_messages_from_langgraph_state", fail_output_persistence)
    failing_chunks = []
    async for raw in stream_agent_resume(
        prepared_execution=prepared_execution(),
        thread_id="parent-thread",
        resume_input={"ok": True},
        meta={
            "run_id": "resume-output-error",
            "turn_id": "resume-turn-error",
            "worker_id": "resume-worker:attempt-1",
        },
        current_user=SimpleNamespace(uid="user-1"),
        db=db,
        on_prepared=on_prepared,
    ):
        failing_chunks.append(_chunk(raw))

    assert failing_chunks[-1]["status"] == "error"
    assert failing_chunks[-1]["error_type"] == "output_persistence_error"
    assert all(chunk.get("status") not in {"finished", "warning"} for chunk in failing_chunks)
