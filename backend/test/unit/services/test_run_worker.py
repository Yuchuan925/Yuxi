from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yuxi.modules.agents.services.runner as run_worker
from arq.worker import RetryJob
from yuxi.modules.system import options as config_options
import yuxi.modules.tasks.service as task_service
from yuxi.modules.agents.services.execution import RunExecutionResult


@pytest.fixture(autouse=True)
def api_key_derivation_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-that-is-at-least-32-chars")
    monkeypatch.setenv("API_KEY_DERIVATION_SECRET", "test-api-key-derivation-secret-32-chars")
    monkeypatch.setenv("SANDBOX_PROVISIONER_TOKEN", "test-sandbox-token-that-is-at-least-32-chars")


class _RaisingAsyncIter:
    def __init__(self, exc: Exception):
        self._exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc

    async def aclose(self):
        """模拟可关闭的执行流。"""


@pytest.mark.parametrize("cancellation", ["signal", "outer", "consumer_body"])
async def test_stream_cancellation_closes_execution_before_owner_cleanup(cancellation):
    """用户信号、基础设施取消及消费侧取消均不得留下旧执行副作用。"""
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects = []
    ctx = run_worker.RunContext(run_id="run-cancel", worker_id="owner")

    async def stream():
        """用独立屏障检测取消后仍继续执行的旧任务。"""
        try:
            if cancellation == "consumer_body":
                yield b"first"
            entered.set()
            await release.wait()
            effects.append("old execution continued")
            yield b"late"
        finally:
            closed.set()

    producer = stream()

    async def consume():
        """以 Worker 的相同 close 边界消费，退出代表允许外层释放 owner。"""
        async with run_worker.aclosing(run_worker._consume_stream_with_cancel(producer, ctx)) as chunks:
            async for _ in chunks:
                entered.set()
                await release.wait()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancellation == "signal":
            ctx.cancel_event.set()
        else:
            consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
        assert closed.is_set(), "释放 owner 时旧执行尚未关闭"
        release.set()
        await asyncio.sleep(0)
        assert effects == []
    finally:
        release.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.sleep(0)
        await producer.aclose()


async def test_cancel_waiter_is_reused_for_all_stream_chunks():
    """完整输出保持顺序，一个 Run 的取消等待器只启动一次。"""
    waiter_starts = 0

    class Context:
        """只提供消费器需要的取消协议。"""

        async def wait_cancelled(self):
            """持续等待，记录逻辑等待器的创建次数。"""
            nonlocal waiter_starts
            waiter_starts += 1
            await asyncio.Event().wait()

    async def stream():
        """生成连续事件。"""
        for index in range(50):
            yield index

    results = [row async for row in run_worker._consume_stream_with_cancel(stream(), Context())]
    assert results == list(range(50))
    assert waiter_starts == 1


async def test_repeated_cancel_waits_for_async_execution_cleanup():
    """二次取消不能越过异步收尾屏障而释放执行 owner。"""
    entered, cleanup_started, release_cleanup, closed = (asyncio.Event() for _ in range(4))
    ctx = run_worker.RunContext(run_id="repeat-cancel", worker_id="owner")

    async def stream():
        """用异步屏障模拟节点和执行流的清理。"""
        try:
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()

    async def consume():
        """外层退出是 owner 允许释放的时点。"""
        async with run_worker.aclosing(run_worker._consume_stream_with_cancel(stream(), ctx)) as chunks:
            async for _ in chunks:
                pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        consumer.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done(), "二次取消提前释放了仍在清理的 owner"
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
        assert closed.is_set(), "执行流的异步清理被二次取消打断"
    finally:
        release_cleanup.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


def test_durable_task_outer_timeout_tracks_configured_worker_default():
    durable_function = next(
        function
        for function in run_worker.WorkerSettings.functions
        if getattr(function, "name", getattr(function, "__name__", None)) == "process_task"
    )

    assert task_service.tasker.default_timeout_seconds == task_service.TASKER_DEFAULT_TIMEOUT_SECONDS
    assert durable_function.timeout_s == task_service.TASKER_DEFAULT_TIMEOUT_SECONDS + 30


def test_durable_task_shipping_worker_accepts_default_above_24_hours():
    env = os.environ.copy()
    env["TASKER_DEFAULT_TIMEOUT_SECONDS"] = "172800"
    script = """
from yuxi.workers.settings import WorkerSettings
from yuxi.modules.tasks.service import tasker

durable = next(
    function
    for function in WorkerSettings.functions
    if getattr(function, "name", getattr(function, "__name__", None)) == "process_task"
)
assert tasker.default_timeout_seconds == 172800
assert durable.timeout_s == 172830
"""

    completed = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr


class _ExecutionAsyncIter:
    async def aclose(self):
        """模拟真实 async generator 的显式收尾协议。"""

    def __init__(self, values: list[dict | RunExecutionResult]):
        self._values = list(values)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._values):
            raise StopAsyncIteration
        value = self._values[self._idx]
        self._idx += 1
        return value


def _terminal_result(status: str = "finished", **chunk) -> RunExecutionResult:
    """模拟执行器已读回 PostgreSQL checkpoint 并提交业务终态。"""
    return RunExecutionResult(
        checkpoint=SimpleNamespace(values={}),
        chunk={"status": status, "thread_id": "thread-1", "terminal_committed": True, **chunk},
    )


def _build_run() -> SimpleNamespace:
    return SimpleNamespace(
        id="run-1",
        status="pending",
        turn_id="turn-1",
        input_id=None,
        app_id=None,
        input_payload={"model_spec": "provider:model"},
        input_message_id=10,
        run_type="chat",
        agent_slug="ChatbotAgent",
        uid="user-1",
        conversation_id=7,
        conversation_thread_id="thread-1",
        runtime_scope_id="thread-1",
        runtime_cleanup_pending=False,
        created_by_run_id=None,
        subagent_thread_relation_id=None,
    )


@pytest.mark.asyncio
async def test_validate_run_workdir_binding_rejects_top_level_foreign_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    run = _build_run()
    run.runtime_scope_id = "other-thread"

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_resolve(**_kwargs):
        return SimpleNamespace(conversation_id=run.conversation_id)

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session)
    monkeypatch.setattr(run_worker, "resolve_authorized_workdir", fake_resolve)

    with pytest.raises(run_worker.NonRetryableRunError, match="Chat AgentRun"):
        await run_worker._validate_run_workdir_binding(run)


