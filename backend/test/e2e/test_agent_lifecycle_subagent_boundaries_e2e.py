"""真实 Public Thread 链路验证 SubAgent 的权限与独立可观测性。"""

from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg
import httpx
import pytest

from e2e_helpers import archive_public_thread, delete_agent, postgres_dsn
from test.live_api_cleanup import make_test_conversation_title
from yuxi.modules.workspace.paths import user_workspace_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(240)]

OUTPUT = "DETERMINISTIC_AGENT_E2E_OK"
MODEL = "ci-replay:deterministic-chat"
WRITE_CALL = "call-subagent-write"


@pytest.mark.parametrize("mode", ["default", "always_trust"])
async def test_subagent_inherits_write_policy_and_shares_workdir(e2e_client, e2e_headers, mode):
    """子 Run 继承父审批模式，拒绝时不得在共享 Workdir 留下文件。"""
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    provider_created = await _provider(e2e_client, e2e_headers)
    agents: list[str] = []
    thread_id = turn_id = probe_path = None
    try:
        child_slug = await _agent(e2e_client, e2e_headers, uid, child=True)
        agents.append(child_slug)
        parent_slug = await _agent(e2e_client, e2e_headers, uid, subagent_slug=child_slug)
        agents.append(parent_slug)
        created = await e2e_client.post(
            "/api/v1/agents/threads",
            headers={**e2e_headers, "Idempotency-Key": f"subagent-policy-{uuid.uuid4().hex}"},
            json={
                "agent_id": parent_slug,
                "title": make_test_conversation_title("subagent-policy"),
                "model_spec": MODEL,
            },
        )
        assert created.status_code == 200, created.text
        thread_id = created.json()["thread_id"]
        conn = await asyncpg.connect(postgres_dsn())
        try:
            workdir_path = await conn.fetchval(
                """
                SELECT project.workdir_path FROM conversations conversation
                JOIN projects project ON project.id = conversation.project_id
                WHERE conversation.thread_id = $1
                """,
                thread_id,
            )
        finally:
            await conn.close()
        assert workdir_path and str(workdir_path).startswith("projects/")
        file_name = f"subagent-policy-{uuid.uuid4().hex}.txt"
        virtual_path = f"/home/gem/user-data/{workdir_path}/{file_name}"
        probe_path = user_workspace_dir(uid) / workdir_path / file_name
        assert not probe_path.exists()

        accepted = await e2e_client.post(
            f"/api/v1/agents/threads/{thread_id}/events",
            headers={**e2e_headers, "Idempotency-Key": f"subagent-input-{uuid.uuid4().hex}"},
            json={"events": [{
                "type": "agent.thread.input.message",
                "mode": "follow_up",
                "tool_approval_mode": mode,
                "input": [_message(f"{OUTPUT} SUBAGENT_MODE:{mode} SUBAGENT_PATH:{virtual_path}")],
            }]},
        )
        assert accepted.status_code == 202, accepted.text
        receipt = accepted.json()
        turn_id, parent_run_id = receipt["turn_id"], receipt["run_id"]
        assert receipt["input_id"] and turn_id and parent_run_id
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == parent_run_id and turn["output"]["content"] == OUTPUT

        conn = await asyncpg.connect(postgres_dsn())
        try:
            children = await conn.fetch(
                """
                SELECT id, status, conversation_thread_id, runtime_scope_id, turn_id,
                       input_payload, manifest, output_message_id
                FROM agent_runs WHERE created_by_run_id = $1 AND run_type = 'subagent'
                """,
                parent_run_id,
            )
            assert len(children) == 1, [dict(row) for row in children]
            child = children[0]
            assert child["status"] == "completed" and child["turn_id"] == turn_id
            assert child["runtime_scope_id"] == thread_id and child["output_message_id"]
            payload = _json_object(child["input_payload"])
            manifest = _json_object(child["manifest"])
            assert payload["tool_approval_mode"] == mode and payload["model_spec"] == MODEL
            assert manifest["model"]["spec"] == MODEL

            audit = await conn.fetchrow(
                """
                SELECT execution_status, content, run_id, turn_id, extra_metadata
                FROM messages WHERE run_id = $1 AND message_type = 'tool_audit' AND operation_id = $2
                """,
                child["id"], WRITE_CALL,
            )
            model_call = await conn.fetchrow(
                """
                SELECT call.tool_name, call.status FROM tool_calls call
                JOIN messages model ON model.id = call.message_id
                WHERE model.run_id = $1 AND call.langgraph_tool_call_id = $2
                """,
                child["id"], WRITE_CALL,
            )
            assert model_call and model_call["tool_name"] == "write_file"
            if mode == "default":
                assert audit is None, "未暴露的工具不得留下执行审计"
            else:
                assert audit and audit["run_id"] == child["id"] and audit["turn_id"] == turn_id
                assert audit["execution_status"] == "completed"
                assert _json_object(audit["extra_metadata"])["tool_name"] == "write_file"
            assert await conn.fetchval(
                "SELECT count(*) FROM messages WHERE run_id = $1 AND role = 'user'", child["id"]
            ) == 1
        finally:
            await conn.close()

        child_thread_id = child["conversation_thread_id"]
        child_run = await e2e_client.get(
            f"/api/v1/agents/threads/{child_thread_id}/runs/{child['id']}", headers=e2e_headers
        )
        assert child_run.status_code == 200, child_run.text
        assert child_run.json()["status"] == "completed"
        assert child_run.json()["turn_id"] == turn_id
        audits = await e2e_client.get(f"/api/v1/agents/threads/{child_thread_id}/audits", headers=e2e_headers)
        assert audits.status_code == 200, audits.text
        tool_audits = [
            item for item in audits.json()["audits"]
            if item["run_id"] == child["id"] and item["operation_id"] == WRITE_CALL
        ]
        assert len(tool_audits) == (0 if mode == "default" else 1)
        if tool_audits:
            assert tool_audits[0]["execution_status"] == "completed"
        assert probe_path.parent.is_dir()
        if mode == "default":
            assert not probe_path.exists(), "被拒绝的子执行不能写入共享 Workdir"
        else:
            assert probe_path.read_text(encoding="utf-8") == "subagent write verified"
    finally:
        if probe_path:
            probe_path.unlink(missing_ok=True)
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)


