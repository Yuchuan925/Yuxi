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
      if (request.url.endsWith("/external")) return json(response, 200, { databases: [] });
      if (request.url.includes("/files?")) return json(response, 200, { files: [] });
      if (request.url.endsWith("/retrieve")) return json(response, 200, { body: JSON.parse(body) });
      if (request.url.endsWith("/open?offset=0&limit=200")) return json(response, 200, { content: "ok" });
      if (request.url.endsWith("/find")) return json(response, 200, { body: JSON.parse(body) });
      json(response, 404, { detail: "unexpected path" });
    });
  }, async url => {
    const client = new Client({ name: "test", url, apiKey: "yxkey_test" });
    await client.kbList();
    await client.kbFiles("kb 1", "hand book");
    await client.kbQuery("kb 1", "what?");
    await client.kbOpen("kb 1", "file/1");
    await client.kbFind("kb 1", "file/1", ["needle"]);
  });
  assert.deepEqual(seen.map(item => item.url), [
    "/api/v1/knowledge/databases/external",
    "/api/v1/knowledge/databases/external/kb%201/files?offset=0&limit=100&status=all&query=hand%20book",
    "/api/v1/knowledge/databases/external/kb%201/retrieve",
    "/api/v1/knowledge/databases/external/kb%201/files/file%2F1/open?offset=0&limit=200",
    "/api/v1/knowledge/databases/external/kb%201/files/file%2F1/find",
  ]);
  assert.ok(seen.every(item => item.auth === "Bearer yxkey_test"));
  assert.deepEqual(JSON.parse(seen[2].body), { query: "what?", options: {} });
  assert.deepEqual(JSON.parse(seen[4].body), { patterns: ["needle"], use_regex: false, case_sensitive: false, max_windows: 5, window_size: 80 });
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