@pytest.mark.asyncio
async def test_validate_run_workdir_binding_requires_subagent_creator_tree(
    monkeypatch: pytest.MonkeyPatch,
):
    run = _build_run()
    run.run_type = "subagent"
    run.conversation_thread_id = "child-thread"
    run.runtime_scope_id = "root-thread"
    run.created_by_run_id = "creator-run"
    run.subagent_thread_relation_id = 3
    creator = SimpleNamespace(
        id="creator-run",
        app_id=None,
        run_type="chat",
        conversation_thread_id="root-thread",
        runtime_scope_id="root-thread",
        conversation_id=2,
        created_by_run_id=None,
        subagent_thread_relation_id=None,
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_resolve(**kwargs):
        if kwargs["thread_id"] == "child-thread":
            return SimpleNamespace(
                conversation_id=run.conversation_id,
                workdir_path="projects/shared",
                project_id="project-1",
            )
        assert kwargs["thread_id"] == "root-thread"
        return SimpleNamespace(
            conversation_id=creator.conversation_id,
            workdir_path="projects/shared",
            project_id="project-1",
        )

    class RunRepo:
        def __init__(self, _db):
            pass

        async def get_subagent_run_with_creator(self, **kwargs):
            assert kwargs == {
                "uid": "user-1",
                "created_by_run_id": "creator-run",
                "run_id": "run-1",
            }
            return creator, run

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session)
    monkeypatch.setattr(run_worker, "resolve_authorized_workdir", fake_resolve)
    monkeypatch.setattr(run_worker, "AgentRunRepository", RunRepo)

    binding = await run_worker._validate_run_workdir_binding(run)
    assert binding.conversation_id == run.conversation_id

    original_resolve = fake_resolve

    async def resolve_different_project(**kwargs):
        binding = await original_resolve(**kwargs)
        if kwargs["thread_id"] == "child-thread":
            binding.project_id = "project-2"
        return binding

    monkeypatch.setattr(run_worker, "resolve_authorized_workdir", resolve_different_project)
    with pytest.raises(run_worker.NonRetryableRunError, match="SubAgent Run"):
        await run_worker._validate_run_workdir_binding(run)

    monkeypatch.setattr(run_worker, "resolve_authorized_workdir", fake_resolve)
    creator.runtime_scope_id = "corrupted-root"
    with pytest.raises(run_worker.NonRetryableRunError, match="SubAgent Run"):
        await run_worker._validate_run_workdir_binding(run)


@pytest.mark.asyncio
async def test_cancelling_subagent_preserves_shared_runtime(monkeypatch: pytest.MonkeyPatch):
    run = _build_run()
    run.run_type = "subagent"
    release_runtime = AsyncMock()

    async def fake_noop(*args, **kwargs):
        del args, kwargs
        return None

    async def fake_tree_finished(*args, **kwargs):
        del args, kwargs
        return True

    async def fake_mark_terminal(*args, **kwargs):
        del args, kwargs
        return run_worker.TerminalTransition(status="cancelled", changed=True)

    monkeypatch.setattr(run_worker, "_flush_writer_best_effort", fake_noop)
    monkeypatch.setattr(run_worker, "_finish_execution_tree_children", fake_tree_finished)
    monkeypatch.setattr(run_worker, "_release_runtime_before_terminal_event", release_runtime)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_append_run_event_best_effort", fake_noop)
    monkeypatch.setattr(run_worker, "_append_end_event", fake_noop)

    transition = await run_worker._finish_user_cancel(
        run_id=run.id,
        thread_id=run.conversation_thread_id,
        current_user=None,
        worker_id="worker-1",
        writer=SimpleNamespace(),
        run=run,
    )

    assert transition == run_worker.TerminalTransition(status="cancelled", changed=True)
    release_runtime.assert_not_awaited()


def _patch_common(monkeypatch: pytest.MonkeyPatch, run_obj: SimpleNamespace):
    @asynccontextmanager
    async def fake_session_ctx():
        yield SimpleNamespace(commit=AsyncMock())

    async def fake_noop(*args, **kwargs):
        del args, kwargs
        return None

    async def fake_cleanup(*args, **kwargs):
        del args, kwargs
        return True

    async def fake_mark_run_running(*args, **kwargs):
        del args, kwargs
        return True

    async def fake_get_run(run_id: str):
        del run_id
        return run_obj

    async def fake_load_user(uid: str):
        del uid
        return SimpleNamespace(id=1, uid="user-1")

    async def fake_load_run_input_messages(run):
        assert run.input_message_id == 10
        return [SimpleNamespace(content="hello", image_content=None, extra_metadata={})]

    async def fake_get_agent_state_view(**kwargs):
        del kwargs
        return {"agent_state": None}

    async def fake_not_cancelled(self):
        del self
        return False

    async def fake_tree_finished(*args, **kwargs):
        del args, kwargs
        return True

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "_get_run", fake_get_run)
    monkeypatch.setattr(run_worker, "_load_user", fake_load_user)
    monkeypatch.setattr(run_worker, "_load_run_input_messages", fake_load_run_input_messages)
    monkeypatch.setattr(run_worker, "get_agent_state_view", fake_get_agent_state_view)
    monkeypatch.setattr(run_worker, "mark_run_running", fake_mark_run_running)
    monkeypatch.setattr(run_worker, "release_run_lease_for_retry", fake_mark_run_running)
    monkeypatch.setattr(run_worker, "dispatch_next_input", fake_noop)
    monkeypatch.setattr(run_worker, "finish_turn_observation_if_terminal", fake_noop)
    from test.unit.agent_context_fixtures import prepared_execution

    async def fake_prepare_execution(**kwargs):
        run = kwargs["run"]
        execution = prepared_execution(
            model=run.input_payload.get("model_spec"),
            runtime_scope_id=run.runtime_scope_id,
        )
        if run.run_type == "subagent":
            from dataclasses import asdict, replace
            from yuxi.modules.agents.runtime.builtin.subagent.context import SubAgentContext

            execution = replace(
                execution,
                context=SubAgentContext(
                    **asdict(execution.context),
                    parent_thread_id=run.input_payload["runtime"]["parent_thread_id"],
                ),
            )
        return execution

    monkeypatch.setattr(run_worker, "prepare_and_record_run_execution", fake_prepare_execution)
    monkeypatch.setattr(run_worker, "_record_run_timing_best_effort", fake_noop)
    monkeypatch.setattr(
        run_worker,
        "_validate_run_workdir_binding",
        AsyncMock(
            return_value=SimpleNamespace(
                workdir_path="projects/11111111-1111-4111-8111-111111111111",
                virtual_path="/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111",
            )
        ),
    )
    monkeypatch.setattr(run_worker, "_finish_execution_tree_children", fake_tree_finished)
    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fake_cleanup)
    monkeypatch.setattr(run_worker, "clear_cancel_signal", fake_noop)
    monkeypatch.setattr(run_worker, "stream_agent_chat", lambda **kwargs: object())
    monkeypatch.setattr(run_worker.RunContext, "start", fake_noop)
    monkeypatch.setattr(run_worker.RunContext, "close", fake_noop)
    monkeypatch.setattr(run_worker.RunContext, "is_cancelled", fake_not_cancelled)
    monkeypatch.setattr(
        run_worker,
        "get_sandbox_provider",
        lambda: SimpleNamespace(release=lambda *_args, **_kwargs: None),
    )


