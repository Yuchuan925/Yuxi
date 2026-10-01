"""真实 Public Thread 链路验证 SubAgent 的权限与独立可观测性。"""

from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg
import httpx
import pytest

from test.e2e.test_agent_lifecycle_e2e import output_text
from e2e_helpers import archive_public_thread, delete_agent, postgres_dsn
from test.live_api_cleanup import make_test_conversation_title
from yuxi.modules.workspace.paths import user_workspace_dir

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(240)]

OUTPUT = "DETERMINISTIC_AGENT_E2E_OK"
MODEL = "ci-replay:deterministic-chat"
WRITE_CALL = "call-subagent-write"


@pytest.mark.parametrize("mode", ["default", "always_trust"])
async def test_subagent_inherits_write_policy_and_shares_workdir(e2e_client, e2e_headers, mode):
    """子 Run 继承父审批模式，并在共享 Project Workdir 内写入文件。"""
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
            json={
                "events": [
                    {
                        "type": "agent.session.input.message",
                        "input": [_message(f"{OUTPUT} SUBAGENT_MODE:{mode} SUBAGENT_PATH:{virtual_path}")],
                        "yuxi": {"mode": "follow_up", "tool_approval_mode": mode},
                    }
                ]
            },
        )
        assert accepted.status_code == 202, accepted.text
        receipt = accepted.json()
        turn_id, parent_run_id = receipt["turn_id"], receipt["run_id"]
        assert receipt["input_id"] and turn_id and parent_run_id
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == parent_run_id and output_text(turn["output"]) == OUTPUT

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
            assert child["status"] == "completed" and child["turn_id"] != turn_id
            assert child["runtime_scope_id"] == child["conversation_thread_id"] and child["output_message_id"]
            payload = _json_object(child["input_payload"])
            manifest = _json_object(child["manifest"])
            assert payload["tool_approval_mode"] == mode and payload["model_spec"] == MODEL
            assert manifest["model"]["spec"] == MODEL

            audit = await conn.fetchrow(
                """
                SELECT execution_status, content, run_id, turn_id, extra_metadata
                FROM messages WHERE run_id = $1 AND message_type = 'tool_audit' AND operation_id = $2
                """,
                child["id"],
                WRITE_CALL,
            )
            model_call = await conn.fetchrow(
                """
                SELECT call.tool_name, call.status FROM tool_calls call
                JOIN messages model ON model.id = call.message_id
                WHERE model.run_id = $1 AND call.langgraph_tool_call_id = $2
                """,
                child["id"],
                WRITE_CALL,
            )
            assert model_call and model_call["tool_name"] == "write_file"
            assert audit and audit["run_id"] == child["id"] and audit["turn_id"] == child["turn_id"]
            assert audit["execution_status"] == "completed"
            assert _json_object(audit["extra_metadata"])["tool_name"] == "write_file"
            assert (
                await conn.fetchval("SELECT count(*) FROM messages WHERE run_id = $1 AND role = 'user'", child["id"])
                == 1
            )
        finally:
            await conn.close()

        child_thread_id = child["conversation_thread_id"]
        child_run = await e2e_client.get(
            f"/api/v1/agents/threads/{child_thread_id}/runs/{child['id']}", headers=e2e_headers
        )
        assert child_run.status_code == 200, child_run.text
        assert child_run.json()["status"] == "completed"
        assert child_run.json()["turn_id"] == child["turn_id"]
        audits = await e2e_client.get(f"/api/v1/agents/threads/{child_thread_id}/audits", headers=e2e_headers)
        assert audits.status_code == 200, audits.text
        tool_audits = [
            item
            for item in audits.json()["audits"]
            if item["run_id"] == child["id"] and item["operation_id"] == WRITE_CALL
        ]
        assert len(tool_audits) == 1
        if tool_audits:
            assert tool_audits[0]["execution_status"] == "completed"
        assert probe_path.parent.is_dir()
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
                "tool_approval_mode": "always_trust",
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
                    by_call = {_json_object(row["input_payload"])["runtime"]["tool_call_id"]: row for row in children}
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
            assert fast["output_message_id"] and fast["turn_id"] != turn_id
            assert slow["turn_id"] != turn_id and slow["conversation_thread_id"] != fast["conversation_thread_id"]
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id = $1", parent_run_id) == "running"

            fast_run = await e2e_client.get(
                f"/api/v1/agents/threads/{fast['conversation_thread_id']}/runs/{fast['id']}",
                headers=e2e_headers,
            )
            assert fast_run.status_code == 200, fast_run.text
            assert fast_run.json()["status"] == "completed"
            assert output_text(fast_run.json()["output"]) == OUTPUT
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
                        if event["type"] == "agent.session.turn.completed" and event["yuxi"]["run_id"] == fast["id"]:
                            assert event["session_id"] == fast["conversation_thread_id"]
                            assert event["turn_id"] == fast["turn_id"] and event["turn"]["status"] == "completed"
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
        assert turn["result_run_id"] == parent_run_id and output_text(turn["output"]) == OUTPUT
        for child in (fast, slow):
            run = await e2e_client.get(
                f"/api/v1/agents/threads/{child['conversation_thread_id']}/runs/{child['id']}",
                headers=e2e_headers,
            )
            assert run.status_code == 200, run.text
            assert run.json()["status"] == "completed" and output_text(run.json()["output"]) == OUTPUT
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
                "tool_approval_mode": "always_trust",
                "input": [_message(f"{OUTPUT} {marker} SUBAGENT_PATH:/tmp/not-written")],
            },
        )
        assert created.status_code == 200, created.text
        receipt = created.json()
        thread_id, turn_id, parent_run_id = receipt["thread_id"], receipt["turn_id"], receipt["run_id"]
        turn = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert turn["status"] == "completed", turn
        assert turn["result_run_id"] == parent_run_id and output_text(turn["output"]) == OUTPUT

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
            assert child["status"] == "failed" and child["turn_id"] != turn_id
            assert marker in child["error_message"] and "Model lifecycle" not in child["error_message"]
            assert child["output_message_id"]

            output = await conn.fetchrow(
                "SELECT run_id, turn_id, content, extra_metadata FROM messages WHERE id = $1",
                child["output_message_id"],
            )
            assert output and (output["run_id"], output["turn_id"]) == (child["id"], child["turn_id"])
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
            assert all((item["run_id"], item["turn_id"]) == (child["id"], child["turn_id"]) for item in audits)

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
        assert all(item["yuxi"]["run_id"] == child["id"] for item in child_run.json()["output"])
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
        from test.e2e.e2e_helpers import wait_model_provider_cache

        await wait_model_provider_cache()
        return True
    assert response.status_code == 400 and response.json().get("detail") == "供应商 ci-replay 已存在", response.text
    return False