async def test_child_end_is_public_while_parent_waits_for_slow_child(e2e_client, e2e_headers):
    """父 Run 仍在等待慢子任务时，快子 Run 的 Public SSE 与 PG 终态可读取。"""
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    provider_created = await _provider(e2e_client, e2e_headers)
    agents: list[str] = []
    thread_id = turn_id = None
    gate = str(uuid.uuid4())
    try:
        child_slug = await _agent(e2e_client, e2e_headers, uid, child=True)
        agents.append(child_slug)
        parent_slug = await _agent(e2e_client, e2e_headers, uid, subagent_slug=child_slug)
        agents.append(parent_slug)
        created = await e2e_client.post(
            "/api/v1/agents/threads",
            headers={**e2e_headers, "Idempotency-Key": f"subagent-observe-{uuid.uuid4().hex}"},
            json={
                "agent_id": parent_slug,
                "title": make_test_conversation_title("subagent-observation"),
                "model_spec": MODEL,
                "tool_approval_mode": "default",
                "input": [_message(f"{OUTPUT} SUBAGENT_OBSERVATION_GATE:{gate} SUBAGENT_PATH:/tmp/not-written")],
            },
        )
        assert created.status_code == 200, created.text
        receipt = created.json()
        thread_id, turn_id, parent_run_id = receipt["thread_id"], receipt["turn_id"], receipt["run_id"]

        conn = await asyncpg.connect(postgres_dsn())
        try:
            async with asyncio.timeout(45):
                while True:
                    children = await conn.fetch(
                        """
                        SELECT id, status, conversation_thread_id, input_payload, output_message_id, turn_id
                        FROM agent_runs WHERE created_by_run_id = $1 AND run_type = 'subagent'
                        """,
                        parent_run_id,
                    )
                    by_call = {
                        _json_object(row["input_payload"])["runtime"]["tool_call_id"]: row for row in children
                    }
                    awaiting = await conn.fetchval(
                        """
                        SELECT execution_status FROM messages
                        WHERE run_id = $1 AND message_type = 'tool_audit'
                          AND operation_id = 'await-call-subagent-slow'
                        """,
                        parent_run_id,
                    )
                    if (
                        len(by_call) == 2
                        and by_call["call-subagent-start"]["status"] == "completed"
                        and by_call["call-subagent-slow"]["status"] == "running"
                        and awaiting == "running"
                    ):
                        break
                    await asyncio.sleep(0.2)
            fast, slow = by_call["call-subagent-start"], by_call["call-subagent-slow"]
            assert fast["output_message_id"] and fast["turn_id"] == turn_id
            assert slow["turn_id"] == turn_id and slow["conversation_thread_id"] != fast["conversation_thread_id"]
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id = $1", parent_run_id) == "running"

            fast_run = await e2e_client.get(
                f"/api/v1/agents/threads/{fast['conversation_thread_id']}/runs/{fast['id']}",
                headers=e2e_headers,
            )
            assert fast_run.status_code == 200, fast_run.text
            assert fast_run.json()["status"] == "completed"
            assert fast_run.json()["output"]["content"] == OUTPUT
            parent_run = await e2e_client.get(
                f"/api/v1/agents/threads/{thread_id}/runs/{parent_run_id}", headers=e2e_headers
            )
            assert parent_run.status_code == 200 and parent_run.json()["status"] == "running", parent_run.text

            async with asyncio.timeout(20):
                async with e2e_client.stream(
                    "GET", f"/api/v1/agents/threads/{fast['conversation_thread_id']}/events", headers=e2e_headers
                ) as response:
                    assert response.status_code == 200, await response.aread()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event["type"] == "agent.thread.run.completed" and event["run_id"] == fast["id"]:
                            assert event["thread_id"] == fast["conversation_thread_id"]
                            assert event["turn_id"] == turn_id and event["payload"]["status"] == "completed"
                            break
                    else:
                        pytest.fail("快子 Run 终态未出现在其 Public Thread SSE 中")
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id = $1", parent_run_id) == "running"
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id = $1", slow["id"]) == "running"
        finally:
            await conn.close()

        async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
            released = await replay.get("/release-subagent", params={"token": gate})
            assert released.status_code == 200, released.text
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == parent_run_id and turn["output"]["content"] == OUTPUT
        for child in (fast, slow):
            run = await e2e_client.get(
                f"/api/v1/agents/threads/{child['conversation_thread_id']}/runs/{child['id']}",
                headers=e2e_headers,
            )
            assert run.status_code == 200, run.text
            assert run.json()["status"] == "completed" and run.json()["output"]["content"] == OUTPUT
    finally:
        try:
            async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
                await replay.get("/release-subagent", params={"token": gate})
        except httpx.HTTPError:
            pass
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)


