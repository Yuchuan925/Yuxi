from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain.messages import AIMessage, HumanMessage, ToolMessage

from yuxi.modules.agents.runtime import context as agent_context
from yuxi.modules.workspace import paths as workspace_paths
from test.unit.agent_context_fixtures import prepared_execution
import yuxi.modules.agents.services.execution as svc
import yuxi.modules.agents.services.messages as message_svc
import yuxi.modules.agents.services.state as state_svc
import yuxi.modules.agents.services.runs as lifecycle_runs


def _empty_agent_context(_uid: str) -> str:
    return ""


async def _fake_normalize_agent_context_config(context, **_kwargs):
    return dict(context or {})


async def _resolve_test_workdir(**_kwargs):
    """返回测试 Conversation 的 Project Workdir。"""

    return "projects/11111111-1111-4111-8111-111111111111"


def test_context_update_applies_only_declared_runtime_fields() -> None:
    context = agent_context.BaseContext()
    context.update(
        {
            "thread_id": "thread-1",
            "uid": "user-1",
            "system_prompt": "runtime prompt",
            "unknown_field": "ignored",
            "update": "must not shadow the method",
        },
    )

    assert context.thread_id == "thread-1"
    assert context.uid == "user-1"
    assert context.system_prompt == "runtime prompt"
    assert not hasattr(context, "unknown_field")
    assert callable(context.update)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        prepared_execution(backend_id="SubAgentBackend"),
        prepared_execution(backend_id="SubAgentBackend", system_prompt="manifest"),
    ],
)
async def test_resolve_agent_runtime_includes_subagents_only_when_requested(
    monkeypatch: pytest.MonkeyPatch, snapshot
) -> None:
    calls: list[str] = []

    class FakeAgentRepository:
        def __init__(self, _db):
            pass

        async def get_visible_by_slug(self, *, slug: str, user, kind="main"):
            del user
            assert slug == "worker"
            calls.append(kind)
            if kind == "subagent":
                return SimpleNamespace(slug="worker", backend_id="SubAgentBackend", config_json={"context": {}})
            return None

    class FakeConversationRepository:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, thread_id: str):
            return SimpleNamespace(
                uid="user-1",
                agent_id="worker",
                thread_id=thread_id,
                status="subagent",
                project_id="11111111-1111-4111-8111-111111111111",
            )

    monkeypatch.setattr(svc, "AgentRepository", FakeAgentRepository)
    monkeypatch.setattr(svc, "ConversationRepository", FakeConversationRepository)
    monkeypatch.setattr(svc, "resolve_conversation_workdir_path", _resolve_test_workdir)

    async def normalize(context, **kwargs):
        """有 manifest 快照时重复解析应使回归失败。"""
        pytest.fail("manifest 后不应重新解析配置")

    monkeypatch.setattr(agent_context, "normalize_agent_context_config", normalize)
    monkeypatch.setattr(
        svc,
        "get_agent_backend",
        lambda backend_id: SimpleNamespace(context_schema=None) if backend_id == "SubAgentBackend" else None,
    )

    user = SimpleNamespace(uid="user-1")

    with pytest.raises(ValueError, match="对话线程不存在"):
        await svc._resolve_agent_runtime(
            db=object(),
            user=user,
            requested_agent_slug="worker",
            thread_id="child-thread",
            prepared_execution=snapshot,
        )

    agent_item, backend, agent_config, conversation = await svc._resolve_agent_runtime(
        db=object(),
        user=user,
        requested_agent_slug="worker",
        thread_id="child-thread",
        agent_kind="subagent",
        prepared_execution=snapshot,
    )

    assert calls == ["subagent"]
    assert agent_item.slug == "worker"
    assert backend.context_schema is None
    assert agent_config is snapshot.context
    assert conversation.thread_id == "child-thread"


class _EmptyModelAuditRepo:
    def __init__(self, _db):
        pass

    async def list_for_run(self, _run_id: str):
        return []


class _EmptyToolAuditRepo:
    def __init__(self, _db):
        pass

    async def list_for_run(self, _run_id: str):
        return []


class _FakeDBBase:
    async def flush(self):
        """模拟仓储原语只 flush、顶层拥有提交。"""


class _FakeRunRepoBase:
    async def get_run(self, _run_id: str):
        """消息重建测试只关注输出归属，使用子执行免除根 Turn 调度。"""
        return SimpleNamespace(run_type="subagent")


