"""通过 Public Thread 验证预加载工具、执行限制和定时任务的真实 worker 链路。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid

import asyncpg
import httpx
import pytest

from test.e2e.e2e_helpers import archive_public_thread, delete_agent, postgres_dsn
from test.e2e.test_agent_lifecycle_e2e import output_text
from test.live_api_cleanup import make_test_session_title
from yuxi.infrastructure.runtime_settings import get_skill_projection_dir
from yuxi.modules.workspace.paths import workspace_uid_dirname

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(240)]

OUTPUT = "DETERMINISTIC_AGENT_E2E_OK"
MODEL = "ci-replay:deterministic-chat"
TOOL = "present_artifacts"
TOOL_CALL_ID = "call-preloaded-tool"
TOOL_RESULT = "已将交付物展示给用户"
TOOL_ERROR_MARKER = "DETERMINISTIC_TOOL_ERROR"


async def test_public_run_persists_preloaded_tool_and_model_audit(e2e_client, e2e_headers):
    """冷启动预加载工具后，Public 结果、审计和 PG 因果归属一致。"""
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    provider_created = await _provider(e2e_client, e2e_headers)
    slug = thread_id = turn_id = None
    try:
        slug = await _agent(e2e_client, e2e_headers, uid)
        projection_root = get_skill_projection_dir() / workspace_uid_dirname(uid)
        shutil.rmtree(projection_root, ignore_errors=True)
        assert not projection_root.exists()

        created = await _create_thread(e2e_client, e2e_headers, slug, "preloaded-audit")
        thread_id, turn_id, run_id = created["thread_id"], created["turn_id"], created["run_id"]
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == run_id
        assert output_text(turn["output"]) == OUTPUT
        assert projection_root.is_dir(), "worker 应在工具运行前物化用户 Skill 投影"

        run_response = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}/runs/{run_id}", headers=e2e_headers)
        assert run_response.status_code == 200, run_response.text
        run = run_response.json()
        assert run["status"] == "completed"
        assert run["turn_id"] == turn_id and run["input_id"] == created["input_id"]
        assert run["output"] == turn["output"]

        history_response = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}/history", headers=e2e_headers)
        assert history_response.status_code == 200, history_response.text
        history = history_response.json()["items"]
        tool_call = next(item for item in history if item["type"] == "function_call")
        tool_result = next(item for item in history if item["type"] == "function_call_output")
        assert tool_call["yuxi"]["run_id"] == run_id and tool_call["turn_id"] == turn_id
        assert (tool_call["call_id"], tool_call["name"], tool_call["status"]) == (TOOL_CALL_ID, TOOL, "completed")
        assert tool_result["call_id"] == TOOL_CALL_ID and TOOL_RESULT in tool_result["output"]
        assert [item["id"] for item in history if item["type"] == "message" and output_text([item]) == OUTPUT] == [
            turn["output"][0]["id"]
        ]

        audits_response = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}/audits", headers=e2e_headers)
        assert audits_response.status_code == 200, audits_response.text
        audits = [item for item in audits_response.json()["audits"] if item["run_id"] == run_id]
        assert [item["type"] for item in audits] == ["ai", "tool", "ai"]
        assert [item["sequence"] for item in audits] == sorted(item["sequence"] for item in audits)
        assert audits[1]["tool_call_id"] == TOOL_CALL_ID and audits[1]["tool_input"] == {"filepaths": []}
        assert audits[1]["source_model_operation_id"] == audits[0]["operation_id"]
        assert TOOL_RESULT in audits[1]["content"]
        assert audits[2]["id"] == turn["output"][0]["yuxi"]["message_id"]

        conn = await asyncpg.connect(postgres_dsn())
        try:
            binding = await conn.fetchrow(
                """
                SELECT input.id AS input_id, input.status AS input_status, input.turn_id AS input_turn_id,
                       input.consumed_run_id, turn.result_run_id, run.output_message_id,
                       output.run_id AS output_run_id, output.turn_id AS output_turn_id,
                       output.content AS output_content, project.workdir_path, run.runtime_scope_id,
                       run.manifest, run.manifest_fingerprint
                FROM agent_runs run
                JOIN agent_turns turn ON turn.id = run.turn_id
                JOIN agent_inputs input ON input.id = run.input_id
                JOIN messages output ON output.id = run.output_message_id
                JOIN sessions agent_session ON agent_session.thread_id = run.thread_id
                JOIN projects project ON project.id = agent_session.project_id
                WHERE run.id = $1
                """,
                run_id,
            )
            assert binding and binding["input_id"] == created["input_id"]
            assert (binding["input_status"], binding["input_turn_id"], binding["consumed_run_id"]) == (
                "consumed",
                turn_id,
                run_id,
            )
            assert (binding["result_run_id"], binding["output_run_id"], binding["output_turn_id"]) == (
                run_id,
                run_id,
                turn_id,
            )
            assert binding["output_message_id"] == turn["output"][0]["yuxi"]["message_id"]
            assert binding["output_content"] == OUTPUT
            assert binding["runtime_scope_id"] == thread_id
            assert str(binding["workdir_path"]).startswith("projects/")

            model_rows = await conn.fetch(
                """
                SELECT id, message_type, execution_status, usage, started_at, finished_at, duration_ms
                FROM messages WHERE run_id = $1 AND role = 'assistant' AND operation_id IS NOT NULL
                ORDER BY sequence
                """,
                run_id,
            )
            assert [row["message_type"] for row in model_rows] == ["model_audit", "text"]
            assert all(row["execution_status"] == "completed" for row in model_rows)
            assert all(row["started_at"] and row["finished_at"] for row in model_rows)
            assert all(row["duration_ms"] is not None and row["duration_ms"] >= 0 for row in model_rows)
            assert all(row["usage"] for row in model_rows)

            tool_row = await conn.fetchrow(
                """
                SELECT audit.execution_status, audit.content, audit.usage, audit.duration_ms,
                       call.langgraph_tool_call_id, call.tool_name, call.status, call.tool_output
                FROM messages audit
                JOIN tool_calls call
                  ON call.id = (audit.extra_metadata->>'compatibility_tool_call_id')::integer
                WHERE audit.run_id = $1 AND audit.message_type = 'tool_audit'
                """,
                run_id,
            )
            assert tool_row and tool_row["execution_status"] == "completed"
            assert tool_row["usage"] is None and tool_row["duration_ms"] >= 0
            assert (tool_row["langgraph_tool_call_id"], tool_row["tool_name"], tool_row["status"]) == (
                TOOL_CALL_ID,
                TOOL,
                "success",
            )
            assert TOOL_RESULT in tool_row["content"] and tool_row["tool_output"]

            manifest = binding["manifest"]
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            assert manifest["agent"] == {"slug": slug, "backend_id": "ChatbotAgent"}
            assert manifest["model"] == {"spec": MODEL}
            assert [skill["slug"] for skill in manifest["resources"]["skills"]] == ["image-gen"]
            assert (
                binding["manifest_fingerprint"]
                == hashlib.sha256(
                    json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            attempts = await conn.fetch(
                "SELECT attempt_no, outcome, finished_at FROM agent_run_attempts WHERE run_id = $1",
                run_id,
            )
            assert len(attempts) == 1 and attempts[0]["outcome"] == "completed"
            assert attempts[0]["attempt_no"] == 1 and attempts[0]["finished_at"]
        finally:
            await conn.close()
    finally:
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        if slug:
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)


async def test_standard_user_run_uses_admin_execution_limit(e2e_client, e2e_headers):
    """普通用户的失败与成功执行都使用管理员配置的步数上限。"""
    departments = await e2e_client.get("/api/departments", headers=e2e_headers)
    assert departments.status_code == 200, departments.text
    password = f"Pw!{uuid.uuid4().hex}"
    created = await e2e_client.post(
        "/api/auth/users",
        headers=e2e_headers,
        json={
            "username": f"pytest_limit_{uuid.uuid4().hex[:6]}",
            "password": password,
            "role": "user",
            "department_id": departments.json()[0]["id"],
        },
    )
    assert created.status_code == 200, created.text
    user = created.json()
    slug = None
    threads: list[tuple[str, str]] = []
    provider_created = False
    user_headers = None
    try:
        login = await e2e_client.post("/api/auth/token", data={"username": user["uid"], "password": password})
        assert login.status_code == 200, login.text
        user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        provider_created = await _provider(e2e_client, e2e_headers)
        slug = await _agent(e2e_client, e2e_headers, str(user["uid"]))

        for limit, expected_status in ((1, "failed"), (42, "completed")):
            updated = await e2e_client.put(
                f"/api/agent/{slug}",
                headers=e2e_headers,
                json={"config_json": {"context": {"max_execution_steps": limit}}},
            )
            assert updated.status_code == 200, updated.text
            receipt = await _create_thread(e2e_client, user_headers, slug, f"execution-limit-{limit}")
            thread_id, turn_id, run_id = receipt["thread_id"], receipt["turn_id"], receipt["run_id"]
            threads.append((thread_id, turn_id))
            turn = await _terminal_turn(e2e_client, user_headers, thread_id, turn_id)
            assert turn["status"] == expected_status, turn
            assert turn["current_run_id"] == run_id

            conn = await asyncpg.connect(postgres_dsn())
            try:
                row = await conn.fetchrow(
                    "SELECT status, error_message, manifest FROM agent_runs WHERE id = $1", run_id
                )
                assert row and row["status"] == expected_status
                manifest = row["manifest"]
                if isinstance(manifest, str):
                    manifest = json.loads(manifest)
                assert manifest["limits"]["max_execution_steps"] == limit
                if limit == 1:
                    assert "Recursion limit of 1 reached" in row["error_message"]
                    assert turn["output"] is None
                else:
                    assert turn["result_run_id"] == run_id
                    assert output_text(turn["output"]) == OUTPUT
            finally:
                await conn.close()
    finally:
        if user_headers:
            for thread_id, turn_id in threads:
                await archive_public_thread(e2e_client, user_headers, thread_id, turn_id=turn_id)
        if slug:
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)
        deleted = await e2e_client.delete(f"/api/auth/users/{user['id']}", headers=e2e_headers)
        assert deleted.status_code in {200, 404}, deleted.text


async def test_scheduled_task_run_now_reaches_exact_thread_and_turn(e2e_client, e2e_headers):
    """定时任务立即运行复用 Public 输入链，并返回准确的 Thread、Turn 和结果。"""
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    provider_created = await _provider(e2e_client, e2e_headers)
    slug = directory_name = project_id = job_id = thread_id = turn_id = None
    try:
        slug = await _agent(e2e_client, e2e_headers, str(me.json()["uid"]))
        directory_name = f"pytest-scheduled-e2e-{uuid.uuid4().hex[:10]}"
        directory = await e2e_client.post(
            "/api/workspace/directory",
            headers=e2e_headers,
            json={"parent_path": "/", "name": directory_name},
        )
        assert directory.status_code == 200, directory.text
        project = await e2e_client.post(
            "/api/projects",
            headers=e2e_headers,
            json={
                "request_id": f"scheduled-project-{uuid.uuid4()}",
                "name": f"pytest scheduled {uuid.uuid4().hex[:8]}",
                "workdir": {"mode": "linked", "path": directory_name},
            },
        )
        assert project.status_code == 200, project.text
        project_id = str(project.json()["id"])
        job_response = await e2e_client.post(
            "/api/scheduled-tasks",
            headers=e2e_headers,
            json={
                "request_id": f"scheduled-create-{uuid.uuid4()}",
                "name": make_test_session_title("scheduled-agent"),
                "project_id": project_id,
                "agent_slug": slug,
                "prompt": f"只输出 {OUTPUT}",
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
                "model_spec": MODEL,
            },
        )
        assert job_response.status_code == 200, job_response.text
        job_id = str(job_response.json()["id"])
        executed = await e2e_client.post(
            f"/api/scheduled-tasks/{job_id}/run-now",
            headers=e2e_headers,
            json={"request_id": f"scheduled-run-{uuid.uuid4()}"},
        )
        assert executed.status_code == 200, executed.text
        execution = executed.json()
        thread_id, turn_id, run_id = execution["thread_id"], execution["turn_id"], execution["run_id"]
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == run_id and output_text(turn["output"]) == OUTPUT

        thread = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}", headers=e2e_headers)
        assert thread.status_code == 200, thread.text
        assert thread.json()["project_id"] == project_id
        history_response = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}/history", headers=e2e_headers)
        assert history_response.status_code == 200, history_response.text
        history = history_response.json()["items"]
        assert any(item["id"] == turn["output"][0]["id"] and item["yuxi"]["run_id"] == run_id for item in history)

        jobs_response = await e2e_client.get("/api/scheduled-tasks", headers=e2e_headers)
        assert jobs_response.status_code == 200, jobs_response.text
        job = next(item for item in jobs_response.json()["jobs"] if item["id"] == job_id)
        saved = next(item for item in job["runs"] if item["run_id"] == run_id)
        assert saved["status"] == "completed"
        assert saved["thread_id"] == thread_id and saved["turn_id"] == turn_id
        assert saved["session_available"] is True

        conn = await asyncpg.connect(postgres_dsn())
        try:
            row = await conn.fetchrow(
                """
                SELECT scheduled.id AS scheduled_id, input.id AS input_id, input.source,
                       input.external_id, input.consumed_run_id, run.thread_id,
                       run.turn_id, turn.result_run_id, output.content
                FROM scheduled_agent_runs scheduled
                JOIN agent_inputs input ON input.id = scheduled.input_id
                JOIN agent_runs run ON run.id = input.consumed_run_id
                JOIN agent_turns turn ON turn.id = run.turn_id
                JOIN messages output ON output.id = run.output_message_id
                WHERE scheduled.id = $1
                """,
                execution["id"],
            )
            assert row and row["scheduled_id"] == execution["id"]
            assert row["input_id"] == execution["input_id"]
            assert (row["source"], row["external_id"], row["consumed_run_id"]) == (
                "scheduled_agent",
                execution["id"],
                run_id,
            )
            assert (row["thread_id"], row["turn_id"], row["result_run_id"]) == (thread_id, turn_id, run_id)
            assert row["content"] == OUTPUT
        finally:
            await conn.close()
    finally:
        if job_id:
            deleted = await e2e_client.delete(f"/api/scheduled-tasks/{job_id}", headers=e2e_headers)
            assert deleted.status_code in {200, 404}, deleted.text
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        if project_id:
            deleted = await e2e_client.delete(f"/api/projects/{project_id}", headers=e2e_headers)
            assert deleted.status_code in {200, 404}, deleted.text
        if directory_name:
            deleted = await e2e_client.delete(
                "/api/workspace/file", headers=e2e_headers, params={"path": f"/{directory_name}"}
            )
            assert deleted.status_code in {200, 404}, deleted.text
        if slug:
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)


async def test_tool_error_is_persisted_by_tool_message(e2e_client, e2e_headers):
    """ToolNode 受控错误进入 ToolMessage 审计和原始 ToolCall 错误。"""
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    provider_created = await _provider(e2e_client, e2e_headers)
    slug = thread_id = turn_id = None
    try:
        slug = await _agent(e2e_client, e2e_headers, str(me.json()["uid"]), suffix=TOOL_ERROR_MARKER)
        created = await _create_thread(e2e_client, e2e_headers, slug, "tool-error")
        thread_id, turn_id, run_id = created["thread_id"], created["turn_id"], created["run_id"]
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == run_id and output_text(turn["output"]) == OUTPUT

        audits_response = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}/audits", headers=e2e_headers)
        assert audits_response.status_code == 200, audits_response.text
        tool_audits = [
            item
            for item in audits_response.json()["audits"]
            if item["run_id"] == run_id and item["message_type"] == "tool_audit"
        ]
        assert len(tool_audits) == 1
        audit = tool_audits[0]
        assert audit["execution_status"] == "failed"
        assert audit["tool_call_id"] == TOOL_CALL_ID and audit["tool_name"] == TOOL
        assert audit["error_message"] and audit["duration_ms"] >= 0

        conn = await asyncpg.connect(postgres_dsn())
        try:
            row = await conn.fetchrow(
                """
                SELECT audit.execution_status, audit.content, audit.duration_ms,
                       audit.turn_id, audit.run_id, call.status AS call_status, call.error_message
                FROM messages audit
                JOIN tool_calls call
                  ON call.id = (audit.extra_metadata->>'compatibility_tool_call_id')::integer
                WHERE audit.run_id = $1 AND audit.message_type = 'tool_audit'
                """,
                run_id,
            )
            assert row and row["execution_status"] == "failed"
            assert row["run_id"] == run_id and row["turn_id"] == turn_id
            assert row["duration_ms"] is not None and row["duration_ms"] >= 0
            assert row["content"] and row["call_status"] == "error" and row["error_message"]
        finally:
            await conn.close()
    finally:
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        if slug:
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)


async def _provider(client: httpx.AsyncClient, headers: dict[str, str]) -> bool:
    """注册本地确定性重放模型，返回本测试是否拥有供应商。"""
    response = await client.post(
        "/api/system/model-providers",
        headers=headers,
        json={
            "provider_id": "ci-replay",
            "display_name": "CI deterministic replay",
            "provider_type": "openai",
            "base_url": "http://api:8765/v1",
            "api_key": "ci-replay-key",
            "capabilities": ["chat"],
            "enabled_models": [
                {"id": "deterministic-chat", "display_name": "Deterministic chat", "type": "chat", "source": "manual"}
            ],
            "is_enabled": True,
        },
    )
    if response.status_code == 200:
        from test.e2e.e2e_helpers import wait_model_provider_cache

        await wait_model_provider_cache()
        return True
    assert response.status_code == 400 and response.json().get("detail") == "供应商 ci-replay 已存在", response.text
    return False


async def _delete_provider(client: httpx.AsyncClient, headers: dict[str, str], created: bool) -> None:
    """只清理由当前测试创建的模型供应商。"""
    if created:
        response = await client.delete("/api/system/model-providers/ci-replay", headers=headers)
        assert response.status_code in {200, 404}, response.text


async def _agent(client: httpx.AsyncClient, headers: dict[str, str], uid: str, *, suffix: str = "") -> str:
    """创建仅向指定用户共享、预加载 image-gen 的测试 Agent。"""
    slug = f"ci-lifecycle-ext-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/agent",
        headers=headers,
        json={
            "name": f"Lifecycle extended {slug[-8:]}",
            "slug": slug,
            "backend_id": "ChatbotAgent",
            "visibility": "shared",
            "description": "扩展生命周期 E2E",
            "config_json": {
                "context": {
                    "model": MODEL,
                    "system_prompt": f"不要调用工具，只输出 {OUTPUT}。{suffix}",
                    "tools": [],
                    "knowledges": [],
                    "mcps": [],
                    "skills": ["image-gen"],
                    "preload_skills": ["image-gen"],
                }
            },
            "share_config": {
                "version": 2,
                "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
                "manage_scope": None,
            },
        },
    )
    assert response.status_code == 200, response.text
    return slug


async def _create_thread(client: httpx.AsyncClient, headers: dict[str, str], slug: str, tag: str) -> dict:
    """用 Public 创建 Thread，并原子接收首批文本输入。"""
    response = await client.post(
        "/api/v1/agents/threads",
        headers={**headers, "Idempotency-Key": f"{tag}-{uuid.uuid4().hex}"},
        json={
            "agent_id": slug,
            "title": make_test_session_title(tag),
            "model_spec": MODEL,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": f"只输出 {OUTPUT}"}]}],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["input_id"] and result["turn_id"] and result["run_id"]
    return result


async def _terminal_turn(client: httpx.AsyncClient, headers: dict[str, str], thread_id: str, turn_id: str) -> dict:
    """从 Public 持久快照等待 Turn 终态。"""
    for _ in range(150):
        response = await client.get(f"/api/v1/agents/threads/{thread_id}/turns/{turn_id}", headers=headers)
        assert response.status_code == 200, response.text
        turn = response.json()
        if turn["status"] in {"completed", "failed", "cancelled"}:
            return turn
        await asyncio.sleep(0.2)
    pytest.fail(f"Turn 未在 30 秒内终结: {turn_id}")
