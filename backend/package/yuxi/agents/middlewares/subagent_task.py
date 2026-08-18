from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ModelRequest, ModelResponse, ResponseT
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.user_repository import UserRepository
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Agent


def _subagent_run_service_module():
    from yuxi.services import subagent_run_service

    return subagent_run_service


def _async_only_tool(*, name: str, coroutine: Callable[..., Awaitable[Any]], description: str) -> StructuredTool:
    """后台子智能体工具只在异步链路执行；仅声明 coroutine，同步调用由 LangChain 直接报错。"""
    return StructuredTool.from_function(name=name, coroutine=coroutine, description=description, infer_schema=True)


TASK_SYSTEM_PROMPT = """## `task`（子智能体任务工具）

你可以使用 `task` 工具把复杂、独立的子任务交给已配置的子智能体处理。子智能体只返回最终结果，你看不到它的中间步骤。
工具结果会包含子智能体线程 ID，后续需要继续同一个子任务时，把该 ID 作为 `thread_id` 传回 `task`。

使用原则：
- 任务足够复杂、可以独立完成、或需要隔离上下文时使用。
- 多个互不依赖的子任务可以并行调用多个 `task`。
- 继续既有子智能体任务时传入之前结果中的 `thread_id`；新任务不要填写 `thread_id`。
- 不要并行调用同一个 `thread_id`，避免多个续跑请求同时写入同一子线程。
- 简单问题或少量直接工具调用不要委派。
- 调用时必须选择下方可用的 `subagent_slug`，并在 `description` 中写清目标、上下文和期望输出。
- 不要通过 shell、curl、HTTP API 或命令行间接调用子智能体；需要子智能体时必须使用 `task` 工具。

后台子智能体：
- 长任务或多个可并行任务优先使用 `subagent_start`，它会立即返回 `run_id` 和 `thread_id`，父智能体可以继续工作。
- 后续用 `subagent_status` 查询状态和最近进度，`subagent_cancel` 取消，
  `subagent_await` 在明确需要结果时等待。
- `thread_id` 是子智能体长期上下文 ID；同一个 `thread_id` 完成后可以继续创建新的 run。
  若同线程已有运行中 run，会返回 busy，不会隐藏排队。
- 短任务且父智能体必须立刻依赖结果时继续使用 `task`。

Available subagent slugs:

{available_agents}"""

TASK_TOOL_DESCRIPTION = """Launch a configured Yuxi subagent to handle an isolated task.

Available subagent slugs:
{available_agents}

Use `subagent_slug` to select one available subagent and put the full task brief in `description`.
Omit `thread_id` for a new task. To continue a previous subagent task, pass the child thread ID returned by
that prior task result as `thread_id`.
Do not call subagents through shell, curl, HTTP APIs, or command-line indirection."""

SUBAGENT_START_DESCRIPTION = """Start a configured Yuxi subagent asynchronously.

Returns a child thread ID for future continuation and a run ID for status/cancel/result checks.
Use this for long-running or parallelizable subagent work. If `thread_id` is provided, it continues that subagent
thread when no active run is currently writing to it."""

SUBAGENT_STATUS_DESCRIPTION = """Check a subagent run status by run_id.

Returns the current run status, a compact progress summary with the latest 3 readable messages, and the final result
when the run has reached a terminal status."""

SUBAGENT_CANCEL_DESCRIPTION = """Cancel a running subagent run by run_id."""

SUBAGENT_AWAIT_DESCRIPTION = """Wait for a subagent run to finish and return its final result."""

TASK_DESCRIPTION_ARG = "需要子智能体独立完成的任务描述，包含必要上下文和期望输出。"
SUBAGENT_SLUG_ARG = "要调用的子智能体 slug，必须是工具描述中列出的可用项之一。"
TASK_THREAD_ID_ARG = "可选。要继续的既有子智能体线程 ID，通常来自之前 task 工具结果；新任务不要填写。"
ASYNC_THREAD_ID_ARG = "可选。要继续的后台子智能体线程 ID，来自之前 subagent_start 返回的 thread_id；新任务不要填写。"
SUBAGENT_RUN_ID_ARG = "子智能体运行 ID，由 subagent_start 返回。"


