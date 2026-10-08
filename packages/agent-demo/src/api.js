import { normalizeBaseUrl } from "./config.js";

/** 将公开错误转为可读信息，保留状态码用于判断接收结果。 */
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/** 拒绝浏览器 Header 无法携带的终端身份字符。 */
export function validateUserId(id) {
  if (typeof id !== "string" || /[^\x20-\x7e]/.test(id)) {
    throw new ApiError(
      "用户 ID 仅支持可打印 ASCII 字符，请使用英文、数字或符号；显示名称可用中文",
      0,
    );
  }
}

/** 固定一次身份配置，所有 API 和订阅都使用相同凭据。 */
export function createApi(
  config,
  { signal, log = () => {}, identify = () => {}, fetcher = fetch } = {},
) {
  const baseUrl = normalizeBaseUrl(config.baseUrl);
  const credentials = {
    key: config.key.trim(),
    appId: config.appId.trim(),
    userId: config.userId,
  };

  async function request(
    path,
    { method = "GET", body, idempotencyKey, cursor, raw = false } = {},
  ) {
    if (!credentials.key) throw new ApiError("请先在调试台填写 API Key", 401);
    validateUserId(credentials.userId);
    const headers = { Authorization: `Bearer ${credentials.key}` };
    if (credentials.userId) headers["X-End-User-Id"] = credentials.userId;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    if (cursor) headers["Last-Event-ID"] = cursor;
    const started = performance.now();
    let response;
    try {
      response = await fetcher(`${baseUrl}/api/v1/agents${path}`, {
        method,
        headers,
        signal,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        redirect: "error",
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      log({
        kind: "http",
        method,
        path,
        status: "网络错误",
        ms: Math.round(performance.now() - started),
      });
      throw new ApiError(
        "无法连接服务。请检查地址及 Yuxi 是否允许当前网页 Origin 的跨域访问。",
        0,
      );
    }
    log({
      kind: "http",
      method,
      path,
      status: response.status,
      ms: Math.round(performance.now() - started),
      idempotencyKey,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      let detail =
        payload.detail || payload.message || `请求失败（${response.status}）`;
      if (Array.isArray(detail))
        detail = detail.map((item) => item.msg).join("；");
      throw new ApiError(
        typeof detail === "string" ? detail : JSON.stringify(detail),
        response.status,
      );
    }
    const appId = response.headers.get("X-App-Id");
    if (appId && credentials.appId && appId !== credentials.appId) {
      throw new ApiError(
        `Key 绑定的 APP 是 ${appId}，与填写的 APP ID 不一致`,
        403,
      );
    }
    if (appId) identify(appId);
    if (raw) return response;
    return response.json();
  }

  return {
    request,
    listAgents: () => request("/"),
    listThreads: (agentId, offset = 0) =>
      request(
        `/threads?agent_id=${encodeURIComponent(agentId)}&limit=50&offset=${offset}`,
      ),
    history: (id) => request(`/threads/${encodeURIComponent(id)}/history`),
    state: (id) =>
      request(
        `/threads/${encodeURIComponent(id)}/state?include_messages=false&include_relations=false`,
      ),
    events: (id, cursor) =>
      request(`/threads/${encodeURIComponent(id)}/events`, {
        cursor,
        raw: true,
      }),
    download: (id, path) => request(artifactPath(id, path), { raw: true }),
  };
}

/** 对虚拟文件路径逐段编码，保持 Thread 下载边界。 */
export function artifactPath(threadId, path) {
  if (
    typeof path !== "string" ||
    !path.trim() ||
    path.split("/").includes("..") ||
    path.split("/").includes(".")
  ) {
    throw new Error("产物路径无效");
  }
  const encoded = path
    .replace(/^\/+/, "")
    .split("/")
    .map(encodeURIComponent)
    .join("/");
  return `/threads/${encodeURIComponent(threadId)}/artifacts/${encoded}?download=true`;
}

/** 优先采用服务器提供的下载文件名。 */
export function downloadFilename(header, path) {
  const extended = header?.match(/filename\*=UTF-8''([^;]+)/i);
  if (extended) {
    try {
      return decodeURIComponent(extended[1]);
    } catch {
      /* 使用普通文件名。 */
    }
  }
  return (
    header?.match(/filename="([^"]+)"/i)?.[1] ||
    path.split("/").pop() ||
    "download"
  );
}
