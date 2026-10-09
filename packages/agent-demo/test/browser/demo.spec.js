import { test, expect } from "@playwright/test";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { networkInterfaces } from "node:os";

const config = (baseUrl) => ({
  baseUrl,
  layout: "phone",
  activeAppId: "alpha",
  apps: [
    {
      id: "alpha",
      name: "Alpha APP",
      appId: "alpha",
      key: "test-alpha-key",
      userId: "",
      users: [
        { id: "", name: "默认用户" },
        { id: "customer-2", name: "小林" },
      ],
    },
    {
      id: "beta",
      name: "Beta APP",
      appId: "beta",
      key: "test-beta-key",
      userId: "",
      users: [{ id: "", name: "默认用户" }],
    },
  ],
});
const message = (id, role, text, status = "completed") => ({
  id,
  type: "message",
  role,
  status,
  phase: role === "assistant" && status === "completed" ? "final_answer" : null,
  content: [{ type: role === "user" ? "input_text" : "output_text", text }],
  yuxi: {
    message_id: Number(id.split("-").at(-1)),
    run_id: role === "assistant" ? "run-1" : null,
    created_at: "2026-10-08T09:00:00Z",
  },
});

/** 用独立 HTTP fixture 验证浏览器实际发送的协议与渲染结果。 */
async function fixture() {
  const requests = [];
  const streams = new Set();
  const state = {
    snapshot: {
      thread_id: "thread-1",
      title: "历史对话",
      agent_id: "chat",
      current_turn: null,
      queued_input_count: 0,
      queue_paused: false,
    },
    items: [
      message("input-1", "user", "历史问题"),
      message("output-2", "assistant", "历史回答"),
    ],
    runs: [],
    artifacts: [],
    failNext: false,
    delayAlpha: false,
    oldHistoryDelay: false,
    eventSeq: 0,
    subscriptionCount: 0,
  };
  const receipts = new Map();
  const server = createServer(async (req, res) => {
    res.setHeader(
      "Access-Control-Allow-Origin",
      req.headers.origin || "http://localhost:5180",
    );
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    res.setHeader(
      "Access-Control-Allow-Headers",
      "Authorization,Content-Type,Idempotency-Key,X-End-User-Id,Last-Event-ID",
    );
    res.setHeader(
      "Access-Control-Expose-Headers",
      "X-App-Id,Content-Disposition",
    );
    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return;
    }
    const app =
      req.headers.authorization === "Bearer test-alpha-key" ? "alpha" : "beta";
    const user = req.headers["x-end-user-id"] || "";
    res.setHeader("X-App-Id", app);
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : undefined;
    requests.push({
      method: req.method,
      path: req.url,
      headers: req.headers,
      body,
    });
    const url = new URL(req.url, "http://fixture");
    const json = (value, status = 200) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(value));
    };
    if (url.pathname === "/api/v1/agents/") {
      if (state.delayAlpha && app === "alpha")
        await new Promise((resolve) => setTimeout(resolve, 450));
      json({
        data: [
          {
            id: "chat",
            name: `${app} Agent`,
            description: "可以聊天和调用工具",
          },
          { id: "research", name: "研究 Agent", description: "研究问题" },
        ],
      });
    } else if (
      url.pathname === "/api/v1/agents/sessions" &&
      req.method === "GET"
    ) {
      json(app === "alpha" && !user ? [state.snapshot] : []);
    } else if (url.pathname.endsWith("/history")) {
      const snapshot = structuredClone({
        thread: state.snapshot,
        items: state.items,
        runs: state.runs,
      });
      if (state.oldHistoryDelay) {
        state.oldHistoryDelay = false;
        await new Promise((resolve) => setTimeout(resolve, 450));
      }
      json(snapshot);
    } else if (url.pathname.endsWith("/state")) {
      json({ agent_state: { artifacts: state.artifacts, todos: [] } });
    } else if (url.pathname.endsWith("/events") && req.method === "GET") {
      state.subscriptionCount += 1;
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.write(": connected\n\n");
      streams.add(res);
      res.on("close", () => streams.delete(res));
      if (state.snapshot.current_turn?.status === "running") {
        const item = state.items.find((entry) => entry.role === "assistant");
        emit({ type: "agent.session.turn.item.added", item });
        emit({
          type: "agent.session.turn.output_text.delta",
          item_id: item.id,
          content_index: 0,
          delta: "你好，",
        });
      }
    } else if (
      url.pathname === "/api/v1/agents/sessions" &&
      req.method === "POST"
    ) {
      const key = req.headers["idempotency-key"];
      if (!receipts.has(key)) {
        state.snapshot = {
          ...state.snapshot,
          title: body.title,
          current_turn: {
            turn_id: "turn-1",
            run_id: "run-1",
            status: "running",
            result_run_id: null,
          },
        };
        state.items = [
          message("input-1", "user", body.input[0].content[0].text),
          message("output-2", "assistant", "", "in_progress"),
        ];
        receipts.set(key, { thread_id: "thread-1", input_id: "input-1" });
      }
      if (state.failNext) {
        state.failNext = false;
        json({ detail: "暂时无法返回回执" }, 503);
      } else json(receipts.get(key));
    } else if (url.pathname.endsWith("/events") && req.method === "POST") {
      const event = body.events[0];
      if (event.type === "agent.session.input.message") {
        state.items.push(
          message("input-3", "user", event.input[0].content[0].text),
        );
        state.snapshot.queued_input_count = 1;
      } else if (event.type === "agent.session.input.cancel") {
        state.snapshot.current_turn.status = "cancelled";
        state.snapshot.queue_paused = true;
        state.items.find((item) => item.role === "assistant").status =
          "incomplete";
        emit({ type: "agent.session.turn.cancelled" });
      } else if (event.type === "yuxi.session.input.continue") {
        state.snapshot.queue_paused = false;
      } else if (event.type === "yuxi.session.input.resume") {
        state.snapshot.current_turn.status = "running";
        state.snapshot.current_turn.waitpoint = null;
      }
      json({ input_id: "input-3", status: "accepted" }, 202);
    } else if (url.pathname.includes("/artifacts/")) {
      res.writeHead(200, {
        "Content-Type": "text/plain",
        "Content-Disposition":
          "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.txt",
      });
      res.end("真实下载字节\n");
    } else json({ detail: "未注册 fixture 路径" }, 404);
  });
  await new Promise((resolve) => server.listen(0, "0.0.0.0", resolve));

  function emit(event) {
    state.eventSeq += 1;
    const cursor = `v2-cursor-${state.eventSeq}`;
    const payload = {
      session_id: "thread-1",
      event_id: `event-${state.eventSeq}`,
      ...event,
    };
    for (const response of streams)
      response.write(
        `id: ${cursor}\nevent: ${event.type}\ndata: ${JSON.stringify(payload)}\n\n`,
      );
    return cursor;
  }

  return {
    requests,
    state,
    emit,
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    disconnect: () => {
      for (const stream of streams) stream.end();
    },
    close: async () => {
      for (const stream of streams) stream.end();
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

let backend;
test.beforeEach(async ({ page }) => {
  backend = await fixture();
  await page.addInitScript((settings) => {
    if (!localStorage.getItem("yuxi-agent-demo:v1"))
      localStorage.setItem("yuxi-agent-demo:v1", JSON.stringify(settings));
  }, config(backend.baseUrl));
});
test.afterEach(async () => {
  await backend?.close();
});

test("桌面手机、平板和真实手机布局，配置持久化", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("选择 Agent")).toHaveValue("chat");
  const phone = await page.locator(".device-frame").boundingBox();
  expect(phone.width / phone.height).toBeCloseTo(9 / 16, 2);
  await page.getByRole("button", { name: "平板 4:3" }).click();
  const tablet = await page.locator(".device-frame").boundingBox();
  expect(tablet.width / tablet.height).toBeCloseTo(4 / 3, 2);
  await expect(page.getByLabel("对话列表", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "平板 4:3" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "关闭调试台" }).click();
  const mobile = await page.locator(".device-frame").boundingBox();
  expect(mobile.width).toBe(390);
  expect(mobile.height).toBe(844);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    390,
  );
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await expect(page.getByLabel("对话列表", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭对话列表" }).click();
  await page.getByRole("button", { name: "打开调试台" }).click();
  await expect(page.getByLabel("调试工作台", { exact: true })).toBeVisible();
});

test("历史回读、流式正文、生成中追加消息和携带身份下载文件", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("历史回答");
  await page.getByRole("button", { name: "新对话", exact: true }).click();
  await page.getByLabel("消息输入").fill("第一条消息");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("你好，");
  await expect(page.locator(".agent-status")).toContainText("正在回复");
  await page.getByLabel("消息输入").fill("追加消息");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("追加消息");
  const post = backend.requests.find(
    (request) =>
      request.body?.events?.[0]?.type === "agent.session.input.message",
  );
  expect(post.body.events[0].yuxi.mode).toBe("follow_up");
  expect(post.headers.authorization).toBe("Bearer test-alpha-key");
  expect(post.headers["idempotency-key"]).toBeTruthy();
  backend.state.items[1] = message("output-2", "assistant", "你好，已完成");
  backend.state.snapshot.current_turn = {
    turn_id: "turn-1",
    run_id: "run-1",
    result_run_id: "run-1",
    status: "completed",
  };
  backend.state.artifacts = ["/home/gem/user-data/报告 #1.txt"];
  backend.emit({
    type: "agent.session.turn.item.done",
    item: backend.state.items[1],
  });
  backend.emit({ type: "agent.session.turn.completed" });
  await expect(page.getByLabel("对话消息")).toContainText("你好，已完成");
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "报告 #1.txt 点击下载" }).click();
  const file = await downloading;
  expect(file.suggestedFilename()).toBe("报告.txt");
  expect(await readFile(await file.path(), "utf8")).toBe("真实下载字节\n");
  const downloadRequest = backend.requests.find((request) =>
    request.path.includes("/artifacts/"),
  );
  expect(downloadRequest.path).toContain(
    "%E6%8A%A5%E5%91%8A%20%231.txt?download=true",
  );
  expect(downloadRequest.headers.authorization).toBe("Bearer test-alpha-key");
});

