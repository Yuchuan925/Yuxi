from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import asyncpg
import httpx
import pytest

from e2e_helpers import delete_agent, postgres_dsn, skip_if_external_quota
from test.live_api_cleanup import (
    make_test_conversation_title,
)
from yuxi.modules.agents.runtime.sandbox.paths import runtime_workdir_path

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]

RUN_TIMEOUT_SECONDS = int(os.getenv("E2E_RUN_TIMEOUT_SECONDS", "300"))


def _assert_ok(response: httpx.Response) -> None:
    assert response.status_code < 400, response.text


async def _create_agent(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post("/api/agent", json=payload, headers=headers)
    _assert_ok(response)
    agent = response.json().get("agent")
    assert isinstance(agent, dict), response.text
    return agent


async def _create_thread(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    agent_id: str,
    marker: str,
) -> tuple[str, str]:
    """创建 Public Thread，并从持久 Project 读取其 runtime Workdir。"""
    response = await client.post(
        "/api/v1/agents/threads",
        json={
            "agent_id": agent_id,
            "title": make_test_conversation_title(f"subagent-stream-e2e-{marker}"),
        },
        headers={**headers, "Idempotency-Key": f"subagent-thread-{uuid.uuid4().hex}"},
    )
    _assert_ok(response)
    payload = response.json()
    thread_id = payload.get("thread_id")
    assert thread_id, payload
    conn = await asyncpg.connect(postgres_dsn())
    try:
        workdir_path = await conn.fetchval(
            "SELECT p.workdir_path FROM projects p "
            "JOIN conversations c ON c.project_id = p.id WHERE c.thread_id = $1",
            thread_id,
        )
    finally:
        await conn.close()
    assert workdir_path, payload
    return str(thread_id), runtime_workdir_path(str(workdir_path))


async def _create_run(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    thread_id: str,
    query: str,
) -> dict:
    """经 Public Input 创建顶层 Turn 与首段 Run。"""
    response = await client.post(
        f"/api/v1/agents/threads/{thread_id}/events",
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": query}]}],
                    "yuxi": {"mode": "follow_up", "tool_approval_mode": "always_trust"},
                }
            ],
        },
        headers={**headers, "Idempotency-Key": f"subagent-input-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["input_id"] and accepted["turn_id"] and accepted["run_id"], accepted
    return accepted


async def _iter_sse(client: httpx.AsyncClient, headers: dict[str, str], thread_id: str):
    """把 Public Thread SSE 的 data 行解析为结构化事件。"""
    async with client.stream("GET", f"/api/v1/agents/threads/{thread_id}/events", headers=headers) as response:
        _assert_ok(response)
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                yield json.loads(line[6:])


async def _consume_run_stream(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    thread_id: str,
    turn_id: str,
    run_id: str,
) -> tuple[dict[str, int], dict[str, Any], list[dict[str, Any]]]:
    """消费顶层 Turn 流，只记录其 Run 增量与终态。"""
    event_counts: dict[str, int] = {}
    latest_agent_state: dict[str, Any] = {}
    public_items: list[dict[str, Any]] = []
    terminal_status = ""

    async def consume() -> None:
        nonlocal latest_agent_state, terminal_status
        async for event in _iter_sse(client, headers, thread_id):
            if event.get("turn_id") != turn_id:
                continue
            event_type = event["type"]
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            if event_type == "agent.session.turn.item.done" and event.get("yuxi", {}).get("run_id") == run_id:
                public_items.append(event["item"])
            if event_type == "yuxi.session.turn.state":
                latest_agent_state = event["agent_state"]
            if event_type == "agent.session.turn.failed":
                skip_if_external_quota(event["turn"].get("error"))
            if event_type in {
                "agent.session.turn.completed", "agent.session.turn.failed", "agent.session.turn.cancelled"
            }:
                terminal_status = event["turn"]["status"]
                return

    await asyncio.wait_for(consume(), timeout=RUN_TIMEOUT_SECONDS)
    assert terminal_status == "completed", {"status": terminal_status, "event_counts": event_counts}
    return event_counts, latest_agent_state, public_items


async def _assert_child_stream(
    client: httpx.AsyncClient, headers: dict[str, str], child_thread_id: str, child_run_id: str
) -> None:
    """子 Thread 的 Public 流能单独观察子 Run 的输出与终态。"""
    seen_output = False
    seen_terminal = False

    async def consume() -> None:
        nonlocal seen_output, seen_terminal
        async for event in _iter_sse(client, headers, child_thread_id):
            if event.get("yuxi", {}).get("run_id") != child_run_id:
                continue
            if event["type"] == "agent.session.turn.item.done":
                seen_output = True
            if event["type"] == "agent.session.turn.completed":
                seen_terminal = True
                return

    await asyncio.wait_for(consume(), timeout=RUN_TIMEOUT_SECONDS)
    assert seen_output and seen_terminal


def _find_named_tool_call_ids(items: list[dict], tool_name: str) -> set[str]:
    """只读取明确的公开 function_call，业务 payload 不参与递归路由。"""
    return {item["call_id"] for item in items if item["type"] == "function_call" and item["name"] == tool_name}


def _find_tool_result_contents(items: list[dict], tool_call_ids: set[str]) -> list[str]:
    """读取相同调用的公开结果。"""
    return [
        str(item["output"])
        for item in items
        if item["type"] == "function_call_output" and item["call_id"] in tool_call_ids
    ]


async def _read_viewer_file(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    thread_id: str,
    path: str,
) -> str:
    response = await client.get(
        "/api/viewer/filesystem/file",
        params={"thread_id": thread_id, "path": path},
        headers=headers,
    )
    _assert_ok(response)
    content = response.json().get("content")
    assert isinstance(content, str), response.text
    return content


def _viewer_scope_path(project_root: str, runtime_path: str) -> str:
    """把 Agent runtime 路径转换为 Viewer 的当前 Workdir 相对路径。"""
    prefix = f"{project_root.rstrip('/')}/"
    assert runtime_path.startswith(prefix), (project_root, runtime_path)
    return f"/{runtime_path[len(prefix) :]}"


async def test_subagent_stream_records_run_and_shares_output_files(
    e2e_client: httpx.AsyncClient,
    e2e_headers: dict[str, str],
):
    me_response = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    _assert_ok(me_response)
    me = me_response.json()
    if me.get("role") not in {"admin", "superadmin"}:
        pytest.skip("Subagent E2E needs an admin user to create temporary agents.")
    uid = str(me.get("uid") or "")
    assert uid, me

    suffix = uuid.uuid4().hex[:8]
    marker = f"YUXI_SUBAGENT_STREAM_E2E_{suffix}"
    sub_slug = f"e2e-subagent-{suffix}"
    main_slug = f"e2e-main-{suffix}"
    parent_input_path: str | None = None
    output_path: str | None = None
    parent_input_viewer_path: str | None = None
    output_viewer_path: str | None = None
    expected_content = "由这个子智能体创建"
    runtime_content = f"workdir-shared-{suffix}"
    runtime_marker = None
    created_agents: list[str] = []
    run_id: str | None = None
    turn_id: str | None = None
    thread_id: str | None = None
    child_thread_id: str | None = None
    run_completed = False

    default_response = await e2e_client.get("/api/agent/default", headers=e2e_headers)
    _assert_ok(default_response)
    default_context = ((default_response.json().get("agent") or {}).get("config_json") or {}).get("context") or {}
    base_context: dict[str, Any] = {"tools": [], "knowledges": [], "mcps": [], "skills": []}
    if default_context.get("model"):
        base_context["model"] = default_context["model"]

    share_config = {
        "version": 2,
        "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
        "manage_scope": None,
    }

    try:
        sub_agent = await _create_agent(
            e2e_client,
            e2e_headers,
            {
                "name": f"E2E 子智能体 {suffix}",
                "slug": sub_slug,
                "backend_id": "SubAgentBackend",
                "description": "真实流式 E2E 子智能体",
                "config_json": {
                    "context": {
                        **base_context,
                        "system_prompt": (
                            "你是专门负责文件读取、写入和校验的子智能体。收到任务后必须使用文件系统工具完成任务，"
                            "不要向用户提问。若任务给出 Project Workdir 标记，必须先用 execute 读取并报告其真实内容；"
                            "然后读取用户指定的来源文件，再严格写入目标路径，文件内容必须完全符合要求，"
                            "不要自动追加句号、引号、说明或其他字符。完成后只回复写入的路径和文件内容。"
                        ),
                    }
                },
                "share_config": share_config,
                "is_subagent": True,
            },
        )
        created_agents.append(sub_slug)

        await _create_agent(
            e2e_client,
            e2e_headers,
            {
                "name": f"E2E 主智能体 {suffix}",
                "slug": main_slug,
                "backend_id": "ChatbotAgent",
                "description": "真实流式 E2E 主智能体",
                "config_json": {
                    "context": {
                        **base_context,
                        "subagents": [sub_slug],
                        "system_prompt": (
                            "你是主智能体。严格按用户给出的工具顺序执行：先用 execute 创建 Project Workdir 标记，"
                            "再由你写入父文件，然后调用 subagent_start 派发子智能体，再用 subagent_await 等待，"
                            "子智能体完成后由你读取其结果；最后一个工具调用必须是 present_artifacts，"
                            "且必须传入目标文件。"
                            "在 present_artifacts 成功前不得结束回答，也不得用 execute 代替展示。"
                            "不要通过 shell、curl 或 HTTP API 调用子智能体。"
                        ),
                    }
                },
                "share_config": share_config,
                "is_subagent": False,
            },
        )
        created_agents.append(main_slug)

        default_agents_response = await e2e_client.get("/api/agent", headers=e2e_headers)
        _assert_ok(default_agents_response)
        default_agent_slugs = {str(item.get("slug")) for item in default_agents_response.json().get("agents") or []}
        assert sub_slug not in default_agent_slugs

        management_agents_response = await e2e_client.get("/api/agent?include_subagents=true", headers=e2e_headers)
        _assert_ok(management_agents_response)
        management_agent_slugs = {
            str(item.get("slug")) for item in management_agents_response.json().get("agents") or []
        }
        assert sub_slug in management_agent_slugs

        thread_id, project_root = await _create_thread(e2e_client, e2e_headers, main_slug, marker)
        runtime_marker = f"{project_root}/outputs/execution-marker.txt"
        parent_input_path = f"{project_root}/outputs/parent-input.txt"
        output_path = f"{project_root}/outputs/subagents.txt"
        parent_input_viewer_path = _viewer_scope_path(project_root, parent_input_path)
        output_viewer_path = _viewer_scope_path(project_root, output_path)
        query = (
            f"请严格依次完成：1）你先用 execute 执行 `printf '%s' '{runtime_content}' > '{runtime_marker}'`；"
            f"2）用 write_file 创建 {parent_input_path}，内容只有一行“{expected_content}”；"
            f"3）通过 subagent_start 派发子智能体 {sub_slug}，要求它先用 execute 执行 `cat '{runtime_marker}'`，"
            f"确认内容是 {runtime_content}，再读取 {parent_input_path}，并把完全相同的内容写入 {output_path}；"
            f"4）subagent_await 等待完成后，你必须用 read_file 读取 {output_path}；"
            f"5）最后调用 present_artifacts 展示 {output_path}。不要省略任何一步。"
        )
        accepted = await _create_run(
            e2e_client,
            e2e_headers,
            thread_id=thread_id,
            query=query,
        )
        run_id, turn_id = accepted["run_id"], accepted["turn_id"]

        event_counts, stream_agent_state, public_items = await _consume_run_stream(
            e2e_client,
            e2e_headers,
            thread_id,
            turn_id,
            run_id,
        )
        assert event_counts.get("agent.session.turn.item.done", 0) > 0
        assert event_counts.get("agent.session.turn.completed") == 1

        turn_response = await e2e_client.get(
            f"/api/v1/agents/threads/{thread_id}/turns/{turn_id}", headers=e2e_headers
        )
        _assert_ok(turn_response)
        parent_turn = turn_response.json()
        assert parent_turn["status"] == "completed"
        assert parent_turn["result_run_id"] == run_id

        run_response = await e2e_client.get(
            f"/api/v1/agents/threads/{thread_id}/runs/{run_id}", headers=e2e_headers
        )
        _assert_ok(run_response)
        parent_run = run_response.json()
        assert parent_run.get("status") == "completed"
        assert parent_run.get("turn_id") == turn_id

        state_response = await e2e_client.get(
            f"/api/v1/agents/threads/{thread_id}/state", headers=e2e_headers
        )
        _assert_ok(state_response)
        final_agent_state = state_response.json().get("agent_state") or stream_agent_state
        history_response = await e2e_client.get(
            f"/api/v1/agents/threads/{thread_id}/history", headers=e2e_headers
        )
        _assert_ok(history_response)
        history_payload = history_response.json()
        subagent_runs = final_agent_state.get("subagent_runs") or []
        assert subagent_runs, final_agent_state
        file_task_calls = {
            item["call_id"]
            for item in history_payload["items"]
            if item["type"] == "function_call" and item["name"] == "subagent_start"
            and output_path in json.dumps(item["arguments"], ensure_ascii=False)
        }
        assert file_task_calls, "父工具调用未指定目标文件任务"
        completed_runs = [
            item
            for item in subagent_runs
            if item.get("status") == "completed"
            and item.get("subagent_slug") == sub_slug
            and item["id"] in file_task_calls
        ]
        assert len(completed_runs) == 1, {"file_task_calls": sorted(file_task_calls), "runs": completed_runs}
        completed_run = completed_runs[0]
        assert completed_run.get("subagent_name") == sub_agent["name"]
        assert completed_run.get("child_thread_id")
        assert completed_run.get("id")

        child_thread_id = str(completed_run["child_thread_id"])
        child_state_response = await e2e_client.get(
            f"/api/v1/agents/threads/{child_thread_id}/state",
            params={"include_messages": "true"},
            headers=e2e_headers,
        )
        _assert_ok(child_state_response)
        child_state_payload = child_state_response.json()
        assert child_state_payload.get("parent_thread_id") == thread_id
        child_subagent_run = child_state_payload.get("subagent_run") or {}
        assert child_subagent_run.get("child_thread_id") == child_thread_id
        assert child_subagent_run.get("run_id")
        child_run_response = await e2e_client.get(
            f"/api/v1/agents/threads/{child_thread_id}/runs/{child_subagent_run['run_id']}",
            headers=e2e_headers,
        )
        _assert_ok(child_run_response)
        child_run = child_run_response.json()
        assert child_run.get("run_type") == "subagent"
        assert child_run.get("conversation_thread_id") == child_thread_id
        assert child_run.get("created_by_run_id") == run_id
        assert child_run.get("status") == "completed"
        assert child_run.get("turn_id") != turn_id
        wrong_thread_response = await e2e_client.get(
            f"/api/v1/agents/threads/{thread_id}/runs/{child_subagent_run['run_id']}",
            headers=e2e_headers,
        )
        assert wrong_thread_response.status_code == 404, wrong_thread_response.text
        await _assert_child_stream(e2e_client, e2e_headers, child_thread_id, child_subagent_run["run_id"])
        assert child_state_payload.get("items"), child_state_payload
        child_messages_text = json.dumps(child_state_payload["items"], ensure_ascii=False, default=str)
        assert all(
            marker in child_messages_text for marker in ("read_file", parent_input_path, "write_file", output_path)
        ), {
            "message": "子智能体未执行父文件读取到子产物写入链路",
            "subagent_run": completed_run,
            "messages": child_state_payload["items"],
        }
        child_read_call_ids = _find_named_tool_call_ids(child_state_payload["items"], "read_file")
        child_read_results = _find_tool_result_contents(child_state_payload["items"], child_read_call_ids)
        assert child_read_call_ids and any(expected_content in content for content in child_read_results), {
            "message": "子智能体 read_file 未从共享 Project Workdir 读到父智能体写入的真实内容",
            "tool_call_ids": sorted(child_read_call_ids),
            "tool_results": child_read_results,
        }
        child_execute_call_ids = _find_named_tool_call_ids(child_state_payload["items"], "execute")
        child_execute_results = _find_tool_result_contents(child_state_payload["items"], child_execute_call_ids)
        assert child_execute_call_ids and any(runtime_content in content for content in child_execute_results), {
            "message": "子智能体未从共享 Project Workdir 读取标记",
            "tool_call_ids": sorted(child_execute_call_ids),
            "tool_results": child_execute_results,
        }

        # 父工具结果允许包含子任务链接；隔离由 item 的执行归属和持久身份证明。
        assert public_items and all(item["yuxi"]["run_id"] == run_id for item in public_items)
        assert all(item["turn_id"] == turn_id for item in public_items)
        assert {item["id"] for item in public_items}.isdisjoint(
            item["id"] for item in child_state_payload["items"]
        )

        history_text = json.dumps(history_payload, ensure_ascii=False)
        start_results = _find_tool_result_contents(history_payload["items"], {completed_run["id"]})
        assert any(json.loads(result).get("run_id") == completed_run["run_id"] for result in start_results)
        assert child_thread_id in history_text
        assert "write_file" in history_text and parent_input_path in history_text
        assert "execute" in history_text and runtime_marker in history_text
        assert "read_file" in history_text and output_path in history_text

        assert (
            await _read_viewer_file(e2e_client, e2e_headers, thread_id, output_viewer_path)
        ).strip() == expected_content
        parent_content = await _read_viewer_file(e2e_client, e2e_headers, thread_id, parent_input_viewer_path)
        assert parent_content.strip() == expected_content

        tree_response = await e2e_client.get(
            "/api/viewer/filesystem/tree",
            params={"thread_id": thread_id, "path": "/outputs"},
            headers=e2e_headers,
        )
        _assert_ok(tree_response)
        assert output_viewer_path in json.dumps(tree_response.json(), ensure_ascii=False)

        viewer_file_response = await e2e_client.get(
            "/api/viewer/filesystem/file",
            params={"thread_id": thread_id, "path": output_viewer_path},
            headers=e2e_headers,
        )
        _assert_ok(viewer_file_response)
        assert expected_content in json.dumps(viewer_file_response.json(), ensure_ascii=False)
        artifact_response = await e2e_client.get(
            f"/api/v1/agents/threads/{thread_id}/artifacts/{output_path.lstrip('/')}",
            headers=e2e_headers,
        )
        _assert_ok(artifact_response)
        assert artifact_response.content.decode("utf-8").strip() == expected_content
        run_completed = True

    finally:
        if not run_completed and thread_id and turn_id:
            await e2e_client.post(
                f"/api/v1/agents/threads/{thread_id}/events",
                json={
                    "events": [
                        {"type": "agent.session.input.cancel", "yuxi": {"turn_id": turn_id, "expected_run_id": run_id}}
                    ]
                },
                headers={**e2e_headers, "Idempotency-Key": f"subagent-cancel-{uuid.uuid4().hex}"},
            )
        if thread_id:
            for path in (parent_input_viewer_path, output_viewer_path):
                if path:
                    await e2e_client.delete(
                        "/api/viewer/filesystem/file",
                        params={"thread_id": thread_id, "path": path},
                        headers=e2e_headers,
                    )
        if thread_id and run_completed:
            archive_response = await e2e_client.post(
                f"/api/v1/agents/threads/{thread_id}/archive", headers=e2e_headers
            )
            assert archive_response.status_code == 200, archive_response.text
        for slug in reversed(created_agents):
            await delete_agent(e2e_client, e2e_headers, slug)
