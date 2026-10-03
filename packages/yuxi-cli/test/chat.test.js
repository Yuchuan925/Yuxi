import test from "node:test";
import assert from "node:assert/strict";
import { ChatSession } from "../dist/chat.js";

function event(id, type, turnId, extra = {}) {
  return { id, event: type, data: JSON.stringify({ type, event_id: `${id}-logical`, turn_id: turnId, ...extra }) };
}

test("consumes only the current turn, keeps newlines, and ignores settled events", async () => {
  const calls = [];
  let sendCount = 0;
  const client = {
    async send() { sendCount += 1; return { turn_id: `turn-${sendCount}` }; },
    async *events(_threadId, cursor) {
      calls.push(cursor);
      const turn = calls.length === 1 ? "turn-1" : "turn-2";
      yield event("old", "agent.session.turn.output_text.delta", "old-turn", { delta: "old" });
      yield event(`${turn}-1`, "agent.session.turn.output_text.delta", turn, { delta: "第一行\n" });
      yield { ...event(`${turn}-duplicate`, "agent.session.turn.output_text.delta", turn, { delta: "错误重复" }), data: JSON.stringify({ type: "agent.session.turn.output_text.delta", event_id: `${turn}-1-logical`, turn_id: turn, delta: "错误重复" }) };
      yield event(`${turn}-settled`, "yuxi.session.run.settled", turn, { status: "completed" });
      yield event(`${turn}-2`, "agent.session.turn.output_text.delta", turn, { delta: "第二行" });
      yield event(`${turn}-done`, "agent.session.turn.completed", turn);
    },
  };
  const session = new ChatSession(client, "thread");
  const first = []; const firstResult = await session.send("one", text => first.push(text));
  const second = []; const secondResult = await session.send("two", text => second.push(text));
  assert.deepEqual(first, ["第一行\n", "第二行"]);
  assert.deepEqual(second, ["第一行\n", "第二行"]);
  assert.equal(firstResult.status, "completed");
  assert.equal(secondResult.status, "completed");
  assert.equal(calls[0], undefined);
  assert.equal(calls[1], "turn-1-done");
});

test("fails instead of silently completing when the stream ends early", async () => {
  const client = {
    async send() { return { turn_id: "turn" }; },
    async *events() { yield event("one", "agent.session.turn.output_text.delta", "turn", { delta: "partial" }); },
  };
  await assert.rejects(() => new ChatSession(client, "thread").send("message", () => {}), /终态前结束/);
});