@pytest.mark.asyncio
async def test_process_agent_run_rejects_corrupted_runtime_scope_before_execution(
    monkeypatch: pytest.MonkeyPatch,
):
    run_obj = _build_run()
    run_obj.runtime_scope_id = "other-root-thread"
    _patch_common(monkeypatch, run_obj)
    terminal_calls: list[dict] = []
    stream_called = False

    async def reject_binding(_run):
        raise run_worker.NonRetryableRunError("AgentRun 的 runtime scope 与 Project Workdir 绑定不一致")

    async def fake_mark_terminal(run_id: str, status: str, *args, **kwargs):
        terminal_calls.append({"run_id": run_id, "status": status, "args": args, **kwargs})
        return run_worker.TerminalTransition(status=status, changed=True)

    def fake_stream_agent_chat(**_kwargs):
        nonlocal stream_called
        stream_called = True
        return _ExecutionAsyncIter([])

    monkeypatch.setattr(run_worker, "_validate_run_workdir_binding", reject_binding)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, run_obj.id)

    assert stream_called is False
    assert terminal_calls[0]["status"] == "failed"
    assert terminal_calls[0]["args"][0] == "invalid_runtime_scope"


@pytest.mark.asyncio
async def test_process_agent_run_keeps_source_without_legacy_invocation_metadata(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    captured: dict[str, object] = {}
    events: list[dict] = []
    terminal_statuses: list[str] = []

    async def fake_load_run_input_messages(run):
        assert run.input_message_id == 10
        return [
            SimpleNamespace(
                content="hello",
                image_content=None,
                extra_metadata={
                    "source": "agent_call",
                    "agent_invocation_meta": {"trace_id": "trace-1"},
                    "evaluation": {"dataset_name": "legacy-top-level"},
                    "custom_variables": {"system_prompt": "legacy"},
                },
            )
        ]

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del kwargs
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload})

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, kwargs
        terminal_statuses.append(status)
        return run_worker.TerminalTransition(status=status, changed=True)

    def fake_stream_agent_chat(**kwargs):
        captured.update(kwargs)
        run_obj.status = "completed"
        return _ExecutionAsyncIter([_terminal_result()])

    monkeypatch.setattr(run_worker, "_load_run_input_messages", fake_load_run_input_messages)
    monkeypatch.setattr(run_worker, "_append_run_event_best_effort", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    meta = captured["meta"]
    assert meta["source"] == "agent_call"
    assert "agent_invocation_meta" not in meta
    assert "evaluation" not in meta
    assert "custom_variables" not in meta
    metadata_event = next(event for event in events if event["event_type"] == "metadata")
    assert metadata_event["payload"]["source"] == "agent_call"
    assert "agent_invocation_meta" not in metadata_event["payload"]
    assert "evaluation" not in metadata_event["payload"]
    assert "custom_variables" not in metadata_event["payload"]
    assert terminal_statuses == []


@pytest.mark.asyncio
async def test_terminal_event_is_published_after_runtime_cleanup(monkeypatch: pytest.MonkeyPatch):
    """客户端看到 end 时，旧 runtime 必须已经释放，避免实时文件请求撞上删除。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    lifecycle: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, payload, kwargs
        if event_type == "end":
            lifecycle.append("end")

    async def fake_release_runtime(run):
        assert run is run_obj
        lifecycle.append("release")
        return True

    def fake_stream_agent_chat(**kwargs):
        del kwargs
        run_obj.status = "completed"
        return _ExecutionAsyncIter([_terminal_result()])

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fake_release_runtime)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert lifecycle[:2] == ["release", "end"]


async def test_next_fifo_input_dispatches_before_optional_turn_trace(monkeypatch: pytest.MonkeyPatch):
    """Langfuse 根导出等待时，已完成 Run 仍先领取下一条持久输入。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    order = []

    async def stream():
        """模拟已在输出事务中完成的当前 Run。"""
        run_obj.status = "completed"
        yield _terminal_result()

    async def dispatch(**_kwargs):
        order.append("dispatch")

    async def trace(_turn_id):
        order.append("trace")

    monkeypatch.setattr(run_worker, "stream_agent_chat", lambda **_kwargs: stream())
    monkeypatch.setattr(run_worker, "dispatch_next_input", dispatch)
    monkeypatch.setattr(run_worker, "finish_turn_observation_if_terminal", trace)

    await run_worker.process_agent_run({"job_try": 1}, run_obj.id)

    assert order == ["dispatch", "trace"]


@pytest.mark.asyncio
async def test_terminal_cleanup_failure_keeps_end_event_unpublished(monkeypatch: pytest.MonkeyPatch):
    """cleanup 失败必须保留 durable fence，不能先向客户端宣告 execution tree 已结束。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    end_event = AsyncMock()

    async def fail_cleanup(_run):
        raise RuntimeError("provisioner delete failed")

    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fail_cleanup)
    monkeypatch.setattr(run_worker, "_append_end_event", end_event)

    def committed_stream(**_kwargs):
        run_obj.status = "completed"
        return _ExecutionAsyncIter([_terminal_result()])

    monkeypatch.setattr(run_worker, "stream_agent_chat", committed_stream)

    with pytest.raises(run_worker.RuntimeCleanupPendingError):
        await run_worker.process_agent_run({"job_try": 1}, "run-1")

    end_event.assert_not_awaited()


@pytest.mark.parametrize(
    "events",
    [
        [{"status": "finished", "thread_id": "thread-1", "terminal_committed": True}],
        [RunExecutionResult(checkpoint=None, chunk={"status": "finished", "terminal_committed": True})],
        [],
    ],
)
async def test_worker_rejects_success_without_final_checkpoint(monkeypatch: pytest.MonkeyPatch, events):
    """字典终态、空 checkpoint 或流提前耗尽都不能完成 Run。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    terminals = []

    async def mark_terminal(run_id, status, **kwargs):
        terminals.append((run_id, status, kwargs.get("error_message")))
        return run_worker.TerminalTransition(status=status, changed=True)

    monkeypatch.setattr(run_worker, "mark_run_terminal", mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", lambda **_kwargs: _ExecutionAsyncIter(events))

    await run_worker.process_agent_run({"job_try": 1}, run_obj.id)

    assert terminals and terminals[0][1] == "failed"
    assert "checkpoint" in terminals[0][2]


async def test_worker_rejects_waitpoint_without_postgres_terminal(monkeypatch: pytest.MonkeyPatch):
    """checkpoint 中断事件不能代替 PostgreSQL 的等待终态。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    terminals = []

    async def mark_terminal(run_id, status, **kwargs):
        terminals.append((run_id, status, kwargs.get("error_message")))
        return run_worker.TerminalTransition(status=status, changed=True)

    wait_result = RunExecutionResult(
        checkpoint=SimpleNamespace(values={}),
        chunk={"status": "ask_user_question_required", "thread_id": "thread-1", "questions": []},
    )
    monkeypatch.setattr(run_worker, "mark_run_terminal", mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", lambda **_kwargs: _ExecutionAsyncIter([wait_result]))

    await run_worker.process_agent_run({"job_try": 1}, run_obj.id)

    assert terminals and terminals[0][1] == "failed"
    assert "PostgreSQL interrupted" in terminals[0][2]


@pytest.mark.asyncio
async def test_cleanup_reconciler_keeps_pending_run_for_dispatch_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    """周期 cleanup 只释放旧 runtime，pending Run 留给独立投递补偿。"""
    run_obj = _build_run()
    run_obj.status = "pending"
    run_obj.runtime_cleanup_pending = True

    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    class Repo:
        def __init__(self, _db):
            pass

        async def list_pending_runtime_cleanups(self):
            return [run_obj]

    cleanup = AsyncMock(return_value=True)
    dispatch = AsyncMock(return_value=run_obj.id)
    append_end = AsyncMock()
    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "AgentRunRepository", Repo)
    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", cleanup)
    monkeypatch.setattr(run_worker, "dispatch_next_input", dispatch)
    monkeypatch.setattr(run_worker, "_append_end_event", append_end)
    import yuxi.modules.agents.services.turns as turns

    monkeypatch.setattr(turns, "reconcile_cancelling_turns", AsyncMock())

    cleaned = await run_worker.reconcile_pending_runtime_cleanups()

    assert cleaned == [run_obj.id]
    dispatch.assert_not_awaited()
    append_end.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_agent_run_does_not_settle_usage_from_transient_deltas(
    monkeypatch: pytest.MonkeyPatch,
):
    """worker 只投影增量；当前 Run 的用量由执行器业务事务收敛。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    terminal_calls = AsyncMock(side_effect=AssertionError("worker must not settle completed result"))

    async def fake_append_event(*args, **kwargs):
        del args, kwargs

    def fake_stream_agent_chat(**kwargs):
        del kwargs
        run_obj.status = "completed"
        return _ExecutionAsyncIter(
            [
                {
                    "status": "agent_state",
                    "thread_id": "thread-1",
                    "agent_state": {
                        "token_usage": {"current_run_id": "other-run", "run": {"total": {"total_tokens": 505}}}
                    },
                },
                {
                    "status": "agent_state",
                    "thread_id": "child-thread",
                    "agent_state": {
                        "token_usage": {"current_run_id": "run-1", "run": {"total": {"total_tokens": 1000}}}
                    },
                },
                _terminal_result(),
            ]
        )

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", terminal_calls)
    monkeypatch.setattr(
        run_worker, "get_agent_state_view", AsyncMock(side_effect=AssertionError("worker must not reread usage"))
    )
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    terminal_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_run_token_usage_from_state_rejects_other_run(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    async def fake_get_agent_state_view(**kwargs):
        del kwargs
        return {
            "agent_state": {
                "token_usage": {
                    "current_run_id": "old-run",
                    "run": {"schema_version": 2, "models": {}, "total": {"total_tokens": 99}},
                }
            }
        }

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "get_agent_state_view", fake_get_agent_state_view)

    usage = await run_worker._read_run_token_usage_from_state(
        run_id="run-1",
        thread_id="thread-1",
        current_user=SimpleNamespace(uid="user-1"),
    )

    assert usage is None


@pytest.mark.asyncio
async def test_finish_run_marks_usage_unavailable_when_state_read_fails(monkeypatch: pytest.MonkeyPatch):
    terminal_calls: list[dict] = []

    async def fake_read_usage(**kwargs):
        del kwargs
        return None

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        terminal_calls.append({"run_id": run_id, "status": status, **kwargs})
        return run_worker.TerminalTransition(status=status, changed=True)

    async def fake_append_end_event(*args, **kwargs):
        del args, kwargs

    async def fake_get_run(_run_id: str):
        return None

    monkeypatch.setattr(run_worker, "_read_run_token_usage_from_state", fake_read_usage)
    monkeypatch.setattr(run_worker, "_get_run", fake_get_run)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_append_end_event", fake_append_end_event)

    await run_worker._finish_run(
        "run-1",
        "completed",
        thread_id="thread-1",
        chunk={"status": "finished"},
        current_user=SimpleNamespace(uid="user-1"),
        worker_id="worker-1:attempt-1",
    )

    assert terminal_calls[0]["token_usage"] == {"available": False}


@pytest.mark.asyncio
async def test_process_agent_run_publishes_interrupt_after_final_state(monkeypatch: pytest.MonkeyPatch):
    """审批中断必须在最终状态落流后结束，避免前端过早刷新历史。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    events: list[dict] = []
    terminal_statuses: list[str] = []
    lifecycle: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append({"event_type": event_type, "payload": payload})
        if event_type in {"interrupt", "end"}:
            lifecycle.append(event_type)

    async def fake_release_runtime(run):
        assert run is run_obj
        lifecycle.append("release")
        return True

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, kwargs
        terminal_statuses.append(status)
        # 执行器已在 Message/Run/Turn 的 owning transaction 内提交 interrupted。
        return run_worker.TerminalTransition(status=status, changed=False)

    def fake_stream_agent_chat(**kwargs):
        del kwargs
        run_obj.status = "interrupted"
        return _ExecutionAsyncIter(
            [
                {
                    "status": "agent_state",
                    "thread_id": "thread-1",
                    "agent_state": {"artifacts": ["/home/gem/user-data/outputs/app.py"]},
                },
                RunExecutionResult(
                    checkpoint=SimpleNamespace(values={}),
                    chunk={
                        "status": "human_approval_required",
                        "thread_id": "thread-1",
                        "approval": {
                            "action_requests": [{"name": "execute", "args": {"command": "python app.py"}}],
                            "review_configs": [{"action_name": "execute", "allowed_decisions": ["approve", "reject"]}],
                        },
                    },
                ),
            ]
        )

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fake_release_runtime)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert [event["event_type"] for event in events] == ["metadata", "custom", "interrupt", "end"]
    assert terminal_statuses == []
    assert lifecycle == ["release", "interrupt", "end"]


@pytest.mark.asyncio
async def test_committed_interrupt_cleanup_failure_keeps_terminal_events_unpublished(
    monkeypatch: pytest.MonkeyPatch,
):
    """Chat 已提交 interrupted 时，runtime cleanup 失败仍不能发布 interrupt/end。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    events: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, payload, kwargs
        events.append(event_type)

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, kwargs
        return run_worker.TerminalTransition(status=status, changed=False)

    async def fail_cleanup(_run):
        raise RuntimeError("provisioner delete failed")

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fail_cleanup)
    monkeypatch.setattr(
        run_worker,
        "stream_agent_chat",
        lambda **_kwargs: _ExecutionAsyncIter(
            [{"status": "interrupted", "thread_id": "thread-1", "message": "input required"}]
        ),
    )

    with pytest.raises(run_worker.RuntimeCleanupPendingError):
        await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert events == ["metadata"]


@pytest.mark.asyncio
async def test_process_agent_run_non_retryable_error_marks_failed(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    terminal_statuses: list[str] = []
    events: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, payload, kwargs
        events.append(event_type)

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, kwargs
        terminal_statuses.append(status)
        return run_worker.TerminalTransition(status=status, changed=True)

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(
        run_worker,
        "_consume_stream_with_cancel",
        lambda stream, run_ctx: _RaisingAsyncIter(RuntimeError("boom")),
    )

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert "error" in events
    assert terminal_statuses == ["failed"]


@pytest.mark.asyncio
async def test_preflight_failure_after_lease_closes_context_and_writes_owned_terminal(
    monkeypatch: pytest.MonkeyPatch,
):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    closed: list[str] = []
    terminal_calls: list[dict] = []

    async def fail_input_load(_message_id):
        raise RuntimeError("preflight failed")

    async def fake_close(context):
        closed.append(context.worker_id)

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        terminal_calls.append({"run_id": run_id, "status": status, **kwargs})
        return run_worker.TerminalTransition(status=status, changed=True)

    monkeypatch.setattr(run_worker, "_load_run_input_messages", fail_input_load)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker.RunContext, "close", fake_close)

    await run_worker.process_agent_run({"worker_id": "worker-preflight", "job_try": 1}, "run-1")

    assert terminal_calls[0]["status"] == "failed"
    assert terminal_calls[0]["error_type"] == "worker_error"
    assert terminal_calls[0]["worker_id"].startswith("worker-preflight:")
    assert closed == [terminal_calls[0]["worker_id"]]


@pytest.mark.asyncio
async def test_redis_event_failure_cannot_block_owned_completed_terminal(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    terminal_calls: list[dict] = []
    closed: list[str] = []

    async def fail_event(*args, **kwargs):
        del args, kwargs
        raise ConnectionError("redis unavailable")

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        terminal_calls.append({"run_id": run_id, "status": status, **kwargs})
        return run_worker.TerminalTransition(status=status, changed=True)

    async def fake_close(context):
        closed.append(context.worker_id)

    def fake_stream_agent_chat(**kwargs):
        del kwargs
        run_obj.status = "completed"
        return _ExecutionAsyncIter([_terminal_result()])

    monkeypatch.setattr(run_worker, "append_run_event", fail_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "release_run_lease_for_retry", AsyncMock(side_effect=AssertionError("no retry")))
    monkeypatch.setattr(run_worker.RunContext, "close", fake_close)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"worker_id": "worker-events", "job_try": 1}, "run-1")

    assert terminal_calls == []
    assert len(closed) == 1 and closed[0].startswith("worker-events:")