async def _delete_provider(client: httpx.AsyncClient, headers: dict[str, str], created: bool) -> None:
    """清理由当前测试创建的模型供应商。"""
    if created:
        response = await client.delete("/api/system/model-providers/ci-replay", headers=headers)
        assert response.status_code in {200, 404}, response.text


async def _agent(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    uid: str,
    *,
    child: bool = False,
    subagent_slug: str | None = None,
    tools: list[str] | None = None,
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
            "config_json": {
                "context": {
                    "model": "" if child else MODEL,
                    "system_prompt": f"不要调用工具，只输出 {OUTPUT}。{marker}",
                    "tools": tools or [],
                    "knowledges": [],
                    "mcps": [],
                    "skills": ["image-gen"],
                    "preload_skills": ["image-gen"],
                    "subagents": [] if child else [subagent_slug],
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


@pytest.mark.parametrize("outcome", ["completed", "worker_lost"])
async def test_parent_finishes_before_child_and_child_keeps_own_owner(e2e_client, e2e_headers, outcome):
    """父先终态，子随后独立完成或失联失败，结果不可串父 Turn。"""
    uid = str((await e2e_client.get("/api/auth/me", headers=e2e_headers)).json()["uid"])
    provider = await _provider(e2e_client, e2e_headers)
    agents, thread_id, turn_id = [], None, None
    gate = str(uuid.uuid4())
    try:
        child_slug = await _agent(e2e_client, e2e_headers, uid, child=True)
        agents.append(child_slug)
        parent_slug = await _agent(e2e_client, e2e_headers, uid, subagent_slug=child_slug)
        agents.append(parent_slug)
        accepted = await e2e_client.post(
            "/api/v1/agents/threads",
            headers={**e2e_headers, "Idempotency-Key": f"background-{gate}"},
            json={
                "agent_id": parent_slug,
                "model_spec": MODEL,
                "tool_approval_mode": "always_trust",
                "title": make_test_conversation_title("child-background"),
                "input": [
                    _message(f"{OUTPUT} SUBAGENT_BACKGROUND_GATE:{gate} SUBAGENT_SLOW SUBAGENT_PATH:/tmp/no-write")
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text
        thread_id, turn_id, parent_id = (accepted.json()[key] for key in ("thread_id", "turn_id", "run_id"))
        parent = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert parent["status"] == "completed" and output_text(parent["output"]) == OUTPUT
        conn = await asyncpg.connect(postgres_dsn())
        try:
            child = await conn.fetchrow(
                "SELECT id,turn_id,conversation_thread_id,status,worker_id FROM agent_runs WHERE created_by_run_id=$1",
                parent_id,
            )
            assert child and child["turn_id"] != turn_id
            child_thread, child_turn = child["conversation_thread_id"], child["turn_id"]
            async with asyncio.timeout(30):
                while not await conn.fetchval(
                    "SELECT worker_id FROM agent_runs WHERE id=$1 AND status='running'", child["id"]
                ):
                    await asyncio.sleep(0.1)
            if outcome == "worker_lost":
                await conn.execute(
                    "UPDATE agent_runs SET lease_expires_at=(now() at time zone 'UTC')-interval '1 minute' WHERE id=$1",
                    child["id"],
                )
                async with asyncio.timeout(60):
                    while await conn.fetchval("SELECT status FROM agent_runs WHERE id=$1", child["id"]) != "failed":
                        await asyncio.sleep(0.2)
            else:
                async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
                    assert (await replay.get("/release-subagent", params={"token": gate})).status_code == 200
            async with asyncio.timeout(60):
                while True:
                    result = await e2e_client.get(
                        f"/api/v1/agents/threads/{child_thread}/turns/{child_turn}", headers=e2e_headers
                    )
                    assert result.status_code == 200, result.text
                    if result.json()["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.2)
            result = result.json()
            if outcome == "completed":
                assert result["status"] == "completed" and result["result_run_id"] == child["id"]
                assert output_text(result["output"]) == OUTPUT
            else:
                assert result["status"] == "failed"
                assert (
                    await conn.fetchval("SELECT error_type FROM agent_runs WHERE id=$1", child["id"])
                    == "worker_lease_expired"
                )
            assert await conn.fetchval("SELECT result_run_id FROM agent_turns WHERE id=$1", turn_id) == parent_id
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id=$1", parent_id) == "completed"
        finally:
            await conn.close()
    finally:
        async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
            await replay.get("/release-subagent", params={"token": gate})
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider)


@pytest.mark.parametrize("wait_kind", ["answer", "approval"])
async def test_child_question_resumes_after_parent_completed(e2e_client, e2e_headers, wait_kind):
    """子等待不借父生命周期，刷新 items 后回答在自身 Turn 创建新 Run。"""
    uid = str((await e2e_client.get("/api/auth/me", headers=e2e_headers)).json()["uid"])
    provider = await _provider(e2e_client, e2e_headers)
    agents, thread_id, turn_id, child_thread, child_turn = [], None, None, None, None
    try:
        child_slug = await _agent(
            e2e_client, e2e_headers, uid, child=True, tools=["ask_user_question"] if wait_kind == "answer" else []
        )
        agents.append(child_slug)
        parent_slug = await _agent(e2e_client, e2e_headers, uid, subagent_slug=child_slug)
        agents.append(parent_slug)
        accepted = await e2e_client.post(
            "/api/v1/agents/threads",
            headers={**e2e_headers, "Idempotency-Key": f"child-question-{uuid.uuid4()}"},
            json={
                "agent_id": parent_slug,
                "model_spec": MODEL,
                "title": make_test_conversation_title("child-question"),
                "input": [
                    _message(
                        f"{OUTPUT} SUBAGENT_BACKGROUND "
                        + ("SUBAGENT_QUESTION_CHILD" if wait_kind == "answer" else "SUBAGENT_APPROVAL_CHILD")
                    )
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text
        thread_id, turn_id, parent_id = (accepted.json()[key] for key in ("thread_id", "turn_id", "run_id"))
        parent = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
        assert parent["status"] == "completed", parent
        conn = await asyncpg.connect(postgres_dsn())
        try:
            child = await conn.fetchrow(
                "SELECT id,turn_id,conversation_thread_id FROM agent_runs WHERE created_by_run_id=$1", parent_id
            )
            assert child and child["turn_id"] != turn_id
            child_thread, child_turn = child["conversation_thread_id"], child["turn_id"]
        finally:
            await conn.close()
        async with asyncio.timeout(30):
            while True:
                result = await e2e_client.get(
                    f"/api/v1/agents/threads/{child_thread}/turns/{child_turn}", headers=e2e_headers
                )
                if result.json()["status"] == "waiting":
                    break
                assert result.json()["status"] not in {"failed", "completed"}, result.text
                await asyncio.sleep(0.2)
        waiting = result.json()
        assert waiting["waitpoint"]["kind"] == wait_kind
        refused = await e2e_client.post(
            f"/api/v1/agents/threads/{child_thread}/events",
            headers={**e2e_headers, "Idempotency-Key": f"waiting-ordinary-{uuid.uuid4()}"},
            json={"events": [{"type": "agent.session.input.message", "input": [_message("must refuse")]}]},
        )
        assert refused.status_code == 409, refused.text
        response = (
            {
                "type": "answer",
                "answers": [
                    {"question_id": q["question_id"], "answer": "yes"} for q in waiting["waitpoint"]["questions"]
                ],
            }
            if wait_kind == "answer"
            else {
                "type": "approval",
                "decisions": [{"call_id": c["call_id"], "decision": "reject"} for c in waiting["waitpoint"]["calls"]],
            }
        )
        history = await e2e_client.get(f"/api/v1/agents/threads/{child_thread}/history", headers=e2e_headers)
        assert history.status_code == 200 and history.json()["items"]
        assert all(item.get("yuxi", {}).get("run_id") in {None, child["id"]} for item in history.json()["items"])
        resume = await e2e_client.post(
            f"/api/v1/agents/threads/{child_thread}/events",
            headers={**e2e_headers, "Idempotency-Key": f"child-answer-{uuid.uuid4()}"},
            json={
                "events": [
                    {
                        "type": "yuxi.session.input.resume",
                        "turn_id": child_turn,
                        "waitpoint_id": waiting["waitpoint"]["id"],
                        "response": response,
                    }
                ]
            },
        )
        assert resume.status_code == 202, resume.text
        assert resume.json()["turn_id"] == child_turn and resume.json()["run_id"] != child["id"]
        completed = await _terminal_turn(e2e_client, e2e_headers, child_thread, child_turn)
        assert completed["status"] == "completed", completed
        assert completed["result_run_id"] == resume.json()["run_id"] and output_text(completed["output"]) == OUTPUT
        history = (await e2e_client.get(f"/api/v1/agents/threads/{child_thread}/history", headers=e2e_headers)).json()
        call = next(
            item
            for item in history["items"]
            if item["type"] == "function_call"
            and item["name"] == ("ask_user_question" if wait_kind == "answer" else "execute")
        )
        result = next(
            item
            for item in history["items"]
            if item["type"] == "function_call_output" and item["yuxi"]["run_id"] == resume.json()["run_id"]
        )
        assert call["status"] == result["status"] == ("completed" if wait_kind == "answer" else "failed")
        if wait_kind == "approval":
            assert "CHILD_APPROVAL_MUST_NOT_EXECUTE" not in (result["output"] or "")
        assert result["yuxi"]["call_item_id"] == call["id"]
        assert result["yuxi"]["call_run_id"] == child["id"]
        assert result["yuxi"]["run_id"] == resume.json()["run_id"]
        parent = await e2e_client.get(f"/api/v1/agents/threads/{thread_id}/turns/{turn_id}", headers=e2e_headers)
        assert parent.json()["result_run_id"] == parent_id and parent.json()["status"] == "completed"
    finally:
        if child_thread:
            snapshot = await e2e_client.get(
                f"/api/v1/agents/threads/{child_thread}/turns/{child_turn}", headers=e2e_headers
            )
            if snapshot.json()["status"] in {"waiting", "running", "cancelling"}:
                await e2e_client.post(
                    f"/api/v1/agents/threads/{child_thread}/events",
                    headers={**e2e_headers, "Idempotency-Key": f"child-cleanup-{uuid.uuid4()}"},
                    json={"events": [{"type": "agent.session.input.cancel", "yuxi": {"turn_id": child_turn}}]},
                )
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider)


@pytest.mark.parametrize("target", ["parent", "child", "unrelated_follow_up"])
async def test_parent_cancel_cascades_and_child_cancel_isolated(e2e_client, e2e_headers, target):
    """真实 worker 在父 await 子模型时验证双向取消边界。"""
    uid = str((await e2e_client.get("/api/auth/me", headers=e2e_headers)).json()["uid"])
    provider = await _provider(e2e_client, e2e_headers)
    agents, thread_id, turn_id, child_thread, child_turn = [], None, None, None, None
    gate = str(uuid.uuid4())
    try:
        child_slug = await _agent(e2e_client, e2e_headers, uid, child=True)
        agents.append(child_slug)
        parent_slug = await _agent(e2e_client, e2e_headers, uid, subagent_slug=child_slug)
        agents.append(parent_slug)
        accepted = await e2e_client.post(
            "/api/v1/agents/threads",
            headers={**e2e_headers, "Idempotency-Key": f"cancel-tree-{gate}"},
            json={
                "agent_id": parent_slug,
                "model_spec": MODEL,
                "tool_approval_mode": "always_trust",
                "title": make_test_conversation_title("cancel-tree"),
                "input": [_message(f"{OUTPUT} SUBAGENT_OBSERVATION_GATE:{gate} SUBAGENT_PATH:/tmp/no-write")],
            },
        )
        assert accepted.status_code == 200, accepted.text
        thread_id, turn_id, parent_id = (accepted.json()[key] for key in ("thread_id", "turn_id", "run_id"))
        conn = await asyncpg.connect(postgres_dsn())
        try:
            async with asyncio.timeout(30):
                while True:
                    children = await conn.fetch(
                        "SELECT id,turn_id,conversation_thread_id,status,input_payload FROM agent_runs "
                        "WHERE created_by_run_id=$1",
                        parent_id,
                    )
                    slow = next(
                        (
                            row
                            for row in children
                            if _json_object(row["input_payload"])["runtime"]["tool_call_id"] == "call-subagent-slow"
                        ),
                        None,
                    )
                    awaiting = await conn.fetchval(
                        "SELECT execution_status FROM messages WHERE run_id=$1 AND operation_id=$2 AND role='tool'",
                        parent_id,
                        "await-call-subagent-slow",
                    )
                    fast_ready = target != "unrelated_follow_up" or any(
                        row["status"] == "completed" for row in children
                    )
                    if slow and slow["status"] == "running" and awaiting == "running" and fast_ready:
                        break
                    await asyncio.sleep(0.1)
            child_thread, child_turn = slow["conversation_thread_id"], slow["turn_id"]
            if target == "unrelated_follow_up":
                fast = next(row for row in children if row["status"] == "completed")
                follow = await e2e_client.post(
                    f"/api/v1/agents/threads/{fast['conversation_thread_id']}/events",
                    headers={**e2e_headers, "Idempotency-Key": f"independent-{gate}"},
                    json={
                        "events": [
                            {
                                "type": "agent.session.input.message",
                                "input": [
                                    _message(
                                        f"{OUTPUT} DETERMINISTIC_CANCEL_FOLLOWUP "
                                        f"DETERMINISTIC_BLOCK_BEFORE_RESPONSE:{gate}"
                                    )
                                ],
                                "yuxi": {"mode": "follow_up"},
                            }
                        ]
                    },
                )
                assert follow.status_code == 202, follow.text
                independent = follow.json()
                assert independent["turn_id"] != fast["turn_id"]
                configuration = await conn.fetchrow(
                    "SELECT input_payload->>'model_spec' AS model, input_payload->>'tool_approval_mode' AS approval "
                    "FROM agent_runs WHERE id=$1",
                    independent["run_id"],
                )
                assert dict(configuration) == {"model": MODEL, "approval": "always_trust"}
                async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
                    async with asyncio.timeout(30):
                        while not (await replay.get("/blocking-started", params={"token": gate})).json()["started"]:
                            await asyncio.sleep(0.1)
                assert (
                    await conn.fetchval("SELECT created_by_run_id FROM agent_runs WHERE id=$1", independent["run_id"])
                    is None
                )
            selected_thread, selected_turn = (child_thread, child_turn) if target == "child" else (thread_id, turn_id)
            cancelled = await e2e_client.post(
                f"/api/v1/agents/threads/{selected_thread}/events",
                headers={**e2e_headers, "Idempotency-Key": f"cancel-{gate}"},
                json={"events": [{"type": "agent.session.input.cancel", "yuxi": {"turn_id": selected_turn}}]},
            )
            assert cancelled.status_code == 202, cancelled.text
            child = await _terminal_turn(e2e_client, e2e_headers, child_thread, child_turn)
            assert child["status"] == "cancelled" and child["result_run_id"] is None
            parent = await _terminal_turn(e2e_client, e2e_headers, thread_id, turn_id)
            assert parent["status"] == ("completed" if target == "child" else "cancelled"), parent
            if target == "child":
                assert parent["result_run_id"] == parent_id and output_text(parent["output"]) == OUTPUT
            else:
                assert parent["result_run_id"] is None
            assert await conn.fetchval("SELECT status FROM agent_runs WHERE id=$1", slow["id"]) == "cancelled"
            if target == "unrelated_follow_up":
                assert (
                    await conn.fetchval("SELECT status FROM agent_turns WHERE id=$1", independent["turn_id"])
                    == "running"
                )
                async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
                    await replay.get("/release-blocking", params={"token": gate})
                completed = await _terminal_turn(
                    e2e_client, e2e_headers, fast["conversation_thread_id"], independent["turn_id"]
                )
                assert completed["status"] == "completed" and completed["result_run_id"] == independent["run_id"]
        finally:
            await conn.close()
    finally:
        async with httpx.AsyncClient(base_url="http://api:8765", timeout=5) as replay:
            await replay.get("/release-subagent", params={"token": gate})
            await replay.get("/release-blocking", params={"token": gate})
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
        for slug in reversed(agents):
            await delete_agent(e2e_client, e2e_headers, slug)
        await _delete_provider(e2e_client, e2e_headers, provider)
