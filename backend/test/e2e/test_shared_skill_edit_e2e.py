"""共享 Skill 编辑后由真实 worker 加载新内容。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import asyncpg
import httpx
import pytest
from e2e_helpers import (
    RUN_TIMEOUT_SECONDS,
    archive_public_thread,
    delete_agent,
    iter_public_thread_events,
    postgres_dsn,
)
from yuxi.modules.extensions.skills.projection import get_user_skills_root_dir

from test.live_api_cleanup import make_test_session_title, remove_e2e_thread_storage

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(360)]


async def test_edited_shared_skill_is_loaded_by_next_run(e2e_client: httpx.AsyncClient, e2e_headers: dict[str, str]):
    """从 HTTP 编辑到 worker Run，回读实际投影与持久运行清单。"""
    slug = f"pytest-run-skill-{uuid.uuid4().hex[:8]}"
    agent_slug = f"pytest-run-agent-{uuid.uuid4().hex[:8]}"
    original = f"---\nname: {slug}\nslug: {slug}\ndescription: before\n---\n# Before\n"
    marker = f"UPDATED_SHARED_SKILL_{uuid.uuid4().hex}"
    updated = original.replace(
        "description: before", "description: after\ntool_dependencies:\n- present_artifacts"
    ).replace("# Before", f"# 图片生成技能\n{marker}")
    me = await e2e_client.get("/api/auth/me", headers=e2e_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    provider_created = False
    skill_created = False
    agent_created = False
    thread_id = None
    run_id = None
    turn_id = None
    provider_id = f"ci-skill-replay-{uuid.uuid4().hex[:8]}"
    try:
        prepared = await e2e_client.post(
            "/api/skills/import/prepare",
            headers=e2e_headers,
            files={"file": ("SKILL.md", original.encode(), "text/markdown")},
        )
        assert prepared.status_code == 200, prepared.text
        draft_id = prepared.json()["data"]["draft_id"]
        confirmed = await e2e_client.post(
            f"/api/skills/install-drafts/{draft_id}/confirm",
            headers=e2e_headers,
            json={"slugs": [slug], "share_config": None},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["data"][0]["success"] is True
        skill_created = True

        saved = await e2e_client.put(
            f"/api/system/skills/{slug}/file",
            headers=e2e_headers,
            json={
                "path": "SKILL.md",
                "content": updated,
                "expected_revision": hashlib.sha256(original.encode()).hexdigest(),
            },
        )
        assert saved.status_code == 200, saved.text

        provider = await e2e_client.post(
            "/api/system/model-providers",
            headers=e2e_headers,
            json={
                "provider_id": provider_id,
                "display_name": "Skill edit deterministic replay",
                "provider_type": "openai",
                "base_url": "http://api:8765/v1",
                "api_key": "ci-replay-key",
                "capabilities": ["chat"],
                "enabled_models": [
                    {"id": "deterministic-chat", "display_name": "Replay", "type": "chat", "source": "manual"}
                ],
                "is_enabled": True,
            },
        )
        assert provider.status_code == 200, provider.text
        provider_created = True
        agent = await e2e_client.post(
            "/api/agent",
            headers=e2e_headers,
            json={
                "name": agent_slug,
                "slug": agent_slug,
                "backend_id": "ChatbotAgent",
                "visibility": "shared",
                "description": "共享 Skill 编辑后加载测试",
                "config_json": {
                    "context": {
                        "model": f"{provider_id}:deterministic-chat",
                        "system_prompt": "不要调用工具，只输出 DETERMINISTIC_AGENT_E2E_OK。",
                        "tools": [],
                        "knowledges": [],
                        "mcps": [],
                        "skills": [slug],
                        "preload_skills": [slug],
                    }
                },
                "share_config": {
                    "version": 2,
                    "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
                    "manage_scope": None,
                },
            },
        )
        assert agent.status_code == 200, agent.text
        agent_created = True
        thread = await e2e_client.post(
            "/api/v1/agents/sessions",
            headers={**e2e_headers, "Idempotency-Key": f"skill-edit-thread-{uuid.uuid4()}"},
            json={"agent_id": agent_slug, "title": make_test_session_title("shared-skill-edit")},
        )
        assert thread.status_code == 201, thread.text
        thread_id = str(thread.json()["id"])
        run = await e2e_client.post(
            f"/api/v1/agents/sessions/{thread_id}/events",
            headers={**e2e_headers, "Idempotency-Key": f"skill-edit-input-{uuid.uuid4()}"},
            json={
                "events": [
                    {
                        "type": "agent.session.input.message",
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "只输出 DETERMINISTIC_AGENT_E2E_OK",
                                    }
                                ],
                            }
                        ],
                        "yuxi": {"mode": "follow_up"},
                    }
                ]
            },
        )
        assert run.status_code == 202, run.text
        run_id = str(run.json()["run_id"])
        turn_id = str(run.json()["turn_id"])
        async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
            async for event in iter_public_thread_events(e2e_client, e2e_headers, thread_id):
                if event.get("turn_id") == turn_id and event["type"] in {
                    "agent.session.turn.completed",
                    "agent.session.turn.failed",
                    "agent.session.turn.cancelled",
                }:
                    break
            else:
                pytest.fail("共享 Skill 的 Thread SSE 在 Turn 终态前断开")
        turn = await e2e_client.get(
            f"/api/v1/agents/sessions/{thread_id}/turns/{turn_id}",
            headers=e2e_headers,
        )
        assert turn.status_code == 200, turn.text
        assert turn.json()["status"] == "completed", turn.text
        assert turn.json()["yuxi"]["result_run_id"] == run_id, turn.text
        assert get_user_skills_root_dir(uid).joinpath(slug, "SKILL.md").read_text(encoding="utf-8") == updated

        conn = await asyncpg.connect(postgres_dsn())
        try:
            raw_manifest = await conn.fetchval("SELECT manifest FROM agent_runs WHERE id = $1", run_id)
        finally:
            await conn.close()
        manifest = json.loads(raw_manifest) if isinstance(raw_manifest, str) else raw_manifest
        assert [item["slug"] for item in manifest["resources"]["skills"]] == [slug]
        assert (
            manifest["resources"]["skills"][0]["preload_content_hash"] == hashlib.sha256(updated.encode()).hexdigest()
        )
    finally:
        if thread_id:
            await archive_public_thread(e2e_client, e2e_headers, thread_id, turn_id=turn_id)
            remove_e2e_thread_storage(thread_id)
        if agent_created:
            await delete_agent(e2e_client, e2e_headers, agent_slug)
        if provider_created:
            response = await e2e_client.delete(f"/api/system/model-providers/{provider_id}", headers=e2e_headers)
            assert response.status_code in {200, 404}, response.text
        if skill_created:
            response = await e2e_client.delete(f"/api/system/skills/{slug}", headers=e2e_headers)
            assert response.status_code in {200, 404}, response.text