@pytest.mark.asyncio
async def test_durable_cancel_without_redis_signal_never_enters_agent_stream(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    run_obj.status = "cancel_requested"
    _patch_common(monkeypatch, run_obj)
    terminal_calls: list[dict] = []
    lifecycle: list[str] = []

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        lifecycle.append("terminal")
        terminal_calls.append({"run_id": run_id, "status": status, **kwargs})
        run_obj.status = status
        return run_worker.TerminalTransition(status=status, changed=True)

    def forbidden_stream(**kwargs):
        del kwargs
        raise AssertionError("durably cancelled run must not execute")

    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", forbidden_stream)

    async def fake_release_sandbox(_run):
        lifecycle.append("release")
        return True

    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fake_release_sandbox)

    await run_worker.process_agent_run({"worker_id": "worker-cancel", "job_try": 1}, "run-1")

    assert [call["status"] for call in terminal_calls] == ["cancelled"]
    assert terminal_calls[0]["worker_id"].startswith("worker-cancel:")
    assert lifecycle == ["terminal", "release"]


@pytest.mark.asyncio
async def test_infrastructure_cancel_releases_pending_and_propagates(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    released: list[tuple[str, str]] = []
    closed: list[str] = []

    async def release(run_id: str, worker_id: str) -> bool:
        released.append((run_id, worker_id))
        return True

    async def fake_close(context):
        closed.append(context.worker_id)

    monkeypatch.setattr(run_worker, "append_run_event", AsyncMock())
    monkeypatch.setattr(run_worker, "mark_run_terminal", AsyncMock(side_effect=AssertionError("not user cancel")))
    monkeypatch.setattr(run_worker, "release_run_lease_for_retry", release)
    monkeypatch.setattr(run_worker.RunContext, "close", fake_close)
    monkeypatch.setattr(
        run_worker,
        "_consume_stream_with_cancel",
        lambda stream, context: _RaisingAsyncIter(asyncio.CancelledError("worker shutdown")),
    )

    with pytest.raises(asyncio.CancelledError, match="worker shutdown"):
        await run_worker.process_agent_run({"worker_id": "worker-shutdown", "job_try": 1}, "run-1")

    assert len(released) == 1
    assert released[0][0] == "run-1"
    assert released[0][1].startswith("worker-shutdown:")
    assert closed == [released[0][1]]


@pytest.mark.asyncio
async def test_release_failure_does_not_mask_infrastructure_cancel(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    async def fail_release(_run_id: str, _worker_id: str) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(run_worker, "append_run_event", AsyncMock())
    monkeypatch.setattr(run_worker, "release_run_lease_for_retry", fail_release)
    monkeypatch.setattr(
        run_worker,
        "_consume_stream_with_cancel",
        lambda stream, context: _RaisingAsyncIter(asyncio.CancelledError("worker shutdown")),
    )

    with pytest.raises(asyncio.CancelledError, match="worker shutdown"):
        await run_worker.process_agent_run({"worker_id": "worker-shutdown", "job_try": 1}, "run-1")


@pytest.mark.asyncio
async def test_run_context_stream_checks_only_local_cancel_event(
    monkeypatch: pytest.MonkeyPatch,
):
    """模型事件循环不得把取消检查放大为逐事件 PostgreSQL 查询。"""
    run_context = run_worker.RunContext(run_id="run-1", worker_id="worker-1:attempt-1")
    durable_read = AsyncMock(side_effect=AssertionError("stream check must not query PostgreSQL"))
    monkeypatch.setattr(run_worker, "_is_cancel_requested", durable_read)

    assert await run_context.is_cancelled() is False
    run_context.cancel_event.set()
    assert await run_context.is_cancelled() is True
    durable_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_cancel_watcher_stops_execution_when_postgres_fact_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
):
    run_context = run_worker.RunContext(run_id="run-1", worker_id="worker-1:attempt-1")
    monkeypatch.setattr(run_worker, "_is_cancel_requested", AsyncMock(side_effect=RuntimeError("db unavailable")))

    await run_context._watch_durable_cancel()

    assert run_context.cancel_event.is_set()


@pytest.mark.asyncio
async def test_process_agent_run_retryable_error_retries_then_completes(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    terminal_statuses: list[str] = []
    events: list[dict] = []
    lifecycle: list[str] = []
    attempts = {"count": 0}

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append({"event_type": event_type, "payload": payload})

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, kwargs
        terminal_statuses.append(status)
        return run_worker.TerminalTransition(status=status, changed=True)

    def fake_consume(stream, run_ctx):
        del stream, run_ctx
        attempts["count"] += 1
        if attempts["count"] == 1:
            return _RaisingAsyncIter(run_worker.RetryableRunError("temporary failure"))
        run_obj.status = "completed"
        return _ExecutionAsyncIter([_terminal_result()])

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_consume_stream_with_cancel", fake_consume)

    async def fake_release_lease(*_args, **_kwargs):
        lifecycle.append("lease-and-descendants")
        return True

    async def fake_release_runtime(*_args, **_kwargs):
        lifecycle.append("runtime")
        return True

    monkeypatch.setattr(run_worker, "release_run_lease_for_retry", fake_release_lease)
    monkeypatch.setattr(run_worker, "_release_runtime_if_idle", fake_release_runtime)

    with pytest.raises(run_worker.RetryableRunError) as retry:
        await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert isinstance(retry.value, RetryJob)
    assert terminal_statuses == []
    assert lifecycle == ["lease-and-descendants", "runtime"]
    assert any(
        item["event_type"] == "error" and item["payload"]["chunk"].get("error_type") == "retryable_worker_error"
        for item in events
    )

    await run_worker.process_agent_run({"job_try": 2}, "run-1")
    assert terminal_statuses == []


@pytest.mark.asyncio
async def test_finish_run_terminal_loser_does_not_append_end_event(monkeypatch: pytest.MonkeyPatch):
    events: list[tuple[str, dict]] = []

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, status, kwargs
        return run_worker.TerminalTransition(status="cancelled", changed=False)

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append((event_type, payload))

    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "_get_run", AsyncMock(return_value=None))

    transition = await run_worker._finish_run(
        "run-1",
        "completed",
        thread_id="thread-1",
        chunk={"status": "finished"},
        current_user=SimpleNamespace(uid="user-1"),
        worker_id="worker-1:attempt-1",
    )

    assert transition == run_worker.TerminalTransition(status="cancelled", changed=False)
    assert events == []


@pytest.mark.asyncio
async def test_process_subagent_run_restores_runtime_context(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    run_obj.run_type = "subagent"
    run_obj.agent_slug = "worker"
    run_obj.conversation_thread_id = "child-thread"
    run_obj.runtime_scope_id = "parent-thread"
    run_obj.created_by_run_id = "parent-run"
    run_obj.input_payload = {
        "model_spec": "provider:model",
        "runtime": {
            "parent_thread_id": "parent-thread",
        },
    }
    _patch_common(monkeypatch, run_obj)

    from test.unit.agent_context_fixtures import prepared_execution
    from yuxi.modules.agents.runtime.builtin.subagent.context import SubAgentContext
    from dataclasses import asdict, replace

    prepared = prepared_execution(
        model="prepared:model",
        tool_approval_mode="always_trust",
        runtime_scope_id="prepared-root",
        workdir_relative_path="projects/prepared",
        workdir_path="/home/gem/user-data/projects/prepared",
    )
    prepared = replace(
        prepared, context=SubAgentContext(**asdict(prepared.context), parent_thread_id="prepared-parent")
    )
    monkeypatch.setattr(run_worker, "prepare_and_record_run_execution", AsyncMock(return_value=prepared))
    captured: dict[str, object] = {}
    terminal_statuses: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, event_type, payload, kwargs

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        del run_id, kwargs
        terminal_statuses.append(status)
        return run_worker.TerminalTransition(status=status, changed=True)

    def fake_stream_agent_chat(**kwargs):
        captured.update(kwargs)
        run_obj.status = "completed"
        return _ExecutionAsyncIter([_terminal_result(thread_id="child-thread")])

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    meta = captured["meta"]
    assert meta["run_type"] == "subagent"
    assert meta["parent_thread_id"] == "prepared-parent"
    assert meta["runtime_scope_id"] == "prepared-root"
    assert meta["workdir_relative_path"] == "projects/prepared"
    assert meta["workdir_path"] == "/home/gem/user-data/projects/prepared"
    assert meta["model_spec"] == "prepared:model"
    assert meta["tool_approval_mode"] == "always_trust"
    assert captured["agent_slug"] == "worker"
    assert captured["thread_id"] == "child-thread"
    assert captured["input_messages"][0].content == "hello"
    assert captured["input_messages"][0].langchain_message.content == "hello"
    assert "image_content" not in captured
    assert terminal_statuses == []


@pytest.mark.asyncio
async def test_process_agent_run_rejects_unknown_run_type(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    run_obj.run_type = "unknown"
    _patch_common(monkeypatch, run_obj)

    terminal_errors: list[dict] = []

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, **kwargs):
        terminal_errors.append(
            {
                "run_id": run_id,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
            }
        )
        return run_worker.TerminalTransition(status=status, changed=True)

    def fail_stream_agent_chat(**kwargs):
        del kwargs
        raise AssertionError("unknown run_type must not enter chat stream")

    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fail_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert terminal_errors == [
        {
            "run_id": "run-1",
            "status": "failed",
            "error_type": "invalid_run_type",
            "error_message": "不支持的 run_type: unknown",
        }
    ]


@pytest.mark.asyncio
async def test_process_agent_run_rejects_invalid_raw_input_message(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    terminal_errors: list[dict] = []

    async def fake_load_run_input_messages(run):
        assert run.input_message_id == 10
        return [
            SimpleNamespace(
                content="hello",
                image_content=None,
                extra_metadata={"raw_message": {"type": "human", "content": object()}},
            )
        ]

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, **kwargs):
        terminal_errors.append(
            {
                "run_id": run_id,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
            }
        )
        return run_worker.TerminalTransition(status=status, changed=True)

    def fail_stream_agent_chat(**kwargs):
        del kwargs
        raise AssertionError("invalid input message must not enter chat stream")

    monkeypatch.setattr(run_worker, "_load_run_input_messages", fake_load_run_input_messages)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fail_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert terminal_errors == [
        {
            "run_id": "run-1",
            "status": "failed",
            "error_type": "invalid_input_message",
            "error_message": "invalid raw_message for chat input message",
        }
    ]


@pytest.mark.asyncio
async def test_chunked_event_writer_flushes_loading_chunks_by_thread(monkeypatch: pytest.MonkeyPatch):
    events: list[dict] = []

    async def fake_append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload, "thread_id": thread_id})

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_run_event)

    writer = run_worker.ChunkedEventWriter("run-1", "parent-thread")
    await writer.append({"status": "loading", "response": "parent", "thread_id": "parent-thread"})
    await writer.append({"status": "loading", "response": "child", "thread_id": "child-thread"})
    await writer.flush()

    assert events == [
        {
            "run_id": "run-1",
            "event_type": "messages",
            "payload": {"items": [{"status": "loading", "response": "parent", "thread_id": "parent-thread"}]},
            "thread_id": "parent-thread",
        },
        {
            "run_id": "run-1",
            "event_type": "messages",
            "payload": {"items": [{"status": "loading", "response": "child", "thread_id": "child-thread"}]},
            "thread_id": "child-thread",
        },
    ]