class _FakeConvRepo:
    def __init__(self, _db):
        self.db = _db
        self.saved_messages: list[dict] = []
        self.tool_calls: list[dict] = []
        self.conversations: dict[str, SimpleNamespace] = {}
        self.source_ids: set[str] = set()
        self.published_message_ids: list[int] = []

    def _conversation(self, thread_id: str) -> SimpleNamespace:
        return self.conversations.setdefault(
            thread_id,
            SimpleNamespace(
                id=1,
                uid="user-1",
                app_id=None,
                agent_id="test-agent",
                thread_id=thread_id,
                status="active",
                extra_metadata={},
            ),
        )

    async def add_message_by_thread_id(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        message_type: str = "text",
        extra_metadata: dict | None = None,
        image_content: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        commit: bool = True,
    ):
        self.saved_messages.append(
            {
                "thread_id": thread_id,
                "role": role,
                "content": content,
                "message_type": message_type,
                "extra_metadata": extra_metadata,
                "image_content": image_content,
                "run_id": run_id,
                "turn_id": turn_id,
                "commit": commit,
            }
        )
        return SimpleNamespace(
            id=1,
            conversation_id=1,
            message_type=message_type,
            extra_metadata=extra_metadata or {},
        )

    async def get_conversation_by_thread_id(self, thread_id: str):
        return self._conversation(thread_id)

    async def lock_conversation_by_thread_id(self, thread_id: str):
        return self._conversation(thread_id)

    async def get_messages_by_thread_id(self, _thread_id: str):
        return []

    async def get_message_source_ids_by_thread_id(self, _thread_id: str):
        return set(self.source_ids)

    async def publish_assistant_output(self, message) -> None:
        message.message_type = "text"
        self.published_message_ids.append(message.id)

    async def add_tool_call(
        self,
        *,
        message_id: int,
        tool_name: str,
        tool_input: dict | None = None,
        status: str = "pending",
        langgraph_tool_call_id: str | None = None,
        commit: bool = True,
    ):
        self.tool_calls.append(
            {
                "message_id": message_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "status": status,
                "langgraph_tool_call_id": langgraph_tool_call_id,
                "commit": commit,
            }
        )
        return SimpleNamespace(id=len(self.tool_calls))

    async def create_conversation(self, *, uid: str, agent_id: str, thread_id: str, metadata: dict | None = None):
        conversation = SimpleNamespace(
            id=1,
            uid=uid,
            agent_id=agent_id,
            thread_id=thread_id,
            status="active",
            extra_metadata=metadata or {},
        )
        self.conversations[thread_id] = conversation
        return conversation

    async def get_attachments_by_turn_id(self, conversation_id: int, turn_id: str):
        return []

    async def bind_attachments_to_request(self, conversation_id: int, turn_id: str, file_ids: list[str]):
        return []


@pytest.mark.asyncio
async def test_error_output_and_failed_run_commit_together(monkeypatch):
    """异常输出必须等 Run 失败状态写入后才提交。"""
    steps = []

    class FakeDB(_FakeDBBase):
        async def commit(self):
            steps.append("commit")

        async def rollback(self):
            steps.append("rollback")

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-1", run_type="subagent")

        async def set_output_message(self, *_args, **_kwargs):
            steps.append("output")

        async def cancel_active_execution_tree_descendants(self, _run):
            return []

    async def settle_checkpoint(**kwargs):
        assert kwargs["status"] == "failed"
        steps.append("failed")
        return SimpleNamespace(changed=True)

    async def publish_cancel_signals(_ids):
        steps.append("published")

    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "settle_checkpoint", settle_checkpoint)
    monkeypatch.setattr(message_svc, "publish_cancel_signals", publish_cancel_signals)
    repo = _FakeConvRepo(FakeDB())
    message = await message_svc.save_partial_message(
        repo,
        "thread-1",
        run_id="run-1",
        turn_id="turn-1",
        worker_id="worker-1",
        error_type="stream_error",
    )
    assert message.id == 1
    assert repo.saved_messages[0]["commit"] is False
    assert steps == ["output", "failed", "commit", "published"]


@pytest.mark.asyncio
async def test_empty_final_checkpoint_cannot_complete_prior_error_output(monkeypatch):
    """当前执行无 AI 输出时，旧错误消息不能充当完成结果。"""

    class FakeDB(_FakeDBBase):
        committed = False
        rolled_back = False

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-1", run_type="subagent", output_message_id=99)

    async def forbidden_settlement(**_kwargs):
        pytest.fail("缺少当前 AI 输出时不能结算完成终态")

    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", _EmptyModelAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)
    monkeypatch.setattr(message_svc, "settle_checkpoint", forbidden_settlement)
    db = FakeDB()
    with pytest.raises(ValueError, match="缺少当前 Run 的 AI 输出"):
        await message_svc.save_messages_from_langgraph_state(
            state=SimpleNamespace(values={"messages": []}),
            thread_id="thread-1",
            conv_repo=_FakeConvRepo(db),
            run_id="run-1",
            turn_id="turn-1",
            worker_id="worker-1",
            complete_run=True,
        )
    assert db.rolled_back and not db.committed