test("APP 与用户切换清空旧消息，所有调用使用新的身份", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("历史回答");
  await page.getByRole("tab", { name: "用户", exact: true }).click();
  await page.getByRole("button", { name: "小 小林 customer-2" }).click();
  await expect(page.getByLabel("对话消息")).not.toContainText("历史回答");
  await expect
    .poll(() =>
      backend.requests.some(
        (request) =>
          request.path === "/api/v1/agents/" &&
          request.headers["x-end-user-id"] === "customer-2",
      ),
    )
    .toBe(true);
  await page.getByRole("tab", { name: "连接", exact: true }).click();
  await page.getByRole("button", { name: "B Beta APP beta" }).click();
  await expect(page.getByLabel("选择 Agent")).toContainText("beta Agent");
  const last = backend.requests
    .filter((request) => request.path === "/api/v1/agents/")
    .at(-1);
  expect(last.headers.authorization).toBe("Bearer test-beta-key");
  expect(last.headers["x-end-user-id"]).toBeUndefined();
});

test("局域网 HTTP 手机可以启动、发送、取消和新增 APP", async ({ page }) => {
  const address = Object.values(networkInterfaces())
    .flat()
    .find((item) => item.family === "IPv4" && !item.internal)?.address;
  expect(address, "需要真实局域网 IPv4 以验证不安全 HTTP Origin").toBeTruthy();
  const settings = config(backend.baseUrl.replace("127.0.0.1", address));
  await page.addInitScript(
    (value) =>
      localStorage.setItem("yuxi-agent-demo:v1", JSON.stringify(value)),
    settings,
  );
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`http://${address}:5180`);
  expect(
    await page.evaluate(() => ({
      secure: isSecureContext,
      randomUUID: typeof crypto.randomUUID,
    })),
  ).toEqual({ secure: false, randomUUID: "undefined" });
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  await page.getByLabel("消息输入").fill("局域网消息");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("局域网消息");
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect
    .poll(() =>
      backend.requests.some(
        (request) =>
          request.body?.events?.[0]?.type === "agent.session.input.cancel",
      ),
    )
    .toBe(true);
  const keys = backend.requests
    .filter((request) => request.method === "POST")
    .map((request) => request.headers["idempotency-key"]);
  expect(keys).toHaveLength(2);
  expect(new Set(keys).size).toBe(2);
  for (const key of keys)
    expect(key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  await page.getByRole("button", { name: "打开调试台" }).click();
  await page.getByRole("button", { name: "新增", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          JSON.parse(localStorage.getItem("yuxi-agent-demo:v1")).apps.length,
      ),
    )
    .toBe(3);
  expect(errors).toEqual([]);
});