@pytest.mark.asyncio
async def test_chunked_event_writer_flushes_semantic_tool_call_immediately(monkeypatch: pytest.MonkeyPatch):
    events: list[dict] = []

    async def fake_append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload, "thread_id": thread_id})

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_run_event)

    writer = run_worker.ChunkedEventWriter("run-1", "parent-thread")
    chunk = {
        "status": "loading",
        "response": "",
        "thread_id": "parent-thread",
        "stream_event": {
            "type": "tool_call",
            "message_id": "msg-1",
            "tool_call_id": "call-1",
            "name": "task",
            "args": {"description": "do work"},
            "index": 0,
            "thread_id": "parent-thread",
            "namespace": [],
        },
    }
    await writer.append(chunk)

    assert events == [
        {
            "run_id": "run-1",
            "event_type": "messages",
            "payload": {"items": [chunk]},
            "thread_id": "parent-thread",
        }
    ]


def test_model_output_detection_accepts_text_reasoning_and_tool_calls_only():
    assert run_worker._contains_model_output({"stream_event": {"type": "message_delta", "content": "你"}})
    assert run_worker._contains_model_output({"stream_event": {"type": "message_delta", "reasoning_content": "思考"}})
    assert run_worker._contains_model_output({"stream_event": {"type": "tool_call_delta", "args_delta": "{"}})
    assert not run_worker._contains_model_output({"status": "metadata", "run_id": "run-1"})
    assert not run_worker._contains_model_output({"stream_event": {"type": "message_delta", "content": ""}})
    assert not run_worker._contains_model_output({"stream_event": {"type": "tool_call_delta", "args_delta": ""}})