@pytest.mark.asyncio
async def test_save_messages_from_langgraph_state_backfills_run_output_message(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDB(_FakeDBBase):
        def __init__(self):
            self.commit_count = 0

        async def commit(self):
            self.commit_count += 1

        async def rollback(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": [HumanMessage(content="question"), AIMessage(content="answer")]})

    fake_db = FakeDB()
    conv_repo = _FakeConvRepo(fake_db)
    captured: dict[str, object] = {}

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, db):
            assert db is fake_db

        async def lock_output_persistence(
            self,
            run_id: str,
            *,
            worker_id: str,
            conversation_thread_id: str,
        ):
            captured["locked"] = (run_id, worker_id, conversation_thread_id)
            return object()

        async def set_output_message(self, run_id: str, message_id: int, *, worker_id: str):
            captured["run_id"] = run_id
            captured["message_id"] = message_id
            captured["worker_id"] = worker_id
            return object()

    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", _EmptyModelAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=conv_repo,
        trace_info={"langfuse_trace_id": "trace-1"},
        run_id="run-1",
        turn_id="req-1",
        worker_id="worker-1",
    )

    assert conv_repo.saved_messages[0]["content"] == "answer"
    assert conv_repo.saved_messages[0]["run_id"] == "run-1"
    assert conv_repo.saved_messages[0]["turn_id"] == "req-1"
    assert conv_repo.saved_messages[0]["commit"] is False
    assert conv_repo.saved_messages[0]["extra_metadata"]["langfuse_trace_id"] == "trace-1"
    assert captured == {
        "locked": ("run-1", "worker-1", "thread-1"),
        "run_id": "run-1",
        "message_id": 1,
        "worker_id": "worker-1",
    }
    assert fake_db.commit_count == 1


@pytest.mark.asyncio
async def test_state_fallback_does_not_rebind_hidden_message_from_previous_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDB(_FakeDBBase):
        async def commit(self):
            pass

        async def rollback(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        AIMessage(content="old intermediate", id="old-model-audit"),
                        AIMessage(content="current answer", id="current-final"),
                    ]
                }
            )

    captured: dict[str, int] = {}

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return object()

        async def set_output_message(self, _run_id, message_id, *, worker_id):
            captured[worker_id] = message_id

    fake_db = FakeDB()
    conv_repo = _FakeConvRepo(fake_db)
    conv_repo.source_ids.add("old-model-audit")
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", _EmptyModelAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=conv_repo,
        run_id="run-current",
        turn_id="request-current",
        worker_id="worker-current",
    )

    assert [message["content"] for message in conv_repo.saved_messages] == ["current answer"]
    assert captured == {"worker-current": 1}


def test_root_tool_audit_event_rejects_unrouted_subagent_namespace() -> None:
    assert svc._is_root_tool_audit_event(
        {"method": "tools", "namespace": [], "data": {}},
        "root-thread",
    )
    assert not svc._is_root_tool_audit_event(
        {"method": "tools", "namespace": ["child:task"], "data": {}},
        "root-thread",
    )
    assert not svc._is_root_tool_audit_event(
        {
            "method": "tools",
            "namespace": ["child:task"],
            "thread_id": "child-thread",
            "data": {},
        },
        "root-thread",
    )


def test_tool_state_only_enriches_running_error_awaiting_terminal() -> None:
    running = SimpleNamespace(execution_status="running", extra_metadata={})
    awaiting_error = SimpleNamespace(
        execution_status="running",
        extra_metadata={"awaiting_run_terminal": True},
    )
    completed = SimpleNamespace(execution_status="completed", extra_metadata={})

    assert not message_svc._should_reconcile_tool_state(running, {"status": "success"})
    assert not message_svc._should_reconcile_tool_state(awaiting_error, {"status": "success"})
    assert message_svc._should_reconcile_tool_state(awaiting_error, {"status": "error"})
    assert not message_svc._should_reconcile_tool_state(completed, {"status": "success"})
    assert not message_svc._should_reconcile_tool_state(
        completed,
        {"status": "success", "content": "Tool result too large, saved in the filesystem"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("interrupt_run", "expected"), [(False, ["current error"]), (True, [])])
async def test_state_reconcile_uses_latest_error_unless_run_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_run: bool,
    expected: list[str],
) -> None:
    """只用最后一条错误补全审计，中断则保留 pending ToolCall 供恢复。"""

    class FakeDB(_FakeDBBase):
        async def commit(self):
            pass

        async def rollback(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        ToolMessage(content="old error", tool_call_id="shared-call", status="error"),
                        ToolMessage(content="current error", tool_call_id="shared-call", status="error"),
                    ]
                }
            )

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-current", run_type="subagent", conversation_id=1)

        async def set_terminal_status(self, *_args, **_kwargs):
            return SimpleNamespace(status="interrupted"), True

        async def cancel_active_execution_tree_descendants(self, _run):
            return []

    class FakeToolAuditRepo:
        def __init__(self, _db):
            pass

        async def list_for_run(self, _run_id):
            return [
                SimpleNamespace(
                    operation_id="shared-call",
                    execution_status="running",
                    extra_metadata={"awaiting_run_terminal": True},
                )
            ]

    reconciled: list[str] = []

    async def reconcile_tool(_conv_repo, **kwargs):
        reconciled.append(kwargs["msg_dict"]["content"])

    fake_db = FakeDB()
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", _EmptyModelAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", FakeToolAuditRepo)
    monkeypatch.setattr(message_svc, "_reconcile_tool_error_from_state", reconcile_tool)

    await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=_FakeConvRepo(fake_db),
        run_id="run-current",
        turn_id="request-current",
        worker_id="worker-current",
        interrupt_run=interrupt_run,
    )

    assert reconciled == expected