test("非法用户 ID 不切换或保存，中文显示名称配 ASCII ID 可以连接", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  await page.getByRole("tab", { name: "用户", exact: true }).click();
  await page.getByLabel("显示名称").fill("用户甲");
  await page.getByLabel("用户 ID", { exact: true }).fill("用户甲");
  await page.getByRole("button", { name: "新增并切换" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "用户 ID 仅支持可打印 ASCII",
  );
  const stored = await page.evaluate(
    () => JSON.parse(localStorage.getItem("yuxi-agent-demo:v1")).apps[0],
  );
  expect(stored.userId).toBe("");
  expect(stored.users).toHaveLength(2);
  await page.getByLabel("用户 ID", { exact: true }).fill("customer-3");
  await page.getByRole("button", { name: "新增并切换" }).click();
  await expect
    .poll(() =>
      backend.requests.some(
        (request) => request.headers["x-end-user-id"] === "customer-3",
      ),
    )
    .toBe(true);
  await expect(
    page.getByRole("button", { name: /用户甲 customer-3/ }),
  ).toBeVisible();
});

test("APP 切换期间迟到的旧目录响应不能覆盖新 APP", async ({ page }) => {
  backend.state.delayAlpha = true;
  await page.goto("/");
  await page.getByRole("button", { name: "B Beta APP beta" }).click();
  await expect(page.getByLabel("选择 Agent")).toContainText("beta Agent");
  await page.waitForTimeout(600);
  await expect(page.getByLabel("选择 Agent")).not.toContainText("alpha Agent");
});