@pytest.mark.asyncio
async def test_timing_persistence_failure_does_not_fail_agent_execution(monkeypatch: pytest.MonkeyPatch):
    class FailingRepository:
        def __init__(self, _db):
            pass

        async def record_prepared(self, *_args, **_kwargs):
            raise RuntimeError("timing storage unavailable")

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session)
    monkeypatch.setattr(run_worker, "AgentRunRepository", FailingRepository)

    await run_worker._record_run_timing_best_effort("run-1", "worker-1:token", "prepared")


def test_run_owner_token_has_stable_worker_prefix_and_unique_attempt_suffix():
    ctx = {"worker_id": "worker-stable"}

    first = run_worker._run_owner_token(ctx)
    second = run_worker._run_owner_token(ctx)

    assert first.startswith("worker-stable:")
    assert second.startswith("worker-stable:")
    assert first != second


@pytest.mark.asyncio
async def test_run_context_stops_when_heartbeat_cannot_renew(monkeypatch: pytest.MonkeyPatch):
    renew = AsyncMock(return_value=False)
    monkeypatch.setattr(run_worker, "RUN_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(run_worker, "renew_run_lease", renew)
    monkeypatch.setattr(run_worker, "_run_attempt_finished", AsyncMock(return_value=False))
    run_ctx = run_worker.RunContext(run_id="run-1", worker_id="worker-1:attempt-1")

    await run_ctx._heartbeat_lease()

    renew.assert_awaited_once_with("run-1", "worker-1:attempt-1")
    assert run_ctx.lease_lost is True
    assert run_ctx.cancel_event.is_set()


@pytest.mark.asyncio
async def test_run_context_fails_closed_when_terminal_attempt_check_fails(monkeypatch: pytest.MonkeyPatch):
    """无法确认本 attempt 的终态时，继续按丢失 ownership 停止执行。"""
    monkeypatch.setattr(run_worker, "RUN_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(run_worker, "renew_run_lease", AsyncMock(return_value=False))
    monkeypatch.setattr(
        run_worker, "_run_attempt_finished", AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    context = run_worker.RunContext(run_id="run-1", worker_id="worker-1:attempt-1")

    await context._heartbeat_lease()

    assert context.lease_lost and context.cancel_event.is_set()


@pytest.mark.asyncio
async def test_worker_startup_ensures_builtin_mcp_servers(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_initialize():
        calls.append("initialize")

    async def fake_require_current_schema():
        calls.append("require_current_schema")

    async def fake_ensure_builtin_mcp_servers_in_db():
        calls.append("ensure_builtin_mcp_servers_in_db")

    @asynccontextmanager
    async def fake_session_ctx():
        yield SimpleNamespace(commit=AsyncMock())

    async def fake_init_builtin_skills(session):
        del session
        calls.append("init_builtin_skills")

    async def fake_ensure_options_in_db(session):
        del session
        calls.append("ensure_options_in_db")

    async def fake_invalidate_option_cache(key):
        del key
        calls.append("invalidate_option_cache")

    async def fake_recover_pending_dispatches():
        calls.append("recover_pending_dispatches")

    async def fake_publish_reconciliation_health():
        calls.append("publish_reconciliation_health")

    async def fake_publish_task_reconciliation_health():
        calls.append("publish_task_reconciliation_health")

    async def fake_reconcile_expired_run_leases():
        calls.append("reconcile_expired_run_leases")
        return []

    async def fake_reconcile_pending_runtime_cleanups():
        calls.append("reconcile_pending_runtime_cleanups")
        return []

    async def fake_reconciliation_loop():
        calls.append("reconciliation_loop")

    async def fake_reconcile_and_publish_tasks():
        calls.append("reconcile_and_publish_tasks")
        return []

    async def fake_task_reconciliation_loop():
        calls.append("task_reconciliation_loop")

    async def fake_recover_scheduled_dispatches():
        calls.append("recover_scheduled_dispatches")

    async def fake_claim_and_dispatch_due_jobs():
        calls.append("claim_and_dispatch_due_jobs")

    monkeypatch.setattr(run_worker.pg_manager, "initialize", fake_initialize)
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", fake_require_current_schema)
    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "ensure_builtin_mcp_servers_in_db", fake_ensure_builtin_mcp_servers_in_db)
    monkeypatch.setattr(run_worker, "init_builtin_skills", fake_init_builtin_skills)
    monkeypatch.setattr(config_options, "ensure_options_in_db", fake_ensure_options_in_db)
    monkeypatch.setattr(config_options, "invalidate_option_cache", fake_invalidate_option_cache)
    monkeypatch.setattr(run_worker, "recover_pending_dispatches", fake_recover_pending_dispatches)
    monkeypatch.setattr(run_worker, "reconcile_expired_run_leases", fake_reconcile_expired_run_leases)
    monkeypatch.setattr(
        run_worker,
        "reconcile_pending_runtime_cleanups",
        fake_reconcile_pending_runtime_cleanups,
    )
    monkeypatch.setattr(run_worker, "_publish_reconciliation_health", fake_publish_reconciliation_health)
    monkeypatch.setattr(run_worker, "_publish_task_reconciliation_health", fake_publish_task_reconciliation_health)
    monkeypatch.setattr(run_worker, "_reconcile_agent_run_leases_forever", fake_reconciliation_loop)
    monkeypatch.setattr(run_worker, "reconcile_and_publish_tasks", fake_reconcile_and_publish_tasks)
    monkeypatch.setattr(run_worker, "_reconcile_durable_tasks_forever", fake_task_reconciliation_loop)
    monkeypatch.setattr(run_worker, "recover_scheduled_dispatches", fake_recover_scheduled_dispatches)
    monkeypatch.setattr(run_worker, "claim_and_dispatch_due_jobs", fake_claim_and_dispatch_due_jobs)
    options_module = importlib.import_module("yuxi.modules.system.options")
    monkeypatch.setattr(options_module, "ensure_options_in_db", fake_ensure_options_in_db)

    ctx = {}
    await run_worker._worker_startup(ctx)
    await ctx[run_worker._RECONCILIATION_TASK_KEY]
    await ctx[run_worker._TASK_RECONCILIATION_TASK_KEY]

    assert calls == [
        "initialize",
        "require_current_schema",
        "ensure_options_in_db",
        "invalidate_option_cache",
        "ensure_builtin_mcp_servers_in_db",
        "init_builtin_skills",
        "reconcile_expired_run_leases",
        "reconcile_pending_runtime_cleanups",
        "recover_pending_dispatches",
        "reconcile_and_publish_tasks",
        "publish_task_reconciliation_health",
        "recover_scheduled_dispatches",
        "claim_and_dispatch_due_jobs",
        "publish_reconciliation_health",
        "reconciliation_loop",
        "task_reconciliation_loop",
    ]
    assert ctx["worker_id"] == run_worker.WORKER_ID


async def test_durable_task_publication_failure_does_not_refresh_health(monkeypatch):
    sleep_calls = 0
    health_calls = 0

    async def controlled_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    async def fail_reconciliation():
        raise ConnectionError("arq publication failed")

    async def publish_health():
        nonlocal health_calls
        health_calls += 1

    monkeypatch.setattr(run_worker.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(run_worker, "reconcile_and_publish_tasks", fail_reconciliation)
    monkeypatch.setattr(run_worker, "_publish_task_reconciliation_health", publish_health)

    with pytest.raises(asyncio.CancelledError):
        await run_worker._reconcile_durable_tasks_forever()

    assert health_calls == 0


def test_worker_settings_publish_short_ttl_versioned_health_contract():
    assert run_worker.WorkerSettings.max_jobs == run_worker.worker_max_jobs()
    assert run_worker.WorkerSettings.health_check_key == "yuxi:worker:health:agent-run-v1"
    assert 0 < run_worker.WorkerSettings.health_check_interval <= 10


async def test_worker_polls_new_requests_within_interactive_latency_budget():
    """真实 ARQ 配置必须把空闲队列轮询等待限制在 50ms 内。"""
    from arq.worker import create_worker

    worker = create_worker(run_worker.WorkerSettings, handle_signals=False)
    assert 0 < worker.poll_delay_s <= 0.05


def test_worker_settings_max_jobs_uses_environment():
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("ARQ_MAX_JOBS", "50")

        assert run_worker.worker_max_jobs() == 50


def test_worker_settings_reject_invalid_redis_dsn_instead_of_using_arq_default():
    env = os.environ.copy()
    env["REDIS_URL"] = "http://configured-redis.invalid:6379/0"

    completed = subprocess.run(
        [sys.executable, "-c", "import yuxi.modules.agents.services.runner"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "invalid DSN scheme" in completed.stderr


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_refresh_success_lease(monkeypatch: pytest.MonkeyPatch):
    calls = {"reconcile": 0, "publish": 0}

    async def no_wait(_seconds: float):
        return None

    async def fail_then_cancel():
        calls["reconcile"] += 1
        if calls["reconcile"] == 1:
            raise RuntimeError("database schema mismatch")
        raise asyncio.CancelledError

    async def publish():
        calls["publish"] += 1

    monkeypatch.setattr(run_worker.asyncio, "sleep", no_wait)
    monkeypatch.setattr(run_worker, "reconcile_expired_run_leases", fail_then_cancel)
    monkeypatch.setattr(run_worker, "_publish_reconciliation_health", publish)

    with pytest.raises(asyncio.CancelledError):
        await run_worker._reconcile_agent_run_leases_forever()

    assert calls == {"reconcile": 2, "publish": 0}


@pytest.mark.asyncio
async def test_worker_startup_fails_when_system_options_cannot_initialize(monkeypatch: pytest.MonkeyPatch):

    monkeypatch.setattr(run_worker.pg_manager, "initialize", lambda: None)
    monkeypatch.setattr(run_worker.pg_manager, "require_current_schema", AsyncMock())

    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    async def fail_initialize(_session):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(config_options, "ensure_options_in_db", fail_initialize)

    with pytest.raises(RuntimeError, match="config load failed"):
        await run_worker._worker_startup({})


@pytest.mark.asyncio
async def test_worker_shutdown_closes_queue_clients_before_postgres(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    never_finishes = asyncio.Event()
    reconciliation_task = asyncio.create_task(never_finishes.wait())

    async def fake_close_queue_clients():
        calls.append("redis")

    async def fake_close_postgres():
        calls.append("postgres")

    monkeypatch.setattr("yuxi.modules.agents.services.transport.close_queue_clients", fake_close_queue_clients)
    monkeypatch.setattr(run_worker.pg_manager, "close", fake_close_postgres)

    await run_worker._worker_shutdown({run_worker._RECONCILIATION_TASK_KEY: reconciliation_task})

    assert calls == ["redis", "postgres"]
    assert reconciliation_task.cancelled()


@pytest.mark.asyncio
async def test_manifest_persist_failure_fails_run_before_execution(monkeypatch: pytest.MonkeyPatch):
    """manifest 固化失败时 Run 显式失败，且不得进入执行流。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    terminal_calls: list[dict] = []
    stream_called = asyncio.Event()

    async def fake_persist_manifest(**kwargs):
        del kwargs
        raise RuntimeError("manifest db unavailable")

    async def fake_mark_terminal(run_id: str, status: str, **kwargs):
        terminal_calls.append({"run_id": run_id, "status": status, **kwargs})
        return run_worker.TerminalTransition(status=status, changed=True)

    def fake_stream_agent_chat(**kwargs):
        del kwargs
        stream_called.set()
        return _ExecutionAsyncIter([])

    monkeypatch.setattr(run_worker, "prepare_and_record_run_execution", fake_persist_manifest)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert not stream_called.is_set()
    assert len(terminal_calls) == 1
    assert terminal_calls[0]["status"] == "failed"
    assert terminal_calls[0]["error_type"] == "manifest_persist_failed"
    assert "执行未开始" in terminal_calls[0]["error_message"]


def test_retry_requires_new_manifest_fingerprint_to_match_write_once_fact():
    persisted = SimpleNamespace(manifest_fingerprint="a" * 64)

    run_worker._require_persisted_manifest_match(persisted, recorded=False, fingerprint="a" * 64)
    with pytest.raises(RuntimeError, match="运行资产已在重试前变化"):
        run_worker._require_persisted_manifest_match(persisted, recorded=False, fingerprint="b" * 64)


@pytest.mark.asyncio
async def test_cancel_during_manifest_preparation_settles_without_waiting_for_lease(monkeypatch):
    """配置准备期间取消不能被误判为固化失败并留待 lease 超时。"""
    run = _build_run()
    _patch_common(monkeypatch, run)
    cancelled = False

    async def persist(**kwargs):
        """模拟取消先提交，随后 manifest 写入因状态改变失败。"""
        nonlocal cancelled
        cancelled = True
        raise RuntimeError("manifest requires running")

    async def is_cancelled(*args):
        return cancelled

    finish = AsyncMock(return_value=run_worker.TerminalTransition(status="cancelled", changed=True))
    monkeypatch.setattr(run_worker, "prepare_and_record_run_execution", persist)
    monkeypatch.setattr(run_worker, "_is_cancel_requested", is_cancelled)
    monkeypatch.setattr(run_worker, "_confirmed_user_cancel", is_cancelled)
    monkeypatch.setattr(run_worker, "_finish_user_cancel", finish)
    monkeypatch.setattr(run_worker, "mark_run_terminal", AsyncMock(side_effect=AssertionError("不能转为 failed")))
    monkeypatch.setattr(run_worker, "stream_agent_chat", lambda **kwargs: pytest.fail("取消后不能开始执行"))
    await run_worker.process_agent_run({"job_try": 1}, run.id)
    finish.assert_awaited_once()
    assert finish.call_args.kwargs["run_id"] == run.id