async def test_child_model_retry_exhaustion_is_reported_to_parent(e2e_client, e2e_headers):
    """子模型 429 耗尽留在子 Run，父 await 消费持久失败后仍能完成。"""
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    provider_created = await _provider(e2e_client, e2e_headers)
    agents: list[str] = []
    thread_id = turn_id = None
    marker = "DETERMINISTIC_RATE_LIMIT"
    try:
        child_slug = await _agent(e2e_client, e2e_headers, uid, child=True)
        agents.append(child_slug)
        parent_slug = await _agent(e2e_client, e2e_headers, uid, subagent_slug=child_slug)
        agents.append(parent_slug)
        created = await e2e_client.post(
            "/api/v1/agents/threads",
            headers={**e2e_headers, "Idempotency-Key": f"subagent-retry-{uuid.uuid4().hex}"},
            json={
                "agent_id": parent_slug,
                "title": make_test_conversation_title("subagent-retry"),
                "model_spec": MODEL,
                "tool_approval_mode": "default",
                "input": [_message(f"{OUTPUT} {marker} SUBAGENT_PATH:/tmp/not-written")],
            },
        )
        assert created.status_code == 200, created.text
        receipt = created.json()
        thread_id, turn_id, parent_run_id = receipt["thread_id"], receipt["turn_id"], receipt["run_id"]
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == parent_run_id and turn["output"]["content"] == OUTPUT

        conn = await asyncpg.connect(postgres_dsn())
        try:
            children = await conn.fetch(
                """
                SELECT id, status, conversation_thread_id, turn_id, error_message, output_message_id
                FROM agent_runs WHERE created_by_run_id = $1 AND run_type = 'subagent'
                """,
                parent_run_id,
            )
            assert len(children) == 1, [dict(row) for row in children]
            child = children[0]
            assert child["status"] == "failed" and child["turn_id"] == turn_id
            assert marker in child["error_message"] and "Model lifecycle" not in child["error_message"]
            assert child["output_message_id"]

            output = await conn.fetchrow(
                "SELECT run_id, turn_id, content, extra_metadata FROM messages WHERE id = $1",
                child["output_message_id"],
            )
            assert output and (output["run_id"], output["turn_id"]) == (child["id"], turn_id)
            metadata = _json_object(output["extra_metadata"])
            assert metadata["is_error"] is True and marker in metadata["error_message"]
            assert "Model call failed after" not in output["content"]

            attempts = await conn.fetch(
                "SELECT attempt_no, outcome, finished_at FROM agent_run_attempts WHERE run_id = $1",
                child["id"],
            )
            assert len(attempts) == 1 and attempts[0]["attempt_no"] == 1
            assert attempts[0]["outcome"] == "failed" and attempts[0]["finished_at"]
            audits = await conn.fetch(
                """
                SELECT run_id, turn_id, message_type, operation_id, execution_status FROM messages
                WHERE run_id = $1 AND message_type IN ('model_audit', 'tool_audit')
                ORDER BY sequence
                """,
                child["id"],
            )
            assert audits and any(item["message_type"] == "model_audit" for item in audits)
            assert all(item["operation_id"] and item["execution_status"] != "running" for item in audits)
            assert all((item["run_id"], item["turn_id"]) == (child["id"], turn_id) for item in audits)

            await_audit = await conn.fetchval(
                """
                SELECT content FROM messages WHERE run_id = $1 AND message_type = 'tool_audit'
                  AND operation_id = 'await-call-subagent-start'
                """,
                parent_run_id,
            )
            observed = _json_object(await_audit)
            assert observed["status"] == "failed"
            assert marker in observed["result"]["error"]["message"]
        finally:
            await conn.close()

        child_run = await e2e_client.get(
            f"/api/v1/agents/threads/{child['conversation_thread_id']}/runs/{child['id']}",
            headers=e2e_headers,
        )
        assert child_run.status_code == 200, child_run.text
        assert child_run.json()["status"] == "failed"
        assert marker in child_run.json()["error_message"]
        assert child_run.json()["output"]["id"] == child["output_message_id"]
    finally:
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider_created)