async def create_subagent_task_middleware(parent_context) -> YuxiSubAgentMiddleware | None:
    """根据父智能体上下文加载可用子智能体，并在存在可调用项时创建 task 中间件。"""
    selected_slugs = [
        str(slug).strip() for slug in (getattr(parent_context, "subagents", None) or []) if str(slug).strip()
    ]
    uid = str(getattr(parent_context, "uid", "") or "").strip()
    if not uid:
        return None

    async with pg_manager.get_async_session_context() as db:
        user = await UserRepository().get_by_uid_with_db(db, uid)
        if user is None:
            return None
        repo = AgentRepository(db)
        if selected_slugs:
            subagents: list[Agent] = []
            seen: set[str] = set()
            for slug in selected_slugs:
                if slug in seen:
                    continue
                seen.add(slug)
                agent = await repo.get_visible_by_slug(slug=slug, user=user, kind="subagent")
                if agent:
                    subagents.append(agent)
        else:
            subagents = await repo.list_visible_subagents(user=user)

    if not subagents:
        return None
    return YuxiSubAgentMiddleware(parent_context=parent_context, subagents=subagents)


class YuxiSubAgentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    def __init__(self, *, parent_context, subagents: list[Agent]) -> None:
        super().__init__()
        self.parent_context = parent_context
        self.subagents = {agent.slug: agent for agent in subagents}
        self._synced_child_run_ids: set[str] = set()
        self._output_sync_lock = asyncio.Lock()
        available_agents = "\n".join(f"- {agent.slug}: {agent.description or agent.name}" for agent in subagents)
        self.system_prompt = TASK_SYSTEM_PROMPT.format(available_agents=available_agents)
        self.tools = [self._build_task_tool(available_agents), *self._build_async_subagent_tools(available_agents)]

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(
            request.override(system_message=append_to_system_message(request.system_message, self.system_prompt))
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        return await handler(
            request.override(system_message=append_to_system_message(request.system_message, self.system_prompt))
        )

    def _build_task_tool(self, available_agents: str) -> StructuredTool:
        """构建 task 工具：启动子智能体后阻塞等待其最终结果。"""

        async def atask(
            description: Annotated[str, TASK_DESCRIPTION_ARG],
            subagent_slug: Annotated[str, SUBAGENT_SLUG_ARG],
            runtime: ToolRuntime,
            thread_id: Annotated[str | None, TASK_THREAD_ID_ARG] = None,
        ) -> str | Command:
            started, error = await self._start_subagent(
                description=description,
                subagent_slug=subagent_slug,
                runtime=runtime,
                thread_id=thread_id,
                error_prefix="无法调用子智能体",
            )
            if error is not None:
                return error

            # 阻塞父智能体运行，直到子 run 终结再读取最终结果；运行失败时 result 含 error 信息
            parent_runtime = started.parent_runtime
            subagent_service = _subagent_run_service_module()
            try:
                from yuxi.services.agent_run_service import AgentRunWaitTimeout, await_agent_run_result

                result = await await_agent_run_result(run_id=started.result.run.id, current_uid=parent_runtime.uid)
                run = await self._get_verified_subagent_run(
                    run_id=started.result.run.id,
                    uid=parent_runtime.uid,
                    created_by_run_id=parent_runtime.created_by_run_id,
                )
            except AgentRunWaitTimeout as exc:
                try:
                    run = await self._get_verified_subagent_run(
                        run_id=started.result.run.id,
                        uid=parent_runtime.uid,
                        created_by_run_id=parent_runtime.created_by_run_id,
                    )
                except ValueError as verify_exc:
                    return str(verify_exc)
                subagent_run = subagent_service.serialize_subagent_run_state(run)
                return _task_wait_timeout_response(exc.result, runtime.tool_call_id, subagent_run)
            except ValueError as exc:
                return str(exc)

            await self._sync_child_outputs_to_parent(parent_runtime, run)
            subagent_run = subagent_service.serialize_subagent_run_state(run)
            return _task_result_response(result, runtime.tool_call_id, subagent_run)

        return _async_only_tool(
            name="task",
            coroutine=atask,
            description=TASK_TOOL_DESCRIPTION.format(available_agents=available_agents),
        )

    def _build_async_subagent_tools(self, available_agents: str) -> list[StructuredTool]:
        """构建后台子智能体生命周期工具：start/status/events/cancel/await。"""

        async def asubagent_start(
            description: Annotated[str, TASK_DESCRIPTION_ARG],
            subagent_slug: Annotated[str, SUBAGENT_SLUG_ARG],
            runtime: ToolRuntime,
            thread_id: Annotated[str | None, ASYNC_THREAD_ID_ARG] = None,
        ) -> str | Command:
            started, error = await self._start_subagent(
                description=description,
                subagent_slug=subagent_slug,
                runtime=runtime,
                thread_id=thread_id,
                error_prefix="无法启动子智能体",
            )
            if error is not None:
                return error

            result, agent_item = started.result, started.agent_item
            subagent_service = _subagent_run_service_module()
            payload = {
                "status": "started" if result.created else "existing",
                "run_id": result.run.id,
                "thread_id": result.relation.child_thread_id,
                "subagent_slug": subagent_slug,
                "subagent_name": agent_item.name,
                "created_by_run_id": result.run.created_by_run_id,
                "run_status": result.run.status,
                "continuing": result.continuing,
                "subagent_thread_relation_id": result.relation.id,
                **subagent_service.subagent_run_urls(result.run.id),
            }
            subagent_run = subagent_service.serialize_subagent_run_state(result.run)
            return _json_tool_command(payload, runtime.tool_call_id, subagent_run=subagent_run)

        async def asubagent_status(
            run_id: Annotated[str, SUBAGENT_RUN_ID_ARG],
            runtime: ToolRuntime,
        ) -> str | Command:
            from yuxi.services.agent_run_service import get_agent_run_progress, get_agent_run_result

            parent_runtime, runtime_error = self._require_async_parent_runtime("无法查询子智能体")
            if runtime_error:
                return runtime_error
            try:
                run = await self._get_verified_subagent_run(
                    uid=parent_runtime.uid,
                    created_by_run_id=parent_runtime.created_by_run_id,
                    run_id=run_id,
                )

                # 如果 run 已经终结，则尝试读取最终结果；否则 result 保持 None
                result = None
                if run.status in TERMINAL_RUN_STATUSES:
                    async with pg_manager.get_async_session_context() as db:
                        result = await get_agent_run_result(run_id=run.id, current_uid=parent_runtime.uid, db=db)

            except ValueError as exc:
                return str(exc)

            await self._sync_child_outputs_to_parent(parent_runtime, run)
            subagent_service = _subagent_run_service_module()
            payload = {
                "status": run.status,
                "run_id": run.id,
                "thread_id": run.conversation_thread_id,
                "subagent_slug": run.agent_slug,
                "error": run.error_message,
                "progress": await get_agent_run_progress(run.id),
                **subagent_service.subagent_run_urls(run.id),
            }
            if result:
                payload["result"] = result
            subagent_run = subagent_service.serialize_subagent_run_state(run)
            return _json_tool_command(payload, runtime.tool_call_id, subagent_run=subagent_run)

        async def asubagent_cancel(
            run_id: Annotated[str, SUBAGENT_RUN_ID_ARG],
            runtime: ToolRuntime,
        ) -> str | Command:
            from yuxi.services.agent_run_service import request_cancel_agent_run

            parent_runtime, runtime_error = self._require_async_parent_runtime("无法取消子智能体")
            if runtime_error:
                return runtime_error
            try:
                await self._get_verified_subagent_run(
                    run_id=run_id,
                    uid=parent_runtime.uid,
                    created_by_run_id=parent_runtime.created_by_run_id,
                )  # 校验子智能体归属

                # 取消子智能体运行，返回最新 run 状态
                async with pg_manager.get_async_session_context() as db:
                    run = await request_cancel_agent_run(run_id=run_id, current_uid=parent_runtime.uid, db=db)

            except ValueError as exc:
                return str(exc)

            subagent_service = _subagent_run_service_module()
            payload = {
                "status": run.status,
                "run_id": run.id,
                "thread_id": run.conversation_thread_id,
                **subagent_service.subagent_run_urls(run.id),
            }
            subagent_run = subagent_service.serialize_subagent_run_state(run)
            return _json_tool_command(payload, runtime.tool_call_id, subagent_run=subagent_run)

        async def asubagent_await(
            run_id: Annotated[str, SUBAGENT_RUN_ID_ARG],
            runtime: ToolRuntime,
        ) -> str | Command:
            from yuxi.services.agent_run_service import AgentRunWaitTimeout, await_agent_run_result

            parent_runtime, runtime_error = self._require_async_parent_runtime("无法等待子智能体")
            if runtime_error:
                return runtime_error
            wait_timed_out = False
            try:
                # 等待前校验 run 归属，避免越权等待其它子任务
                await self._get_verified_subagent_run(
                    run_id=run_id,
                    uid=parent_runtime.uid,
                    created_by_run_id=parent_runtime.created_by_run_id,
                )
                # 等待结束后重新读取已验证的最新 run 状态
                result = await await_agent_run_result(run_id=run_id, current_uid=parent_runtime.uid)
                run = await self._get_verified_subagent_run(
                    run_id=run_id,
                    uid=parent_runtime.uid,
                    created_by_run_id=parent_runtime.created_by_run_id,
                )
            except AgentRunWaitTimeout as exc:
                wait_timed_out = True
                result = exc.result
                try:
                    run = await self._get_verified_subagent_run(
                        run_id=run_id,
                        uid=parent_runtime.uid,
                        created_by_run_id=parent_runtime.created_by_run_id,
                    )
                except ValueError as verify_exc:
                    return str(verify_exc)
            except ValueError as exc:
                return str(exc)

            await self._sync_child_outputs_to_parent(parent_runtime, run)
            subagent_service = _subagent_run_service_module()
            payload = {
                "status": run.status,
                "run_id": run.id,
                "thread_id": run.conversation_thread_id,
                "result": result,
            }
            if wait_timed_out:
                payload["wait_timed_out"] = True
                payload["message"] = "子智能体仍在运行，等待最终结果超时；请稍后继续查询。"
            subagent_run = subagent_service.serialize_subagent_run_state(run)
            return _json_tool_command(payload, runtime.tool_call_id, subagent_run=subagent_run)

        return [
            _async_only_tool(
                name="subagent_start",
                coroutine=asubagent_start,
                description=SUBAGENT_START_DESCRIPTION + "\n\nAvailable subagent slugs:\n" + available_agents,
            ),
            _async_only_tool(
                name="subagent_status",
                coroutine=asubagent_status,
                description=SUBAGENT_STATUS_DESCRIPTION,
            ),
            _async_only_tool(
                name="subagent_cancel",
                coroutine=asubagent_cancel,
                description=SUBAGENT_CANCEL_DESCRIPTION,
            ),
            _async_only_tool(
                name="subagent_await",
                coroutine=asubagent_await,
                description=SUBAGENT_AWAIT_DESCRIPTION,
            ),
        ]

    def _parent_runtime(self) -> _ParentRuntime:
        """从父智能体 context 中抽取子智能体运行所需的最小父运行信息。"""
        parent_thread_id = str(getattr(self.parent_context, "parent_thread_id", None) or self.parent_context.thread_id)
        file_thread_id = str(getattr(self.parent_context, "file_thread_id", None) or parent_thread_id)
        uid = str(getattr(self.parent_context, "uid", "") or "").strip()
        created_by_run_id = str(getattr(self.parent_context, "run_id", "") or "").strip()
        runtime_thread_id = str(getattr(self.parent_context, "thread_id", "") or "").strip()
        skills_thread_id = str(getattr(self.parent_context, "skills_thread_id", None) or runtime_thread_id)
        sandbox_instance_id = str(
            getattr(self.parent_context, "sandbox_instance_id", None) or created_by_run_id or runtime_thread_id
        )
        return _ParentRuntime(
            runtime_thread_id=runtime_thread_id,
            file_thread_id=file_thread_id,
            skills_thread_id=skills_thread_id,
            uid=uid,
            created_by_run_id=created_by_run_id,
            sandbox_instance_id=sandbox_instance_id,
        )

    async def _checkpoint_parent_outputs(
        self,
        parent_runtime: _ParentRuntime,
        *,
        base_revision_id: str | None = None,
    ) -> tuple[str, list[dict]]:
        """固化父 Run 当前 outputs 私有快照，作为父子独立 sandbox 的同步边界。"""
        from yuxi.repositories.thread_output_repository import ThreadOutputRepository
        from yuxi.services.thread_output_service import get_current_output_snapshot, stage_thread_outputs

        async with pg_manager.get_async_session_context() as db:
            conversation = await ConversationRepository(db).get_conversation_by_thread_id(parent_runtime.file_thread_id)
            if conversation is None or str(conversation.uid) != parent_runtime.uid:
                raise ValueError("父运行文件线程不存在")
            if base_revision_id is None:
                base_revision_id, _files = await get_current_output_snapshot(conversation=conversation, db=db)
            conversation_id = conversation.id

        revision_id = await stage_thread_outputs(
            runtime_thread_id=parent_runtime.runtime_thread_id,
            file_thread_id=parent_runtime.file_thread_id,
            skills_thread_id=parent_runtime.skills_thread_id,
            uid=parent_runtime.uid,
            conversation_id=conversation_id,
            run_id=parent_runtime.created_by_run_id,
            base_revision_id=base_revision_id,
            sandbox_instance_id=parent_runtime.sandbox_instance_id,
        )
        async with pg_manager.get_async_session_context() as db:
            revision = await ThreadOutputRepository(db).checkpoint(revision_id)
            await db.commit()
            return revision.id, list(revision.files or [])

    async def _sync_child_outputs_to_parent(self, parent_runtime: _ParentRuntime, child_run) -> None:
        """三方合并子 Run revision 与父 Run 增量，并重建父 runtime outputs。"""
        if child_run.status != "completed" or child_run.id in self._synced_child_run_ids:
            return
        async with self._output_sync_lock:
            if child_run.id in self._synced_child_run_ids:
                return
            await self._sync_child_outputs_to_parent_locked(parent_runtime, child_run)
            self._synced_child_run_ids.add(child_run.id)

    async def _sync_child_outputs_to_parent_locked(self, parent_runtime: _ParentRuntime, child_run) -> None:
        """在父 runtime 文件锁内完成一次 durable 子产物同步。"""

        from yuxi.repositories.thread_output_repository import ThreadOutputRepository, merge_output_manifests

        async with pg_manager.get_async_session_context() as db:
            conversation = await ConversationRepository(db).get_conversation_by_thread_id(parent_runtime.file_thread_id)
            if conversation is None or str(conversation.uid) != parent_runtime.uid:
                raise ValueError("父运行文件线程不存在")
            repository = ThreadOutputRepository(db)
            child_revision = await repository.get_revision_for_run(conversation, child_run.id, status="checkpoint")
            if child_revision is None or not child_revision.base_revision_id:
                raise ValueError("子智能体 outputs revision 不可用")
            base_revision = await repository.get_snapshot(conversation, child_revision.base_revision_id)
            if base_revision is None or base_revision.status != "checkpoint":
                raise ValueError("子智能体 outputs 基线不可用")
            synced_revision = await repository.get_revision_for_run(
                conversation,
                parent_runtime.created_by_run_id,
                status="checkpoint",
                base_revision_id=child_revision.id,
            )
            if synced_revision is not None:
                files = list(synced_revision.files or [])
            else:
                files = None
            child_files = list(child_revision.files or [])
            base_files = list(base_revision.files or [])

        if files is None:
            revision_id, parent_files = await self._checkpoint_parent_outputs(
                parent_runtime,
                base_revision_id=child_revision.id,
            )
            files = merge_output_manifests(
                base=base_files,
                staged=child_files,
                current=parent_files,
            )
            async with pg_manager.get_async_session_context() as db:
                await ThreadOutputRepository(db).set_checkpoint_files(revision_id, files)
                await db.commit()
        from yuxi.services.thread_output_service import hydrate_thread_outputs_to_sandbox

        await hydrate_thread_outputs_to_sandbox(
            runtime_thread_id=parent_runtime.runtime_thread_id,
            file_thread_id=parent_runtime.file_thread_id,
            skills_thread_id=parent_runtime.skills_thread_id,
            uid=parent_runtime.uid,
            files=files,
            sandbox_instance_id=parent_runtime.sandbox_instance_id,
            create_if_missing=False,
        )

    def _require_async_parent_runtime(self, error_prefix: str) -> tuple[_ParentRuntime, str | None]:
        """校验后台子智能体工具必须依赖的父运行上下文。"""
        parent_runtime = self._parent_runtime()
        if not parent_runtime.uid:
            return parent_runtime, f"{error_prefix}：当前运行时缺少 uid"
        if not parent_runtime.created_by_run_id:
            return parent_runtime, f"{error_prefix}：当前运行时缺少父运行 ID"
        return parent_runtime, None

    async def _start_subagent(
        self,
        *,
        description: str,
        subagent_slug: str,
        runtime: ToolRuntime,
        thread_id: str | None,
        error_prefix: str,
    ) -> tuple[_StartedSubagent | None, str | Command | None]:
        """校验并启动（或继续）后台子智能体 run；成功返回启动结果，失败返回可直接回传的错误响应。"""
        if subagent_slug not in self.subagents:
            allowed = ", ".join(f"`{slug}`" for slug in self.subagents)
            return None, f"无法调用子智能体 {subagent_slug}，可用子智能体只有：{allowed}"
        if not runtime.tool_call_id:
            raise ValueError("Tool call ID is required for subagent invocation")

        parent_runtime, runtime_error = self._require_async_parent_runtime(error_prefix)
        if runtime_error:
            return None, runtime_error

        agent_item = self.subagents[subagent_slug]
        input_message = build_chat_input_message(description)
        output_base_revision_id, _files = await self._checkpoint_parent_outputs(parent_runtime)
        subagent_service = _subagent_run_service_module()
        try:
            async with pg_manager.get_async_session_context() as db:
                result = await subagent_service.SubagentRunService(db).start(
                    uid=parent_runtime.uid,
                    created_by_run_id=parent_runtime.created_by_run_id,
                    agent_item=agent_item,
                    input_message=input_message,
                    tool_call_id=runtime.tool_call_id,
                    requested_thread_id=thread_id,
                    file_thread_id=parent_runtime.file_thread_id,
                    model_spec=self._subagent_model_override(agent_item),
                    output_base_revision_id=output_base_revision_id,
                )
        except subagent_service.SubagentRunBusy as exc:
            cleanup_error = await self._discard_rejected_subagent_checkpoint(output_base_revision_id)
            payload = exc.to_payload()
            if cleanup_error:
                payload["cleanup_warning"] = cleanup_error
            return None, _json_tool_command(payload, runtime.tool_call_id)
        except ValueError as exc:
            cleanup_error = await self._discard_rejected_subagent_checkpoint(output_base_revision_id)
            if cleanup_error:
                return None, f"{exc}；{cleanup_error}"
            return None, str(exc)
        if not result.created:
            existing_runtime = (
                result.run.input_payload.get("runtime") if isinstance(result.run.input_payload, dict) else None
            )
            existing_revision_id = (
                str(existing_runtime.get("output_base_revision_id") or "") if isinstance(existing_runtime, dict) else ""
            )
            if existing_revision_id != output_base_revision_id:
                cleanup_error = await self._discard_rejected_subagent_checkpoint(output_base_revision_id)
                if cleanup_error:
                    return None, f"{error_prefix}：命中已有子 Run，但{cleanup_error}"
        return _StartedSubagent(result=result, parent_runtime=parent_runtime, agent_item=agent_item), None

    async def _discard_rejected_subagent_checkpoint(self, revision_id: str) -> str | None:
        """回收未创建 child Run 时留下的父输出私有快照。"""
        from yuxi.services.thread_output_service import discard_unreferenced_output_checkpoint

        try:
            await discard_unreferenced_output_checkpoint(revision_id)
        except Exception as exc:
            return f"父输出临时快照清理失败：{exc}"
        return None

    def _subagent_model_override(self, agent_item: Agent) -> str | None:
        """当子智能体未显式配置模型时，沿用父智能体当前模型。"""
        config_context = (
            (agent_item.config_json or {}).get("context") if isinstance(agent_item.config_json, dict) else None
        )
        configured_model = ""
        if isinstance(config_context, dict):
            configured_model = str(config_context.get("model") or "").strip()
        if configured_model:
            return None
        return str(getattr(self.parent_context, "model", None) or "").strip() or None

    async def _get_verified_subagent_run(self, *, run_id: str, uid: str, created_by_run_id: str):
        """在工具调用前按父 run 作用域校验子 run 归属。"""
        subagent_service = _subagent_run_service_module()
        async with pg_manager.get_async_session_context() as db:
            return await subagent_service.SubagentRunService(db).get_run_for_creator(
                uid=uid,
                created_by_run_id=created_by_run_id,
                run_id=run_id,
            )


@dataclass(frozen=True)
class _ParentRuntime:
    runtime_thread_id: str
    file_thread_id: str
    skills_thread_id: str
    uid: str
    created_by_run_id: str
    sandbox_instance_id: str


@dataclass(frozen=True)
class _StartedSubagent:
    """``_start_subagent`` 的结果：子 run 启动产物及其依赖的父运行上下文。"""

    result: Any  # SubagentStartResult
    parent_runtime: _ParentRuntime
    agent_item: Agent


def _task_result_response(result: dict[str, Any], tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    """把后台子智能体 run 的最终结果转换为同步 task 工具结果。"""
    output = str(result.get("output") or "").strip()
    error = result.get("error") if isinstance(result.get("error"), dict) else None
    if not output and error:
        output = str(error.get("message") or "子智能体运行失败")
    if not output:
        output = "子智能体已完成任务，但没有返回文本结果。"

    tool_result = _tool_result_with_thread_id(subagent_run["child_thread_id"], output)
    return Command(
        update={"messages": [ToolMessage(tool_result, tool_call_id=tool_call_id)], "subagent_runs": [subagent_run]}
    )


def _task_wait_timeout_response(result: dict[str, Any], tool_call_id: str, subagent_run: dict[str, Any]) -> Command:
    """同步 task 等待到达上限时，明确告诉父智能体子 run 仍未终结。"""
    status = str(result.get("status") or subagent_run.get("status") or "running")
    run_id = str(result.get("agent_run_id") or subagent_run["run_id"])
    output = (
        f"子智能体仍在运行（status: {status}），尚未返回最终文本结果。\n"
        f"run_id: {run_id}\n"
        "请稍后使用 subagent_status 或 subagent_await 查询结果；不要把当前结果视为任务已完成。"
    )
    tool_result = _tool_result_with_thread_id(subagent_run["child_thread_id"], output)
    return Command(
        update={"messages": [ToolMessage(tool_result, tool_call_id=tool_call_id)], "subagent_runs": [subagent_run]}
    )


def _json_tool_command(
    payload: dict[str, Any],
    tool_call_id: str,
    *,
    subagent_run: dict[str, Any] | None = None,
) -> Command:
    """把后台子智能体工具的结构化结果包装成 ToolMessage。"""
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    update: dict[str, Any] = {"messages": [ToolMessage(content, tool_call_id=tool_call_id)]}
    if subagent_run is not None:
        update["subagent_runs"] = [subagent_run]
    return Command(update=update)


def _tool_result_with_thread_id(child_thread_id: str, content: str) -> str:
    """把子线程 ID 放进工具结果，方便后续继续同一子任务。"""
    return f"> 子智能体线程 ID: {child_thread_id}\n\n---\n\n{content}"
