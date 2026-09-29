"""真实 HTTP 与 PostgreSQL 下 Turn 结果只来自明确绑定的 Run。"""

import os
import uuid

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test.live_api_cleanup import make_test_conversation_title
from yuxi.services.agents.scope import ActorScope
from yuxi.services.agents.turns import get_turn_snapshot

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_turn_result_follows_only_its_bound_run(test_client, admin_headers, standard_user):
    """相邻 Turn 的输出与 Run 不得代替本轮最终结果。"""
    agents = await test_client.get("/api/agent", headers=admin_headers)
    assert agents.status_code == 200, agents.text
    agent = next(item for item in agents.json()["agents"] if item.get("is_default"))
    agent_slug = agent.get("slug") or agent["agent_id"]
    created = await test_client.post(
        "/api/v1/agents/threads",
        json={"agent_id": agent_slug, "title": make_test_conversation_title("turn-result")},
        headers={**admin_headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 200, created.text
    thread_id = created.json()["thread_id"]
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    uid = str(me.json()["uid"])
    turn_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    conn = await asyncpg.connect(os.environ["POSTGRES_URL"].replace("+asyncpg", ""))
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        conversation_id = await conn.fetchval("SELECT id FROM conversations WHERE thread_id = $1", thread_id)
        assert conversation_id
        async with conn.transaction():
            for turn_id, run_id, content in zip(turn_ids, run_ids, ("first output", "second output")):
                await conn.execute(
                    "INSERT INTO agent_turns (id, conversation_thread_id, uid, status, created_at) "
                    "VALUES ($1, $2, $3, 'completed', NOW())",
                    turn_id, thread_id, uid,
                )
                await conn.execute(
                    "INSERT INTO agent_runs "
                    "(id, conversation_thread_id, runtime_scope_id, agent_slug, uid, turn_id, status, "
                    "run_type, source, channel, input_payload, token_usage, origin_metadata, conversation_id) "
                    "VALUES ($1, $2, $2, $3, $4, $5, 'completed', 'chat', 'public_api', 'api', "
                    "'{}'::jsonb, '{}'::jsonb, '{}'::jsonb, $6)",
                    run_id, thread_id, agent_slug, uid, turn_id, conversation_id,
                )
                message_id = await conn.fetchval(
                    "INSERT INTO messages (conversation_id, role, content, run_id, turn_id, delivery_status) "
                    "VALUES ($1, 'assistant', $2, $3, $4, 'complete') RETURNING id",
                    conversation_id, content, run_id, turn_id,
                )
                await conn.execute("UPDATE agent_runs SET output_message_id = $2 WHERE id = $1", run_id, message_id)
                await conn.execute(
                    "UPDATE agent_turns SET current_run_id = $2, result_run_id = $2 WHERE id = $1",
                    turn_id, run_id,
                )

        url = f"/api/v1/agents/threads/{thread_id}/turns/{turn_ids[1]}"
        result = await test_client.get(url, headers=admin_headers)
        assert result.status_code == 200, result.text
        assert result.json()["result_run_id"] == run_ids[1]
        assert result.json()["output"]["content"] == "second output"
        assert (await test_client.get(url, headers=standard_user["headers"])).status_code == 404

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_turns SET result_run_id = $2 WHERE id = $1", turn_ids[1], run_ids[0]
                )

        wrong_message_id = await conn.fetchval("SELECT output_message_id FROM agent_runs WHERE id = $1", run_ids[0])
        await conn.execute("UPDATE agent_runs SET output_message_id = $2 WHERE id = $1", run_ids[1], wrong_message_id)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            with pytest.raises(ValueError, match="结果消息归属不一致"):
                await get_turn_snapshot(
                    db=db, scope=ActorScope(uid=uid, app_id=None), thread_id=thread_id, turn_id=turn_ids[1]
                )
    finally:
        async with conn.transaction():
            await conn.execute(
                "UPDATE agent_turns SET current_run_id = NULL, result_run_id = NULL WHERE id = ANY($1::text[])",
                turn_ids,
            )
            await conn.execute("DELETE FROM messages WHERE run_id = ANY($1::text[])", run_ids)
            await conn.execute("DELETE FROM agent_runs WHERE id = ANY($1::text[])", run_ids)
            await conn.execute("DELETE FROM agent_turns WHERE id = ANY($1::text[])", turn_ids)
        await conn.close()
        await engine.dispose()
