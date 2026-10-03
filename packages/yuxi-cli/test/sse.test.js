import test from "node:test";
import assert from "node:assert/strict";
import { parseSse } from "../dist/sse.js";

test("parses SSE fields and multiline data", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(": heartbeat\nid: 7\nevent: message\ndata: {\n"));
      controller.enqueue(new TextEncoder().encode('data: \"ok\"}\n\n'));
      controller.close();
    },
  });
  assert.deepEqual(await Array.fromAsync(parseSse(body)), [{ id: "7", event: "message", data: '{\n"ok"}' }]);
});
