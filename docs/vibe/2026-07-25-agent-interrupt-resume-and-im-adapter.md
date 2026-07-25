# Agent Interrupt / Resume 与纯文本 IM 接入方案（讨论稿）

> 日期：2026-07-25
>
> 状态：待讨论，不代表最终接口承诺
>
> 关联议题：[GitHub Issue #837：Feat: 关于集成 IM](https://github.com/xerrors/Yuxi/issues/837)

## 1. 背景

`feat/cli-chat-debug` 已经用 `yuxi chat` 验证了外部客户端可以创建 Agent Call、订阅 Run SSE 并流式展示结果。但当 Agent 触发工具审批或 `ask_user_question` 时，当前外部调用链会暴露一个边界缺口：

1. Run worker 能识别并发布 `ask_user_question_required`、`human_approval_required`、`interrupted` 事件；
2. Web 前端能从事件中的 `questions`、`approval`、`interrupt_info` 渲染交互，并调用内部 Agent Run 接口创建 resume run；
3. Agent Call 对外响应目前只返回 `status`、`output`、`error` 等结果，没有完整、稳定的 interrupt 对象；
4. Agent Call 也没有同一命名空间下的 resume 接口，外部客户端必须了解内部 `/api/agent/runs` 的 `resume` 与 `created_by_run_id` 约定；
5. 因此 CLI 把 `interrupted` 当作运行结束展示后，用户再输入“运行”，这条文本会被当作新的普通消息，而不是对上一个 interrupt 的决议，后端会拒绝继续执行。

这不是单纯的 CLI 渲染问题，而是对外 Agent 生命周期尚未完整覆盖 `running -> interrupted -> resumed -> terminal`。

## 2. 本方案要解决什么

本方案分成两条能力线，底层共享同一个 interrupt 模型。

### 2.1 富客户端

适用于 CLI、Web、SDK 或其他能解析结构化事件的客户端：

- 接收结构化 interrupt；
- 根据 interrupt 类型渲染审批或问题；
- 将结构化决议提交给公开 resume 接口；
- 继续订阅新 run 的流式事件。

第一阶段只要求 CLI 能完成该闭环，不要求先实现 IM。

### 2.2 纯文本 IM

适用于微信等只能收发普通文本、无法依赖按钮或复杂回调的渠道：

- 所有入站消息进入统一的渠道消息入口；
- 在消息进入普通 Agent turn 之前解析控制命令；
- `/approve`、`/reject`、`/answer`、`/cancel` 等命令解析为结构化 interrupt 决议；
- 只有确认不是控制命令或待回答问题的回复后，才把消息作为普通用户消息分发给 Agent。

本阶段先定义机制，不讨论各 IM 的最终卡片、按钮和视觉渲染。

### 2.3 非目标

- 本文不选定首个正式支持的 IM 平台；
- 不在当前 CLI 分支修改 backend；
- 不要求所有渠道使用相同的按钮或卡片；
- 不把 LangGraph 内部 `Command(resume=...)` 结构直接暴露为长期公共协议；
- 不在第一阶段实现跨多个会话的统一人工审批工作台。

## 3. 外部项目调研

调研基于 2026-07-25 获取的源码快照，而非仅依赖产品文档或二手描述。

### 3.1 Hermes Agent

调研版本：[`NousResearch/hermes-agent@8f8b66d`](https://github.com/NousResearch/hermes-agent/tree/8f8b66d8ac6ed5172daa213b615037cae0ed92f9)

#### 3.1.1 审批是会话内的阻塞队列

Hermes 的 [`tools/approval.py`](https://github.com/NousResearch/hermes-agent/blob/8f8b66d8ac6ed5172daa213b615037cae0ed92f9/tools/approval.py) 使用 `session_key -> approval queue` 保存待审批项。每个待审批项有独立的 `threading.Event`，Agent 工作者阻塞等待决议；`/approve` 默认按 FIFO 解除最早一项，`/approve all` 才批量解除。

值得借鉴的点：

- 待审批状态绑定会话，而不是绑定某个临时 UI；
- 等待有超时，超时不是默许；
- `/stop` 等中断会解除等待并按拒绝处理，避免工作线程永远挂起；
- 多个并行子任务产生审批时有明确的 FIFO / all 语义。

需要谨慎的点：仅以 FIFO 省略审批 ID 对单 Agent、本地进程较方便，但对分布式服务、群聊和跨设备操作不够明确。

#### 3.1.2 文本回复必须先经过等待状态解析

Hermes 的 [`gateway/run.py`](https://github.com/NousResearch/hermes-agent/blob/8f8b66d8ac6ed5172daa213b615037cae0ed92f9/gateway/run.py) 在 Agent 已运行时优先检查 `has_blocking_approval(session_key)`。只有存在待审批时，裸文本 `yes`、`approve`、`no` 等才会被映射为审批决议，否则仍是普通对话文本。代码注释直接说明了原因：若把回复排到普通 follow-up 队列，当前 run 又在等待审批，两者会形成死锁，直到审批超时。

这是纯文本渠道最重要的路由原则：

```text
入站消息
  -> 身份与会话解析
  -> 控制命令 / 待回答状态解析
  -> resolve interrupt（命中时到此结束）
  -> busy turn 的 steer / queue / interrupt 策略
  -> 新普通 turn
```

#### 3.1.3 问题既支持按钮，也支持下一条文本

Hermes 的 [`tools/clarify_gateway.py`](https://github.com/NousResearch/hermes-agent/blob/8f8b66d8ac6ed5172daa213b615037cae0ed92f9/tools/clarify_gateway.py) 为问题生成 `clarify_id`，同时维护 `clarify_id -> entry` 和 `session_key -> FIFO ids` 两个索引。富客户端按钮按 ID resolve；无按钮的渠道把同一会话的下一条非命令文本解释为回答。选择题可以输入序号或完整选项，自由文本问题接收普通文本。

[`gateway/platforms/base.py`](https://github.com/NousResearch/hermes-agent/blob/8f8b66d8ac6ed5172daa213b615037cae0ed92f9/gateway/platforms/base.py) 定义了统一 `send_clarify` 降级：支持按钮的平台覆盖渲染，不支持的平台发送编号列表。Slash command 会绕过问题回答拦截，因此 `/stop` 等控制命令仍可执行。

#### 3.1.4 渠道只负责表现，决议回到统一机制

Hermes 的平台适配器优先尝试按钮审批，不支持时发送 `/approve`、`/deny` 文本说明。适配器最终调用同一个 resolver，而不是各自实现 Agent 恢复逻辑。例如 WhatsApp Cloud 的按钮 payload 携带 approval ID，文本渠道则使用命令。

结论：Yuxi 的渠道插件也应只负责身份归一、消息收发和能力声明；interrupt 状态、授权、幂等与 resume 必须由核心服务统一管理。

### 3.2 OpenClaw

调研版本：[`openclaw/openclaw@d4a90c7`](https://github.com/openclaw/openclaw/tree/d4a90c7bbb69b78990582a3b8952ff99f1812fa3)

#### 3.2.1 问题是独立的一等协议对象

OpenClaw 的 [`packages/gateway-protocol/src/schema/questions.ts`](https://github.com/openclaw/openclaw/blob/d4a90c7bbb69b78990582a3b8952ff99f1812fa3/packages/gateway-protocol/src/schema/questions.ts) 定义了完整问题协议：

- 稳定 `id`；
- `agentId`、`sessionKey`；
- `createdAtMs`、`expiresAtMs`；
- `pending / answered / cancelled / expired` 状态；
- `question.request / waitAnswer / resolve / get / list`；
- `question.requested / question.resolved` 广播事件；
- 回答者 `resolvedBy`。

[`src/gateway/question-manager.ts`](https://github.com/openclaw/openclaw/blob/d4a90c7bbb69b78990582a3b8952ff99f1812fa3/src/gateway/question-manager.ts) 保证一次性状态转换、答案校验、超时和短期 terminal record 保留。重复 resolve 会明确返回 already-terminal，而不是重复执行。

相比只在 SSE chunk 中附加临时字段，这种模型更适合作为 Yuxi 对外协议，因为客户端断线重连后仍可按 ID 查询状态并恢复 UI。

#### 3.2.2 审批命令显式携带 ID 和决策

OpenClaw 的 [`commands-approve.ts`](https://github.com/openclaw/openclaw/blob/d4a90c7bbb69b78990582a3b8952ff99f1812fa3/src/auto-reply/reply/commands-approve.ts) 使用：

```text
/approve <id> allow-once
/approve <id> allow-always
/approve <id> deny
```

处理流程同时校验：

- 命令格式和决策枚举；
- 当前渠道、账号与发送者是否有审批权；
- Gateway client 是否有 `operator.approvals` / `operator.admin` scope；
- ID 属于 exec approval 还是 plugin approval；
- 请求是否存在或已经过期。

[`exec-approval-reply.ts`](https://github.com/openclaw/openclaw/blob/d4a90c7bbb69b78990582a3b8952ff99f1812fa3/src/infra/exec-approval-reply.ts) 和相关提示允许使用短 ID，但如果短 ID 有歧义，要求使用完整 ID。这比“永远处理最旧一项”更适合公开系统。

#### 3.2.3 `ask_user` 与普通消息抢占有明确边界

OpenClaw 的 [`gateway-question.ts`](https://github.com/openclaw/openclaw/blob/d4a90c7bbb69b78990582a3b8952ff99f1812fa3/src/agents/harness/gateway-question.ts) 规定同一 `sessionKey` 最多注册一个待回答问题，并在普通 steering 前调用 `claimPendingAgentQuestionAnswer`。成功 claim 后该消息只作为问题答案，不再作为新的 Agent turn；注册失败或问题已 terminal 时才回落到普通消息路径。

这同时解决两个问题：

- 回答不会被重复记录成一次回答和一次新对话；
- 问题注册与回答并发时，可以先缓冲答案，待 Gateway 注册提交后再 resolve。

OpenClaw 还把问题按钮解析为统一的 `question.resolve`，并限制按钮解析只适用于单个、非 multi-select、非 secret 的问题，复杂问题留给更完整的交互面。

#### 3.2.4 渲染和状态是分离的

[`question-channel-runtime-internal.ts`](https://github.com/openclaw/openclaw/blob/d4a90c7bbb69b78990582a3b8952ff99f1812fa3/src/infra/question-channel-runtime-internal.ts) 记录一个问题可能投递到多个渠道 UI；问题 terminal 后，各投递面分别更新为 Answered / Cancelled / Expired。自由文本不会被回显到公共卡片，避免泄露秘密、mentions 或渠道 markup。

结论：同一 interrupt 可以有多个表现面，但只能有一个核心状态与一次有效决议。

### 3.3 OpenOcta（补充参考）

调研版本：[`openocta/openocta@c4c5bd3`](https://github.com/openocta/openocta/tree/c4c5bd3e839707cb3b844c051f21dc36ced09eb8)

OpenOcta 的 [`src/pkg/security/approval.go`](https://github.com/openocta/openocta/blob/c4c5bd3e839707cb3b844c051f21dc36ced09eb8/src/pkg/security/approval.go) 使用带 `id`、`session_id`、`pending / approved / denied`、审批人、原因、时间和过期时间的持久化 ApprovalQueue。执行方通过 `Wait(ctx, id)` 等待，context 取消会解除等待；session whitelist 也有 TTL。

它进一步说明：审批记录应是可持久化、可审计的领域对象，不能只存在于某个 WebSocket 连接或 CLI 进程内。不过 Yuxi 不应直接照搬 session whitelist；是否支持“本会话始终允许”需要独立安全评审。

## 4. 对 Yuxi 当前边界的判断

Yuxi 已经具备大部分运行时基础：

- `run_worker` 能把 Agent chunk 归一为 `interrupt` 事件；
- interrupt chunk 已携带问题或审批所需的业务数据；
- 内部 Agent Run 创建接口能校验 resume payload；
- resume run 通过 `created_by_run_id` 建立父子关系；
- 服务端已有 `resume_superseded` 检查，避免恢复一个已被后续 run 超越的中断；
- Web 已经验证“解析 interrupt -> 收集用户决议 -> 创建 resume run -> 继续 SSE”的完整链路。

缺少的是公开协议层，而不是 LangGraph 执行层：

1. Agent Call 响应没有规范化 `interrupt`；
2. Agent Call 没有原生 resume URL / endpoint；
3. 外部客户端无法仅凭公开 Agent Call 文档恢复运行；
4. 纯文本入站消息没有统一的控制命令解析与 pending interrupt 路由；
5. interrupt 目前主要依附 run event chunk，缺少稳定 ID、独立状态、过期与审计表达。

## 5. 推荐的统一领域模型

建议把“Run 已中断”和“等待用户处理的 Interrupt”区分开：Run 是一次执行，Interrupt 是该执行产生、等待外部决议的对象。

```json
{
  "interrupt_id": "int_01...",
  "run_id": "run_01...",
  "thread_id": "thread_01...",
  "agent_slug": "...",
  "kind": "tool_approval",
  "status": "pending",
  "version": 1,
  "created_at": "2026-07-25T10:00:00Z",
  "expires_at": "2026-07-25T10:15:00Z",
  "allowed_actions": ["approve", "reject"],
  "payload": {
    "action_requests": [],
    "review_configs": []
  },
  "resolution": null,
  "resolved_at": null,
  "resolved_by": null
}
```

### 5.1 类型

第一阶段只定义两类：

- `tool_approval`：工具调用审批；
- `question`：结构化或自由文本问题。

`interrupted` 如果只是用户取消、服务器停止等终态，不一定创建可 resume 的 Interrupt。只有确实存在 `allowed_actions` 的中断才返回 `status=pending` 的 Interrupt，避免客户端误以为所有 interrupted run 都能继续。

### 5.2 状态

建议状态为：

```text
pending -> resolved
pending -> cancelled
pending -> expired
pending -> superseded
```

终态不可再次修改。生命周期状态与业务决议分开表达：`resolved` 表示 `approve`、`reject` 或 `answer` 决议已被接受，并成功创建唯一的子 resume run；具体决议保存在 `resolution.action`，而不是另设容易和 HTTP 请求拒绝混淆的 `rejected` 状态。`cancelled` 表示明确终止、不再创建 resume run。

### 5.3 决议载荷

公共协议不直接暴露 LangGraph 内部字段。建议使用稳定的外部表达，由服务层转换为现有 `resume.decisions`：

审批：

```json
{
  "interrupt_id": "int_01...",
  "action": "approve",
  "decisions": [
    {"action_request_id": "...", "decision": "approve"}
  ],
  "request_id": "client-generated-id"
}
```

提问：

```json
{
  "interrupt_id": "int_01...",
  "action": "answer",
  "answers": {
    "question_1": ["选项 A"],
    "question_2": ["自由文本"]
  },
  "request_id": "client-generated-id"
}
```

## 6. 能力线一：富客户端公开接口

### 6.1 最小第一阶段

建议在 Agent Call 命名空间内补齐闭环：

```http
POST /api/agent-invocation/agent-call/runs
GET  /api/agent-invocation/agent-call/runs/{run_id}
GET  /api/agent-invocation/agent-call/runs/{run_id}/events
POST /api/agent-invocation/agent-call/runs/{run_id}/resume
POST /api/agent-invocation/agent-call/runs/{run_id}/cancel
```

考虑最小改动，`GET result` 与 Agent Call 专用 SSE 可以后续规范化；第一步至少必须做到：

1. 现有 Agent Call 结果在 interrupted 时返回完整 `interrupt`；
2. 新增 Agent Call 原生 resume endpoint；
3. 创建 run 的响应给出可发现链接，而不是让客户端拼内部路径。

建议响应：

```json
{
  "run_id": "...",
  "thread_id": "...",
  "status": "interrupted",
  "interrupt": {},
  "links": {
    "self": "/api/agent-invocation/agent-call/runs/...",
    "events": "/api/agent-invocation/agent-call/runs/.../events",
    "resume": "/api/agent-invocation/agent-call/runs/.../resume",
    "cancel": "/api/agent-invocation/agent-call/runs/.../cancel"
  }
}
```

### 6.2 CLI 行为

`yuxi chat` 收到 interrupt 后：

1. 保持原 assistant 流式文本；
2. 将当前消息标记为“等待操作”，不能显示成普通成功结束；
3. `tool_approval` 渲染批准 / 拒绝；
4. `question` 渲染选项或输入框；
5. 调用公开 resume endpoint；
6. 使用返回的新 `run_id` 继续订阅 SSE，复用原 `thread_id`；
7. 页面刷新后按 thread/run 查询 pending interrupt 并恢复交互。

CLI 页面只是渲染器，不自行拼 LangGraph resume 数据，也不从普通聊天文本猜审批决议。

## 7. 能力线二：纯文本 IM 统一入口

### 7.1 统一入站信封

外部平台 webhook 与 adapter 到核心服务的 ingestion 必须是两个信任层：

```text
平台 webhook
  -> 渠道专用接收端（平台验签、时间窗、message_id 去重）
  -> 统一内部 ingestion（adapter 服务身份认证、binding 鉴权）
  -> 核心消息路由
```

建议渠道适配器把各平台 webhook 归一后送入同一内部入口，例如：

```http
POST /internal/agent-invocation/channel/messages
```

```json
{
  "adapter_binding_id": "binding_01...",
  "channel": "wechat",
  "account_id": "official-account-a",
  "message_id": "provider-message-id",
  "chat_id": "chat-id",
  "thread_id": null,
  "sender": {"id": "provider-user-id", "display_name": "..."},
  "message": {"type": "text", "text": "/approve int_01..."},
  "timestamp": "...",
  "signature_context": {}
}
```

这是适配器到核心服务的内部标准，不是可匿名访问的公共 webhook，也不建议让每个渠道直接调用 Agent resume。Adapter 负责平台验签、去重、平台 ID 提取与发送；核心服务负责会话映射、权限、命令和 Agent 生命周期。

Adapter 到 ingestion 必须使用 mTLS、请求 HMAC 或 OAuth service principal 等服务身份认证。核心服务根据认证主体与 `adapter_binding_id` 在服务端派生允许的 `tenant_id / channel / account_id`，并拒绝信封声明与 binding 不一致的请求；不得信任调用方自行提交的 tenant、channel、account、sender 权限或 `signature_context`。`signature_context` 若仅用于审计，只能携带非敏感验签结果摘要，不能作为核心鉴权事实，也不能包含原始签名密钥。

### 7.2 路由键

仅用 `user_id` 或 `chat_id` 都不够。建议基础 conversation key 为：

```text
(tenant_id, channel, account_id, chat_id, provider_thread_id?)
```

并额外记录发送者：

```text
actor = (channel, account_id, sender_id)
```

conversation key 映射到 Yuxi `thread_id`。群聊中 pending interrupt 属于 conversation，但能否 resolve 由 actor authorization 单独判断，不能因为同处一个群就自动有审批权。

### 7.3 命令语法

推荐最小命令集：

```text
/pending
/approve <interrupt_id>
/reject <interrupt_id> [reason]
/answer <interrupt_id> <text>
/cancel <run_id>
/help
```

选择题可增加可机器解析的形式：

```text
/answer <interrupt_id> <question_id>=2
/answer <interrupt_id> <question_id>="自定义答案"
```

如果一个 interrupt 只包含一个问题，可允许：

```text
/answer <interrupt_id> 2
```

不建议第一版加入 `/approve all`，因为批量批准扩大误操作半径。若未来加入，必须在命令中显示作用域和数量，并单独做安全评审。

### 7.4 是否接受“运行”“是”“同意”等自然文本

建议默认规则：

- 工具审批：不接受普通自然文本，必须是带 ID 的 slash command 或可信按钮回调；
- 问题回答：优先 `/answer <id> ...`；同一 conversation 恰好只有一个 pending question、发送者有权回答且消息不是命令时，可以把下一条文本 claim 为回答；
- 群聊、多个 pending interrupt、多个问题或安全敏感问题：禁止隐式 claim，必须带 ID；
- 自然语言别名可作为渠道级 opt-in，且只在“唯一 pending + 私聊 + 同一 actor”时启用。

原因：Hermes 的裸 `yes` 依赖严格的会话内唯一等待状态，适合单进程交互；公开服务可能存在多 run、多设备和群聊。OpenClaw 的显式 ID 更稳妥。问题自由文本又天然需要接收普通消息，因此可以在无歧义条件下提供受限便利。

### 7.5 入口解析顺序

```text
1. 验证 adapter 服务身份与 binding，并确认平台验签和时间窗校验已由受信 adapter 完成；核心按 provider message_id 再次去重
2. 从 binding 派生 tenant / channel account，再解析 chat / thread / actor
3. 建立或读取 Yuxi thread 映射
4. 识别 slash command
5. 查询该 conversation 的 pending interrupts
6. 命令命中：鉴权、resolve、返回确认，不创建普通 Agent turn
7. 非命令且满足唯一 pending question 规则：claim 为 answer，不创建普通 Agent turn
8. 否则按 busy policy 执行 steer / queue / reject / new turn
```

关键不变量：一条入站消息只能被消费一次。它要么是 interrupt 决议，要么是普通 Agent 消息，不能两者都是。

## 8. Resume、幂等和并发

### 8.1 一次 interrupt 只产生一个有效 resume run

服务端应在同一事务或等效原子操作中完成：

1. 校验 interrupt 为 `pending`；
2. 校验它仍是 thread 最新可恢复中断；
3. 校验 actor 有权执行 action；
4. 规范化 action / decisions / answers，并计算包含 actor、tenant 与授权 scope 的 payload hash；
5. 用 `interrupt_id + request_id` 查找幂等记录并核对 payload hash 与 actor；
6. 创建唯一 child resume run；
7. 将 interrupt 标记为 terminal，并记录 `resolution` 与 `resumed_run_id`。

两个客户端同时批准时，只允许一个成功创建 child run。另一个请求：

- 同 request ID、同 actor、同授权范围且 canonical payload 相同：返回第一次创建的同一个 run；
- 同 request ID 但 action、答案、actor 或授权范围不同：返回 `409 idempotency_key_conflict`，绝不能静默复用旧决议；
- 不同 request ID：返回 `409 interrupt_already_resolved`；
- 已被后续 turn 超越：返回 `409 interrupt_superseded`；
- 已过期：返回 `410 interrupt_expired`。

### 8.2 不把“收到决议”误认为“恢复成功”

如果创建 resume run 失败，interrupt 不应被永久标成 resolved。可以使用事务、outbox，或先记录 resolving lease 再提交；最终必须可重试且不会重复创建 run。

### 8.3 断线重连

客户端不能只依赖实时 SSE。至少需要通过 run 或 thread 查询当前 pending interrupt。SSE 用于低延迟通知，查询接口是恢复事实来源。

## 9. 安全与审计

### 9.1 授权

- Agent Call：沿用调用 API Key 的用户 / tenant 权限，并校验 run 归属；
- IM：渠道验签只证明消息来自平台，不等于发送者有审批权；
- 私聊可绑定 Yuxi 用户；群聊需要 owner / approver allowlist 或明确角色映射；
- 回答普通问题和批准危险工具可以使用不同权限。

### 9.2 防重放

- 渠道 `message_id` 去重；
- webhook timestamp / nonce 时间窗；
- interrupt terminal 后拒绝再次 resolve；
- request ID 幂等；
- 按钮 callback 必须携带不可猜测或签名过的 interrupt reference。

### 9.3 最小披露

- 审批提示展示执行所需的最小命令与影响，凭据先脱敏；
- 群聊中不回显自由文本答案；
- adapter 日志不记录 API Key、原始签名密钥或敏感回答；
- `resolved_by` 记录稳定 actor ID，不只记录昵称。

### 9.4 审计字段

至少记录：

- interrupt / run / thread；
- channel / account / chat / actor；
- 原始 action 与标准化 decision；
- request ID、provider message ID；
- created / resolved / expired 时间；
- 是否来自按钮、slash command、富客户端或隐式 answer claim；
- resume child run ID；
- 拒绝或失败原因。

## 10. 推荐实施阶段

### Phase 1：公开 Resume + CLI 闭环

- Agent Call interrupted 响应返回结构化 interrupt；
- 新增 Agent Call 原生 resume endpoint；
- 提供 pending interrupt 查询能力；
- `yuxi chat` 渲染审批 / 问题并继续流式输出；
- 覆盖刷新恢复、重复点击、超时、superseded 与断线重连测试。

这是当前最优先工作，能先证明公共协议，不依赖任何 IM。

### Phase 2：文本命令与统一消息路由

- 定义 channel message envelope 与 conversation mapping；
- 实现独立、可测试的 slash command parser；
- 命令解析发生在普通 Agent dispatch 前；
- 实现 `/pending`、`/approve`、`/reject`、`/answer`、`/cancel`；
- 实现唯一 pending question 的受限文本 claim；
- 完成 actor authorization、幂等和审计。

### Phase 3：首个 IM Adapter

- 选择一个具备稳定 webhook 与测试环境的渠道；
- 实现签名验证、入站去重、出站发送、错误重试；
- 富能力渠道用按钮 / 卡片，文本能力缺失时自动降级为 slash command；
- adapter 不包含 Agent resume 业务逻辑。

### Phase 4：多渠道与统一审批面

- 增加飞书、钉钉、微信等适配器；
- 支持一个 interrupt 投递到多个授权表现面；
- 任一表现面成功 resolve 后，其他表现面更新 terminal 状态；
- 评估是否需要跨会话审批箱和代理审批人。

## 11. 测试与验收建议

### 11.1 公共 API

- question / approval 两类 interrupt schema 稳定；
- interrupted run 查询能恢复完整 payload；
- resume 成功产生 child run 并继续 SSE；
- 重复同 request ID 返回同一 child run；
- 并发不同决议只有一个成功；
- expired / cancelled / superseded 返回明确错误；
- 非 run 所属用户不可查询或 resolve。

### 11.2 CLI

- 流式文本后出现审批 UI，不显示为普通完成；
- approve / reject 均能继续到明确 terminal；
- 选择题、自由文本、多个问题可提交；
- 刷新后恢复 pending UI；
- SSE EOF 且无 terminal event 不误报成功；
- API Key 不进入浏览器 HTML / JS / console。

### 11.3 纯文本入口

- `/approve <id>` 不创建普通 Agent turn；
- 普通“是”在没有 pending 时仍是普通消息；
- 多 pending 时省略 ID 必须拒绝并返回 `/pending` 提示；
- 唯一 question 的文本回答只消费一次；
- slash command 在 question pending 时仍优先执行；
- 群聊未授权成员不能批准；
- provider 重放同一 message ID 不重复恢复；
- 两个渠道同时处理同一 interrupt 只有一个成功；
- 相同 request ID 改变 approve / reject、答案或 actor 时返回 idempotency conflict；
- 未认证 adapter、binding 越权和信封身份与 binding 不一致时均被拒绝。

### 11.4 Adapter contract

- 验签失败不进入核心路由；
- 渠道限流或临时失败进入可观测重试；
- 不支持按钮时生成可直接复制的文本命令；
- terminal 后按钮更新或文本回复清楚说明已处理 / 已过期。

## 12. 需要讨论并拍板的问题

1. Interrupt 是否第一版就落独立表，还是先从 run terminal chunk 派生并在后续迁移？推荐独立持久化对象，避免刷新和多端解析依赖事件细节。
2. Agent Call 是否趁此改成标准 REST `GET /runs/{id}`，还是先保持现有 `POST /runs/result` 兼容并只新增 resume？推荐先兼容新增，之后版本化整理。
3. `question` 是否默认允许唯一 pending 时把下一条普通文本 claim 为回答？推荐私聊允许、群聊禁用，并支持渠道配置覆盖。
4. 第一版是否支持 `allow-always` / “本会话始终允许”？推荐不支持，只做单次 approve / reject，待权限和 allowlist 模型成熟后再评估。
5. 多个 action request 是否允许整组批准，还是要求逐项 decision？推荐协议支持逐项，UI 可以提供“全部批准”但必须提交显式列表。
6. 首个 IM 应选哪个？应根据 webhook 可测试性、机器人权限、按钮能力、群聊身份稳定性和国内部署约束另开选型讨论。
7. 渠道账号与 Yuxi 用户如何绑定？需要决定管理员静态映射、一次性 pairing code、OAuth 或企业组织目录中的一种或组合。
8. `/cancel <run_id>` 是只取消仍在执行的 run，还是也能取消该 run 产生的 pending Interrupt？推荐拆成明确语义：`/cancel <run_id>` 只取消 active run，`/cancel-interrupt <interrupt_id>` 才终止等待且不创建 resume run，避免对已 terminal 的 interrupted run 产生歧义。

## 13. 推荐结论

建议先批准机制方向，不在本议题内直接批准具体 backend 实现：

1. 将 Interrupt 作为带稳定 ID、状态、过期与审计的一等对象；
2. Agent Call 补齐结构化 interrupt、公开 resume 与可恢复查询；
3. CLI 先完成富客户端 resume 验证；
4. 纯文本消息统一进入渠道入口，控制命令和 pending question claim 必须先于普通消息分发；
5. 审批默认使用显式 interrupt ID，问题仅在严格无歧义时允许下一条文本隐式回答；
6. Adapter 只做平台能力适配，不复制 interrupt / resume 核心逻辑。

## 14. Checklist

- [x] 核对 Yuxi 当前 interrupt / resume 内部链路
- [x] 调研 Hermes Agent 的审批、提问、文本降级和会话路由
- [x] 调研 OpenClaw 的稳定 ID、问题 RPC、审批命令与授权
- [x] 补充 OpenOcta 持久化审批队列参考
- [x] 给出富客户端与纯文本 IM 两条能力线
- [x] 给出状态模型、命令语法、消歧、幂等、安全与测试建议
- [ ] 讨论并确认公共 Interrupt schema
- [ ] 讨论并确认 Agent Call resume API
- [ ] 讨论并确认文本回答的隐式 claim 规则
- [ ] 确认首个 IM 与身份绑定方案
- [ ] 经方案评审后拆分实现任务