async def _provider(client: httpx.AsyncClient, headers: dict[str, str]) -> bool:
    """注册本地确定性模型服务，并标记供应商清理归属。"""
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
        return True
    assert response.status_code == 400 and response.json().get("detail") == "供应商 ci-replay 已存在", response.text
    return False


async def _delete_provider(client: httpx.AsyncClient, headers: dict[str, str], created: bool) -> None:
    """清理由当前测试创建的模型供应商。"""
    if created:
        response = await client.delete("/api/system/model-providers/ci-replay", headers=headers)
        assert response.status_code in {200, 404}, response.text


async def _agent(
    client: httpx.AsyncClient, headers: dict[str, str], uid: str, *, child: bool = False,
    subagent_slug: str | None = None,
) -> str:
    """创建带确定性模型标记的父或子 Agent。"""
    slug = f"ci-subagent-boundary-{uuid.uuid4().hex[:8]}"
    marker = "DETERMINISTIC_SUBAGENT_CHILD" if child else f"DETERMINISTIC_SUBAGENT_PARENT:{subagent_slug}"
    response = await client.post(
        "/api/agent",
        headers=headers,
        json={
            "name": f"Subagent boundary {slug[-8:]}",
            "slug": slug,
            "backend_id": "SubAgentBackend" if child else "ChatbotAgent",
            "is_subagent": child,
            "description": "SubAgent 边界 E2E",
            "config_json": {"context": {
                "model": "" if child else MODEL,
                "system_prompt": f"不要调用工具，只输出 {OUTPUT}。{marker}",
                "tools": [],
                "knowledges": [],
                "mcps": [],
                "skills": ["image-gen"],
                "preload_skills": ["image-gen"],
                "subagents": [] if child else [subagent_slug],
            }},
            "share_config": {
                "version": 2,
                "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
                "manage_scope": None,
            },
        },
    )
    assert response.status_code == 200, response.text
    return slug


def _message(text: str) -> dict:
    """构建一条 Public 文字输入。"""
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _json_object(value: object) -> dict:
    """读取 asyncpg 返回的 JSON 字段。"""
    return json.loads(value) if isinstance(value, str) else value


async def _terminal_turn(client: httpx.AsyncClient, headers: dict[str, str], thread_id: str, turn_id: str) -> dict:
    """轮询 Public Turn 快照直到持久终态。"""
    for _ in range(150):
        response = await client.get(f"/api/v1/agents/threads/{thread_id}/turns/{turn_id}", headers=headers)
        assert response.status_code == 200, response.text
        turn = response.json()
        if turn["status"] in {"completed", "failed", "cancelled"}:
            return turn
        await asyncio.sleep(0.2)
    pytest.fail(f"Turn 未在 30 秒内终结: {turn_id}")