@pytest.mark.asyncio
async def test_model_state_reconcile_uses_latest_message_when_operation_id_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨 Run 复用 Model operation_id 时不得投影同线程历史 ToolCall。"""

    audit = SimpleNamespace(
        id=11,
        operation_id="shared-model",
        content="",
        extra_metadata={},
        execution_status="running",
        message_type="model_audit",
    )

    class FakeDB(_FakeDBBase):
        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def flush(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        AIMessage(
                            content="historical",
                            id="shared-model",
                            tool_calls=[{"id": "old-call", "name": "search", "args": {}}],
                        ),
                        AIMessage(content="current", id="shared-model"),
                    ]
                }
            )

    class FakeModelAuditRepo:
        def __init__(self, _db):
            pass

        async def list_for_run(self, _run_id):
            return [audit]

        async def get(self, *, run_id, operation_id):
            assert (run_id, operation_id) == ("run-current", "shared-model")
            return audit

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-1", run_type="subagent")

        async def set_output_message(self, *_args, **_kwargs):
            pass

    conv_repo = _FakeConvRepo(FakeDB())
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", FakeModelAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=conv_repo,
        run_id="run-current",
        turn_id="request-current",
        worker_id="worker-current",
    )

    assert audit.content == "current"
    assert audit.extra_metadata["tool_calls"] == []
    assert conv_repo.tool_calls == []


@pytest.mark.asyncio
async def test_completed_run_rejects_unmatched_final_state_message(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_message = SimpleNamespace(
        id=9,
        operation_id="known-intermediate",
        content="",
        extra_metadata={},
        execution_status="completed",
        message_type="model_audit",
        conversation_id=1,
    )

    class FakeDB(_FakeDBBase):
        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def flush(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        AIMessage(content="known", id="known-intermediate"),
                        AIMessage(content="unmatched final", id="missing-final"),
                    ]
                }
            )

    class FakeAuditRepo:
        def __init__(self, _db):
            pass

        async def list_for_run(self, _run_id):
            return [audit_message]

        async def get(self, *, run_id, operation_id):
            assert run_id == "run-1"
            return audit_message if operation_id == "known-intermediate" else None

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-1", run_type="subagent")

    fake_db = FakeDB()
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", FakeAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    with pytest.raises(ValueError, match="最终 State AIMessage"):
        await message_svc.save_messages_from_langgraph_state(
            state=await FakeGraph().aget_state({}),
            thread_id="thread-1",
            conv_repo=_FakeConvRepo(fake_db),
            run_id="run-1",
            turn_id="request-1",
            worker_id="worker-1",
            complete_run=True,
        )


@pytest.mark.asyncio
async def test_interrupted_run_does_not_bind_older_reconciled_model_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State 最后一条 AIMessage 未证明时不得绑定更早审计行。"""
    older_audit = SimpleNamespace(
        id=9,
        operation_id="known-older",
        content="",
        extra_metadata={},
        execution_status="completed",
        message_type="model_audit",
        conversation_id=1,
    )

    class FakeDB(_FakeDBBase):
        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def flush(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        AIMessage(content="older", id="known-older"),
                        AIMessage(content="unmatched interrupt", id="missing-current"),
                    ]
                }
            )

    class FakeAuditRepo:
        def __init__(self, _db):
            pass

        async def list_for_run(self, _run_id):
            return [older_audit]

        async def get(self, *, run_id, operation_id):
            assert run_id == "run-1"
            return older_audit if operation_id == "known-older" else None

    output_ids: list[int] = []

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-1", run_type="subagent")

        async def set_output_message(self, _run_id, message_id, *, worker_id):
            output_ids.append(message_id)

        async def set_terminal_status(self, *_args, **_kwargs):
            return SimpleNamespace(status="interrupted"), True

        async def cancel_active_execution_tree_descendants(self, _run):
            return []

    fake_db = FakeDB()
    conv_repo = _FakeConvRepo(fake_db)
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", FakeAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    committed = await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=conv_repo,
        run_id="run-1",
        turn_id="request-1",
        worker_id="worker-1",
        interrupt_run=True,
    )

    assert committed == "interrupted"
    assert output_ids == []
    assert conv_repo.published_message_ids == []


@pytest.mark.asyncio
async def test_tool_call_interrupt_ignores_historical_same_id_tool_message(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_message = SimpleNamespace(
        id=12,
        operation_id="tool-model",
        content="",
        extra_metadata={},
        execution_status="completed",
        message_type="model_audit",
        conversation_id=1,
    )

    class FakeDB(_FakeDBBase):
        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def flush(self):
            pass

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(
                values={
                    "messages": [
                        ToolMessage(content="historical output", tool_call_id="call-1"),
                        AIMessage(
                            content="",
                            id="tool-model",
                            tool_calls=[{"id": "call-1", "name": "search", "args": {}}],
                        ),
                    ]
                }
            )

    class FakeAuditRepo:
        def __init__(self, _db):
            pass

        async def list_for_run(self, _run_id):
            return [audit_message]

        async def get(self, *, run_id, operation_id):
            return audit_message

    output_ids: list[int] = []

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            return SimpleNamespace(id="run-1", run_type="subagent")

        async def set_output_message(self, _run_id, message_id, *, worker_id):
            output_ids.append(message_id)

        async def set_terminal_status(self, *_args, **_kwargs):
            return SimpleNamespace(status="interrupted"), True

        async def cancel_active_execution_tree_descendants(self, _run):
            return []

    fake_db = FakeDB()
    conv_repo = _FakeConvRepo(fake_db)
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", FakeAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    committed = await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=conv_repo,
        run_id="run-1",
        turn_id="request-1",
        worker_id="worker-1",
        interrupt_run=True,
    )

    assert committed == "interrupted"
    assert output_ids == [audit_message.id]
    assert audit_message.message_type == "model_audit"
    assert audit_message.extra_metadata["state_reconciled"] is True
    assert conv_repo.published_message_ids == []