test("已接收但回执失败的输入手动重试复用同一幂等键", async ({ page }) => {
  backend.state.failNext = true;
  await page.goto("/");
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  await page.getByLabel("消息输入").fill("只能保存一次");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("消息输入")).toBeDisabled();
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByLabel("对话消息")).toContainText("只能保存一次");
  const creates = backend.requests.filter(
    (request) =>
      request.method === "POST" && request.path === "/api/v1/agents/sessions",
  );
  expect(creates).toHaveLength(2);
  expect(creates[0].headers["idempotency-key"]).toBe(
    creates[1].headers["idempotency-key"],
  );
  expect(creates[0].body).toEqual(creates[1].body);
  expect(
    backend.state.items.filter((item) => item.role === "user"),
  ).toHaveLength(1);
});

test("等待回答阻止普通发送，恢复提交完整等待点身份", async ({ page }) => {
  backend.state.snapshot.current_turn = {
    turn_id: "turn-1",
    run_id: "run-1",
    status: "waiting",
    waitpoint: {
      id: "wait-1",
      kind: "answer",
      questions: [{ question_id: "q-1", question: "你的目标是什么？" }],
    },
  };
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  await expect(page.getByLabel("消息输入")).toBeDisabled();
  await page.getByLabel("你的目标是什么？").fill("展示对话能力");
  await page.getByRole("button", { name: "提交并继续" }).click();
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  const resume = backend.requests.find(
    (request) =>
      request.body?.events?.[0]?.type === "yuxi.session.input.resume",
  );
  expect(resume.body.events[0]).toEqual({
    type: "yuxi.session.input.resume",
    turn_id: "turn-1",
    waitpoint_id: "wait-1",
    response: {
      type: "answer",
      answers: [{ question_id: "q-1", answer: "展示对话能力" }],
    },
  });
});

test("取消固定 Turn / Run，暂停队列可显式继续", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  await page.getByLabel("消息输入").fill("运行一轮");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(
    page.getByRole("button", { name: "停止", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByRole("button", { name: "继续队列" })).toBeVisible();
  const cancel = backend.requests.find(
    (request) =>
      request.body?.events?.[0]?.type === "agent.session.input.cancel",
  );
  expect(cancel.body.events[0].yuxi).toEqual({
    turn_id: "turn-1",
    expected_run_id: "run-1",
  });
  await page.getByRole("button", { name: "继续队列" }).click();
  await expect(page.getByLabel("消息输入")).toBeEnabled();
});

test("断线续订带 cursor，resync 回读持久结果并忽略迟到增量", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  await page.getByLabel("消息输入").fill("断线恢复");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("你好，");
  const cursor = backend.emit({
    type: "agent.session.turn.output_text.done",
    item_id: "output-2",
    content_index: 0,
    text: "完成的正文",
  });
  await expect(page.getByLabel("对话消息")).toContainText("完成的正文");
  backend.disconnect();
  backend.state.items[1] = message("output-2", "assistant", "完成的正文");
  backend.state.snapshot.current_turn.status = "completed";
  await expect.poll(() => backend.state.subscriptionCount).toBe(2);
  const resumed = backend.requests
    .filter(
      (request) => request.method === "GET" && request.path.endsWith("/events"),
    )
    .at(-1);
  expect(resumed.headers["last-event-id"]).toBe(cursor);
  backend.emit({ type: "yuxi.session.resync" });
  backend.emit({
    type: "agent.session.turn.output_text.delta",
    item_id: "output-2",
    content_index: 0,
    delta: "不应追加",
  });
  await expect(page.getByLabel("对话消息")).toContainText("完成的正文");
  await expect(page.getByLabel("对话消息")).not.toContainText("不应追加");
});

