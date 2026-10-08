import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { Client } from "../dist/client.js";

async function withServer(handler, run) {
  const server = createServer(handler).listen(0, "127.0.0.1");
  await once(server, "listening");
  try { return await run(`http://127.0.0.1:${server.address().port}`); }
  finally { server.close(); await once(server, "close"); }
}

function json(response, status, value) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(value));
}

test("uses Public v1 knowledge paths, bodies, and bearer authentication", async () => {
  const seen = [];
  await withServer((request, response) => {
    const record = { method: request.method, url: request.url, auth: request.headers.authorization };
    seen.push(record);
    let body = "";
    request.on("data", chunk => { body += chunk; });
    request.on("end", () => {
      record.body = body;
      if (request.url.endsWith("/list_kbs")) return json(response, 200, [{ kb_id: "kb 1", name: "Handbook" }]);
      if (request.url.endsWith("/search_file")) return json(response, 200, { files: [] });
      if (request.url.endsWith("/query_kb")) return json(response, 200, { kb_id: "kb 1", results: [] });
      if (request.url.endsWith("/open_kb_document")) return json(response, 200, { content: "ok" });
      if (request.url.endsWith("/find_kb_document")) return json(response, 200, { windows: [] });
      json(response, 404, { detail: "unexpected path" });
    });
  }, async url => {
    const client = new Client({ name: "test", url, apiKey: "yxkey_test" });
    assert.deepEqual(await client.kbList(), [{ kb_id: "kb 1", name: "Handbook" }]);
    await client.kbFiles("kb 1", "hand book");
    await client.kbQuery("kb 1", "what?");
    await client.kbOpen("kb 1", "file/1");
    await client.kbFind("kb 1", "file/1", ["needle"]);
  });
  assert.deepEqual(seen.map(item => [item.method, item.url]), [
    ["GET", "/api/v1/knowledge/tools/list_kbs"],
    ["POST", "/api/v1/knowledge/tools/search_file"],
    ["POST", "/api/v1/knowledge/tools/query_kb"],
    ["POST", "/api/v1/knowledge/tools/open_kb_document"],
    ["POST", "/api/v1/knowledge/tools/find_kb_document"],
  ]);
  assert.ok(seen.every(item => item.auth === "Bearer yxkey_test"));
  assert.deepEqual(JSON.parse(seen[1].body), { kb_id: "kb 1", query: "hand book", offset: 0, limit: 100 });
  assert.deepEqual(JSON.parse(seen[2].body), { kb_id: "kb 1", query_text: "what?" });
  assert.deepEqual(JSON.parse(seen[3].body), { kb_id: "kb 1", file_id: "file/1", offset: 0, window_size: 200 });
  assert.deepEqual(JSON.parse(seen[4].body), { kb_id: "kb 1", file_id: "file/1", patterns: ["needle"], use_regex: false, case_sensitive: false, max_windows: 5, window_size: 80 });
});

test("maps structured HTTP errors without hiding the server status", async () => {
  await withServer((_request, response) => json(response, 422, { detail: { error: "invalid_input", message: "bad request" } }), async url => {
    const client = new Client({ name: "test", url, apiKey: "yxkey_test" });
    await assert.rejects(() => client.request("/error"), error => error.status === 422 && error.code === "invalid_input" && error.message === "invalid_input: bad request");
  });
});

test("uses the server status when a structured error omits its code and message", async () => {
  await withServer((_request, response) => json(response, 503, { detail: {} }), async url => {
    const client = new Client({ name: "test", url });
    await assert.rejects(() => client.request("/error"), error => error.status === 503 && error.message === "Service Unavailable");
  });
});
