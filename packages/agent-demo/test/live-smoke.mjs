import assert from "node:assert/strict";
import { chromium } from "@playwright/test";

const key = process.env.DEMO_API_KEY;
if (!key)
  throw new Error("真实链路验证需要 DEMO_API_KEY（绑定 APP 的 agents Key）");
const baseUrl = process.env.DEMO_BASE_URL || "http://localhost:5173";
const demoUrl = process.env.DEMO_URL || "http://localhost:5180";
const secondKey = process.env.DEMO_SECOND_API_KEY;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const headers = { Authorization: `Bearer ${key}` };
let threadId;

/** 回读真实 Public API，错误只输出状态。 */
async function get(path) {
  const response = await fetch(`${baseUrl}/api/v1/agents${path}`, { headers });
  assert.equal(response.status, 200, `Public 回读失败：${response.status}`);
  return response.json();
}

/** 等待数据库投影出现明确的整轮结果。 */
async function awaitResults() {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const history = await get(`/threads/${threadId}/history`);
    if (history.thread.current_turn?.status === "failed") {
      const turn = await get(
        `/threads/${threadId}/turns/${history.thread.current_turn.turn_id}`,
      );
      const failed = turn.runs.find((run) => run.status === "failed");
      const detail = String(failed?.error_message || "")
        .replaceAll(key, "[hidden]")
        .slice(0, 300);
      throw new Error(
        `真实 Agent 执行失败：${failed?.error_type || "unknown"} ${detail}`,
      );
    }
    const final = history.items.filter((item) => item.phase === "final_answer");
    if (
      final.length >= 2 &&
      history.thread.current_turn?.status === "completed" &&
      !history.thread.queued_input_count
    )
      return history;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("120 秒内未观察到两轮持久最终结果");
}

try {
  await page.addInitScript(
    (settings) =>
      localStorage.setItem("yuxi-agent-demo:v1", JSON.stringify(settings)),
    {
      baseUrl,
      layout: "phone",
      activeAppId: "live",
      apps: [
        {
          id: "live",
          name: "真实链路验证",
          appId: "",
          key,
          userId: "",
          users: [
            { id: "", name: "默认用户" },
            { id: `demo-live-other-${crypto.randomUUID()}`, name: "隔离用户" },
          ],
        },
        ...(secondKey
          ? [
              {
                id: "other",
                name: "第二 APP",
                appId: "",
                key: secondKey,
                userId: "",
                users: [{ id: "", name: "默认用户" }],
              },
            ]
          : []),
      ],
    },
  );
  await page.goto(demoUrl);
  await page.getByLabel("消息输入").waitFor({ state: "visible" });
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="消息输入"]').disabled,
  );
  const prompt = `YUXI_TEST_SESSION_DEMO_${crypto.randomUUID().slice(0, 8)} 请只回复 DEMO_FIRST_OK，不调用工具。`;
  const created = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/agents/threads",
  );
  await page.getByLabel("消息输入").fill(prompt);
  await page.getByRole("button", { name: "发送消息" }).click();
  threadId = (await (await created).json()).thread_id;
  assert.ok(threadId);
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="消息输入"]').disabled,
  );
  await page
    .getByLabel("消息输入")
    .fill("请只回复 DEMO_SECOND_OK，不调用工具。");
  await page.getByRole("button", { name: "发送消息" }).click();
  const history = await awaitResults();
  const finalItems = history.items.filter(
    (item) => item.phase === "final_answer",
  );
  assert.ok(
    finalItems.some((item) =>
      item.content.some((part) => part.text?.includes("DEMO_FIRST_OK")),
    ),
  );
  assert.ok(
    finalItems.some((item) =>
      item.content.some((part) => part.text?.includes("DEMO_SECOND_OK")),
    ),
  );
  for (const item of finalItems) {
    const turn = await get(`/threads/${threadId}/turns/${item.turn_id}`);
    assert.equal(turn.status, "completed");
    assert.equal(turn.result_run_id, item.yuxi.run_id);
  }
  await page.getByText("DEMO_SECOND_OK", { exact: true }).waitFor();
  await page.getByRole("tab", { name: "用户", exact: true }).click();
  await page.getByRole("button", { name: /隔离用户/ }).click();
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="消息输入"]').disabled,
  );
  assert.ok(
    !(await page.getByLabel("对话消息").textContent()).includes(
      "DEMO_FIRST_OK",
    ),
  );
  await page.getByRole("button", { name: /默认用户 默认身份/ }).click();
  await page.waitForFunction(
    () => !document.querySelector('[aria-label="消息输入"]').disabled,
  );
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page
    .getByRole("button", { name: new RegExp(prompt.slice(0, 36)) })
    .click();
  await page.getByText("DEMO_SECOND_OK", { exact: true }).waitFor();
  if (secondKey) {
    await page.getByRole("tab", { name: "连接", exact: true }).click();
    await page.getByRole("button", { name: /第二 APP 待识别/ }).click();
    await page.waitForFunction(
      () => !document.querySelector('[aria-label="消息输入"]').disabled,
    );
    assert.ok(
      !(await page.getByLabel("对话消息").textContent()).includes(
        "DEMO_FIRST_OK",
      ),
    );
  }
  console.log(
    JSON.stringify({
      realApi: true,
      workerFinalTurns: finalItems.length,
      browserFinalOutput: true,
      userIsolation: true,
      appSwitch: Boolean(secondKey),
      threadId,
    }),
  );
} finally {
  await browser.close();
  if (threadId) {
    const snapshot = await get(`/threads/${threadId}`);
    if (
      ["completed", "failed", "cancelled"].includes(
        snapshot.current_turn?.status,
      )
    ) {
      await fetch(`${baseUrl}/api/v1/agents/threads/${threadId}/archive`, {
        method: "POST",
        headers,
      });
    }
  }
}