@pytest.mark.asyncio
async def test_interrupt_persists_message_and_terminal_status_in_one_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple] = []

    class FakeDB(_FakeDBBase):
        async def commit(self):
            events.append(("commit",))

        async def rollback(self):
            events.append(("rollback",))

    class FakeGraph:
        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": [AIMessage(content="waiting")]})

    class FakeRunRepo(_FakeRunRepoBase):
        def __init__(self, _db):
            pass

        async def lock_output_persistence(self, *_args, **_kwargs):
            events.append(("lock",))
            return SimpleNamespace(id="run-1", run_type="subagent")

        async def set_output_message(self, run_id, message_id, *, worker_id):
            events.append(("message", run_id, message_id, worker_id))

        async def set_terminal_status(self, run_id, **kwargs):
            events.append(("terminal", run_id, kwargs))
            return SimpleNamespace(status="interrupted"), True

        async def cancel_active_execution_tree_descendants(self, _run):
            events.append(("descendants",))
            return []

    fake_db = FakeDB()
    monkeypatch.setattr(message_svc, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(lifecycle_runs, "AgentRunRepository", FakeRunRepo)
    monkeypatch.setattr(message_svc, "ModelMessageAuditRepository", _EmptyModelAuditRepo)
    monkeypatch.setattr(message_svc, "ToolMessageAuditRepository", _EmptyToolAuditRepo)

    terminal_committed = await message_svc.save_messages_from_langgraph_state(
        state=await FakeGraph().aget_state({}),
        thread_id="thread-1",
        conv_repo=_FakeConvRepo(fake_db),
        run_id="run-1",
        turn_id="request-1",
        worker_id="worker-1",
        interrupt_run=True,
        interrupt_error_type="ask_user_question_required",
        interrupt_error_message="请选择",
    )

    assert terminal_committed == "interrupted"
    assert [event[0] for event in events] == ["lock", "message", "terminal", "descendants", "commit"]
    assert events[-3][2] == {
        "status": "interrupted",
        "error_type": "ask_user_question_required",
        "error_message": "请选择",
        "token_usage": {"available": False},
        "worker_id": "worker-1",
    }


@pytest.mark.asyncio
async def test_workspace_prompt_excludes_memory_from_shared_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("YUXI_USER_DATA_DIR", str(tmp_path / "threads"))
    workspace_paths.ensure_user_workspace("user-1")
    agents_dir = tmp_path / "threads" / "shared" / "user-1" / "workspace" / "agents"
    (agents_dir / "AGENTS.md").write_text("行为约束", encoding="utf-8")
    (agents_dir / "USER.md").write_text("用户信息", encoding="utf-8")
    (agents_dir / "MEMORY.md").write_text("长期记忆", encoding="utf-8")

    context = agent_context.BaseContext(thread_id="thread-1", uid="user-1")
    context.update_config({})
    await agent_context._append_workspace_agent_prompt(context)

    assert context.system_prompt == (
        "You are a helpful assistant.\n\n"
        "用户工作区 agents/AGENTS.md 内容：\n行为约束\n\n用户工作区 agents/USER.md 内容：\n用户信息"
    )
    assert "长期记忆" not in context.system_prompt


@pytest.mark.asyncio
async def test_workspace_prompt_merges_workspace_agent_context(monkeypatch: pytest.MonkeyPatch):
    def fake_agent_context(_uid: str) -> str:
        return (
            "用户工作区 agents/AGENTS.md 内容：\n回答前先读取 AGENTS.md\n\n"
            "用户工作区 agents/USER.md 内容：\n用户偏好中文"
        )

    monkeypatch.setattr(agent_context, "_load_workspace_agent_context", fake_agent_context)

    context = agent_context.BaseContext(thread_id="thread-1", uid="user-1")
    context.update_config({"system_prompt": "原始系统提示词", "temperature": 0.1})
    await agent_context._append_workspace_agent_prompt(context)

    assert context.system_prompt == (
        "原始系统提示词\n\n"
        "用户工作区 agents/AGENTS.md 内容：\n回答前先读取 AGENTS.md\n\n"
        "用户工作区 agents/USER.md 内容：\n用户偏好中文"
    )
    assert context.thread_id == "thread-1"
    assert context.uid == "user-1"


@pytest.mark.asyncio
async def test_manifest_snapshot_prompt_keeps_workspace_agent_context(monkeypatch: pytest.MonkeyPatch):
    async def fake_to_thread(func, *args):
        del func, args
        return "用户工作区 agents/AGENTS.md 内容：\nWORKSPACE-MARKER"

    monkeypatch.setattr(agent_context.asyncio, "to_thread", fake_to_thread)
    config = {"system_prompt": "MANIFEST-CONFIG"}

    context = agent_context.BaseContext(thread_id="thread-1", uid="user-1")
    context.update_config(config)
    await agent_context._append_workspace_agent_prompt(context)

    assert context.system_prompt == "MANIFEST-CONFIG\n\n用户工作区 agents/AGENTS.md 内容：\nWORKSPACE-MARKER"
    assert context.thread_id == "thread-1"
    assert context.uid == "user-1"


@pytest.mark.asyncio
async def test_get_agent_state_view_rejects_async_subagent_without_child_conversation(
    monkeypatch: pytest.MonkeyPatch,
):
    child_thread_id = "missing-child-conversation"

    class ConvRepo:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, thread_id: str):
            del thread_id
            return None

    class RunRepo:
        def __init__(self, _db):
            pass

        async def get_latest_subagent_run_by_thread_for_user(self, thread_id: str, uid: str):
            assert thread_id == child_thread_id
            assert uid == "user-1"
            return SimpleNamespace(
                id="child-run",
                conversation_thread_id=child_thread_id,
                agent_slug="worker",
                status="running",
                created_by_run_id="parent-run",
                subagent_thread_relation_id=77,
                input_payload={"runtime": {"tool_call_id": "tool-1"}},
            )

        async def get_run_for_user(self, run_id: str, uid: str):
            del run_id, uid
            raise AssertionError("async subagent state must be loaded through child conversation relation")

    monkeypatch.setattr(state_svc, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(state_svc, "AgentRunRepository", RunRepo)

    with pytest.raises(HTTPException) as exc:
        await state_svc.get_agent_state_view(
            thread_id=child_thread_id,
            current_user=SimpleNamespace(uid="user-1"),
            db=object(),
            include_messages=True,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_agent_state_view_returns_interrupted_checkpoint_payload(monkeypatch: pytest.MonkeyPatch):
    thread_id = "thread-1"

    class ConvRepo:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, requested_thread_id: str):
            assert requested_thread_id == thread_id
            return SimpleNamespace(
                id=20,
                uid="user-1",
                app_id=None,
                agent_id="main",
                status="active",
                project_id="11111111-1111-4111-8111-111111111111",
                extra_metadata={"model_spec": "provider:conversation-model"},
            )

    class ThreadRepo:
        def __init__(self, _db):
            pass

        async def get_by_child_conversation_for_user(self, conversation_id: int, uid: str):
            assert conversation_id == 20
            assert uid == "user-1"
            return None

    class RunRepo:
        async def list_subagent_runs_for_conversation(self, conversation_id, uid):
            return []

        def __init__(self, _db):
            pass

        async def get_latest_run_by_thread_for_user(self, requested_thread_id: str, uid: str):
            assert requested_thread_id == thread_id
            assert uid == "user-1"
            return SimpleNamespace(
                id="run-1",
                status="interrupted",
                input_payload={"model_spec": "provider:stale-run-model"},
            )

    async def read_checkpoint_state(*, uid, thread_id):
        assert uid == "user-1"
        assert thread_id == "thread-1"
        return {}, SimpleNamespace(
            value={
                "action_requests": [{"name": "execute", "args": {"command": "pytest -q"}}],
                "review_configs": [{"action_name": "execute", "allowed_decisions": ["approve", "reject"]}],
            }
        )

    monkeypatch.setattr(state_svc, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(state_svc, "SubagentThreadRepository", ThreadRepo)
    monkeypatch.setattr(state_svc, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(state_svc, "_read_checkpoint_state", read_checkpoint_state)

    result = await state_svc.get_agent_state_view(
        thread_id=thread_id,
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert result["interrupt"]["status"] == "human_approval_required"
    assert result["interrupt"]["run_id"] == "run-1"
    assert result["interrupt"]["approval"]["action_requests"][0]["name"] == "execute"


@pytest.mark.asyncio
async def test_get_agent_state_view_reads_checkpoint_without_workspace_binding(monkeypatch: pytest.MonkeyPatch):
    class ConvRepo:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, thread_id: str):
            assert thread_id == "thread-1"
            return SimpleNamespace(
                id=20,
                uid="user-1",
                app_id=None,
                agent_id="main",
                status="active",
                project_id="missing-project",
            )

    class RunRepo:
        async def list_subagent_runs_for_conversation(self, conversation_id, uid):
            return []

        def __init__(self, _db):
            pass

        async def get_latest_run_by_thread_for_user(self, thread_id: str, uid: str):
            assert thread_id == "thread-1"
            assert uid == "user-1"
            return None

    class ThreadRepo:
        def __init__(self, _db):
            pass

        async def get_by_child_conversation_for_user(self, conversation_id, uid):
            return None

    async def read_checkpoint_state(*_args, **_kwargs):
        return {}, None

    monkeypatch.setattr(state_svc, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(state_svc, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(state_svc, "SubagentThreadRepository", ThreadRepo)
    monkeypatch.setattr(state_svc, "_read_checkpoint_state", read_checkpoint_state)

    result = await state_svc.get_agent_state_view(
        thread_id="thread-1",
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert result["agent_state"]["subagent_runs"] == []


@pytest.mark.asyncio
async def test_get_agent_state_view_includes_subagent_thread_relation(monkeypatch: pytest.MonkeyPatch):
    child_thread_id = "child-thread"

    class ConvRepo:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, thread_id: str):
            if thread_id == child_thread_id:
                return SimpleNamespace(
                    id=20,
                    uid="user-1",
                    app_id=None,
                    agent_id="worker",
                    status="subagent",
                    project_id="11111111-1111-4111-8111-111111111111",
                )
            return None

        async def get_conversation_by_id(self, conversation_id: int):
            assert conversation_id == 11
            return SimpleNamespace(id=11, thread_id="parent-thread", uid="user-1", app_id=None, status="active")

    class ThreadRepo:
        def __init__(self, _db):
            pass

        async def get_by_child_conversation_for_user(self, child_conversation_id: int, uid: str):
            assert child_conversation_id == 20
            assert uid == "user-1"
            return SimpleNamespace(
                id=77,
                parent_conversation_id=11,
                child_conversation_id=20,
                child_thread_id=child_thread_id,
                subagent_slug="worker",
                to_dict=lambda: {
                    "id": 77,
                    "parent_conversation_id": 11,
                    "child_conversation_id": 20,
                    "child_thread_id": child_thread_id,
                    "subagent_slug": "worker",
                },
            )

    class RunRepo:
        async def list_subagent_runs_for_conversation(self, conversation_id, uid):
            return []

        def __init__(self, _db):
            pass

        async def get_latest_run_by_thread_for_user(self, thread_id: str, uid: str):
            assert thread_id == child_thread_id
            assert uid == "user-1"
            return SimpleNamespace(
                status="running",
                runtime_scope_id="parent-thread",
                input_payload={"model_spec": "provider:run-model"},
            )

        async def get_latest_subagent_run_by_thread_for_user(self, thread_id: str, uid: str):
            assert thread_id == child_thread_id
            assert uid == "user-1"
            return SimpleNamespace(
                id="child-run",
                conversation_thread_id=child_thread_id,
                agent_slug="worker",
                uid="user-1",
                status="running",
                created_by_run_id="parent-run",
                subagent_thread_relation_id=77,
                input_payload={
                    "runtime": {
                        "tool_call_id": "tool-1",
                        "subagent_name": "Worker",
                        "description": "do work",
                    },
                },
                error_message=None,
                created_at=None,
                finished_at=None,
                to_dict=lambda: {"created_at": "2026-06-21T01:00:00Z", "finished_at": None},
            )

    async def read_checkpoint_state(*, uid, thread_id):
        assert thread_id == child_thread_id
        assert uid == "user-1"
        return {
            "messages": [HumanMessage(content="do work"), AIMessage(content="working")],
            "artifacts": ["out.txt"],
        }, None

    monkeypatch.setattr(state_svc, "_read_checkpoint_state", read_checkpoint_state)
    monkeypatch.setattr(state_svc, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(state_svc, "SubagentThreadRepository", ThreadRepo)
    monkeypatch.setattr(state_svc, "AgentRunRepository", RunRepo)

    result = await state_svc.get_agent_state_view(
        thread_id=child_thread_id,
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
        include_messages=True,
    )

    assert result["parent_thread_id"] == "parent-thread"
    assert result["subagent_thread"]["id"] == 77
    assert result["subagent_run"]["run_id"] == "child-run"
    assert result["agent_state"]["artifacts"] == ["out.txt"]
    assert [message["type"] for message in result["messages"]] == ["human", "ai"]


@pytest.mark.asyncio
async def test_get_agent_state_view_reports_malformed_subagent_run_as_server_error(
    monkeypatch: pytest.MonkeyPatch,
):
    child_thread_id = "child-thread"

    class ConvRepo:
        def __init__(self, _db):
            pass

        async def get_conversation_by_thread_id(self, thread_id: str):
            assert thread_id == child_thread_id
            return SimpleNamespace(
                id=20,
                uid="user-1",
                app_id=None,
                agent_id="worker",
                status="subagent",
                project_id="11111111-1111-4111-8111-111111111111",
            )

        async def get_conversation_by_id(self, conversation_id: int):
            assert conversation_id == 11
            return SimpleNamespace(id=11, thread_id="parent-thread", uid="user-1", app_id=None, status="active")

    class ThreadRepo:
        def __init__(self, _db):
            pass

        async def get_by_child_conversation_for_user(self, child_conversation_id: int, uid: str):
            assert child_conversation_id == 20
            assert uid == "user-1"
            return SimpleNamespace(
                id=77,
                parent_conversation_id=11,
                to_dict=lambda: {"id": 77},
            )

    class RunRepo:
        async def list_subagent_runs_for_conversation(self, conversation_id, uid):
            return []

        def __init__(self, _db):
            pass

        async def get_latest_run_by_thread_for_user(self, thread_id: str, uid: str):
            assert thread_id == child_thread_id
            assert uid == "user-1"
            return None

        async def get_latest_subagent_run_by_thread_for_user(self, thread_id: str, uid: str):
            assert thread_id == child_thread_id
            assert uid == "user-1"
            return SimpleNamespace(
                id="child-run",
                conversation_thread_id=child_thread_id,
                agent_slug="worker",
                status="running",
                input_payload={"runtime": {}},
            )

    async def read_checkpoint_state(*, uid, thread_id):
        return {}, None

    monkeypatch.setattr(state_svc, "_read_checkpoint_state", read_checkpoint_state)
    monkeypatch.setattr(state_svc, "ConversationRepository", ConvRepo)
    monkeypatch.setattr(state_svc, "SubagentThreadRepository", ThreadRepo)
    monkeypatch.setattr(state_svc, "AgentRunRepository", RunRepo)

    with pytest.raises(HTTPException) as exc:
        await state_svc.get_agent_state_view(
            thread_id=child_thread_id,
            current_user=SimpleNamespace(uid="user-1"),
            db=object(),
        )

    assert exc.value.status_code == 500
    assert exc.value.detail == "子智能体运行记录格式异常"


@pytest.mark.asyncio
async def test_workspace_prompt_keeps_prompt_when_workspace_agent_context_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(agent_context, "_load_workspace_agent_context", _empty_agent_context)

    context = agent_context.BaseContext(thread_id="thread-1", uid="user-1")
    context.update_config({"system_prompt": "原始系统提示词"})
    await agent_context._append_workspace_agent_prompt(context)

    assert context.system_prompt == "原始系统提示词"


@pytest.mark.parametrize(
    ("conversation", "requested_agent", "error"),
    [
        (None, "worker", "对话线程不存在"),
        (SimpleNamespace(uid="user-1", agent_id="worker", status="deleted"), "worker", "对话线程不存在"),
        (SimpleNamespace(uid="user-1", agent_id="worker", status="archived"), "worker", "对话线程不存在"),
        (SimpleNamespace(uid="other-user", agent_id="worker", status="active"), "worker", "对话线程不存在"),
        (SimpleNamespace(uid="user-1", agent_id="worker", status="active"), "other-agent", "不能切换"),
    ],
)
async def test_execution_requires_existing_authorized_conversation(monkeypatch, conversation, requested_agent, error):
    """缺失或越权线程不能在执行层补建，也不能借已有快照绕过绑定校验。"""
    from unittest.mock import AsyncMock

    repository = SimpleNamespace(get_conversation_by_thread_id=AsyncMock(return_value=conversation))
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: repository)
    with pytest.raises(ValueError, match=error):
        await svc._resolve_agent_runtime(
            db=object(),
            user=SimpleNamespace(uid="user-1"),
            requested_agent_slug=requested_agent,
            thread_id="thread-1",
            prepared_execution=prepared_execution(),
        )


async def test_execution_rejects_archived_subagent_thread(monkeypatch):
    """Project 归档后的子线程不能在 worker 中继续执行。"""
    from unittest.mock import AsyncMock

    repository = SimpleNamespace(
        get_conversation_by_thread_id=AsyncMock(
            return_value=SimpleNamespace(uid="user-1", agent_id="worker", status="archived")
        )
    )
    monkeypatch.setattr(svc, "ConversationRepository", lambda _db: repository)
    with pytest.raises(ValueError, match="对话线程不存在"):
        await svc._resolve_agent_runtime(
            db=object(),
            user=SimpleNamespace(uid="user-1"),
            requested_agent_slug="worker",
            thread_id="child-thread",
            agent_kind="subagent",
            prepared_execution=prepared_execution(backend_id="SubAgentBackend"),
        )


@pytest.mark.parametrize(
    ("snapshot", "error", "message"),
    [
        ({}, AttributeError, "context|backend_id"),
        (prepared_execution(backend_id="changed"), ValueError, "后端在执行准备后发生变化"),
    ],
)
async def test_execution_does_not_rebuild_missing_snapshot_context(monkeypatch, snapshot, error, message):
    """错误快照显式失败，不回退到数据库配置。"""
    from unittest.mock import AsyncMock

    conversation = SimpleNamespace(uid="user-1", agent_id="worker", status="active")
    agent = SimpleNamespace(slug="worker", backend_id="ChatbotAgent", config_json={"context": {"system_prompt": "new"}})
    monkeypatch.setattr(
        svc,
        "ConversationRepository",
        lambda _db: SimpleNamespace(get_conversation_by_thread_id=AsyncMock(return_value=conversation)),
    )
    monkeypatch.setattr(
        svc, "AgentRepository", lambda _db: SimpleNamespace(get_visible_by_slug=AsyncMock(return_value=agent))
    )
    monkeypatch.setattr(svc, "get_agent_backend", lambda _backend: object())
    monkeypatch.setattr(svc, "resolve_conversation_workdir_path", _resolve_test_workdir)
    monkeypatch.setattr(
        agent_context, "normalize_agent_context_config", AsyncMock(side_effect=AssertionError("不得重新解析配置"))
    )
    with pytest.raises(error, match=message):
        await svc._resolve_agent_runtime(
            db=object(),
            user=SimpleNamespace(uid="user-1"),
            requested_agent_slug="worker",
            thread_id="thread-1",
            prepared_execution=snapshot,
        )