test("中文 Markdown 产物链接只编码一次并下载正确文件", async ({ page }) => {
  backend.state.items[1] = message(
    "output-2",
    "assistant",
    "[下载报告](/home/gem/user-data/报告%20%231.txt)",
  );
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  const downloading = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载报告" }).click();
  const download = await downloading;
  expect(await readFile(await download.path(), "utf8")).toBe("真实下载字节\n");
  expect(
    backend.requests.find((request) => request.path.includes("/artifacts/"))
      .path,
  ).toContain("/%E6%8A%A5%E5%91%8A%20%231.txt?download=true");
});

test("窄屏抽屉限制键盘焦点，Escape 关闭并归还焦点", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "打开调试台" });
  await trigger.click();
  await expect(page.getByRole("button", { name: "关闭调试台" })).toBeFocused();
  for (let index = 0; index < 20; index += 1) {
    await page.keyboard.press("Tab");
    // 原生 dialog 允许焦点进入浏览器 chrome，但不能进入背景控件。
    expect(
      await page.evaluate(
        () =>
          document.activeElement === document.body ||
          !!document.activeElement.closest("dialog"),
      ),
    ).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(
    page.getByLabel("调试工作台", { exact: true }),
  ).not.toBeVisible();
  await expect(trigger).toBeFocused();
});

test("旧 history 迟到不能把新的等待状态回退为 running", async ({ page }) => {
  backend.state.snapshot.current_turn = {
    turn_id: "turn-1",
    run_id: "run-1",
    status: "running",
  };
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  await page.getByRole("tab", { name: /^事件/ }).click();
  backend.state.oldHistoryDelay = true;
  const oldRequest = page.waitForRequest((request) =>
    request.url().endsWith("/history"),
  );
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await oldRequest;
  backend.state.snapshot.current_turn = {
    turn_id: "turn-1",
    run_id: "run-1",
    status: "waiting",
    waitpoint: {
      id: "wait-new",
      kind: "answer",
      questions: [{ question_id: "q-1", question: "新问题" }],
    },
  };
  backend.emit({ type: "yuxi.session.turn.waiting" });
  await expect(page.getByLabel("新问题")).toBeVisible();
  await page.getByLabel("新问题").fill("保留我的回答");
  await page.waitForTimeout(600);
  await expect(page.getByLabel("消息输入")).toBeDisabled();
  await expect(page.getByLabel("新问题")).toHaveValue("保留我的回答");
});

test("审批恢复保留所有 call ID 和用户选择", async ({ page }) => {
  backend.state.snapshot.current_turn = {
    turn_id: "turn-1",
    run_id: "run-1",
    status: "waiting",
    waitpoint: {
      id: "wait-approval",
      kind: "approval",
      calls: [
        {
          call_id: "call-1",
          name: "write_file",
          args: { path: "report.txt" },
          allowed_decisions: ["approve", "reject"],
        },
        {
          call_id: "call-2",
          name: "send_message",
          args: {},
          allowed_decisions: ["reject"],
        },
      ],
    },
  };
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  await expect(page.getByLabel("消息输入")).toBeDisabled();
  await page.getByLabel("write_file").selectOption("approve");
  await page.getByLabel("send_message").selectOption("reject");
  await page.getByRole("button", { name: "提交并继续" }).click();
  await expect(page.getByLabel("消息输入")).toBeEnabled();
  const event = backend.requests.find(
    (request) =>
      request.body?.events?.[0]?.type === "yuxi.session.input.resume",
  ).body.events[0];
  expect(event.response).toEqual({
    type: "approval",
    decisions: [
      { call_id: "call-1", decision: "approve" },
      { call_id: "call-2", decision: "reject" },
    ],
  });
});

test("失败原因来自当前 Run，公开调试记录隐藏 Key", async ({ page }) => {
  backend.state.snapshot.current_turn = {
    turn_id: "turn-1",
    run_id: "run-1",
    status: "failed",
  };
  backend.state.runs = [
    { run_id: "run-1", error_message: "Insufficient Balance" },
  ];
  await page.goto("/");
  await page.getByRole("button", { name: "打开对话列表" }).click();
  await page.getByRole("button", { name: "历史对话" }).click();
  await expect(page.locator(".turn-notice")).toContainText(
    "Insufficient Balance",
  );
  await expect.poll(() => backend.state.subscriptionCount).toBe(1);
  backend.emit({
    type: "yuxi.session.turn.capability_limited",
    message: "测试脱敏 test-alpha-key",
  });
  await page.getByRole("tab", { name: /^事件/ }).click();
  const event = page
    .locator(".event-entry")
    .filter({ hasText: "yuxi.session.turn.capability_limited" });
  await event.locator("summary").click();
  await expect(event.locator("pre")).toContainText("[已隐藏]");
  await expect(event.locator("pre")).not.toContainText("test-alpha-key");
});
