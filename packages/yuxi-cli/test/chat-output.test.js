import test from "node:test";
import assert from "node:assert/strict";
import { isTurnTerminal } from "../dist/chat-output.js";

test("only turn terminal events end a chat response", () => {
  assert.equal(isTurnTerminal("yuxi.session.run.settled"), false);
  assert.equal(isTurnTerminal("agent.session.turn.completed"), true);
});

