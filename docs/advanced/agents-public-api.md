# Agents Public API

本页供 Web 客户端和外部应用查找 Agent 对话接口、输入格式及状态读取方式。运行时状态与恢复规则见 [Agent 输入队列与调度](../mechanisms/agent-request-queue.md)。Yuxi 使用自己的事件协议，不声明与 OpenAI Agents API 完全兼容。

## 身份与作用域

登录用户用 JWT 调用 Public API；CLI 或产品集成可用未绑定 APP 的 `full` Key，以密钥所属用户身份访问同一产品作用域。外部应用使用绑定 `app_id` 的 `agents` API Key。服务端从 Key 决定 APP，客户端的 `X-App-Id` 不改变作用域。绑定 APP 的 Key 可提供 `X-End-User-Id`，服务端在 Key 所属用户和 APP 内解析独立终端用户；未提供时使用该 APP 的默认终端用户。后续查询和订阅使用相同 Header。JWT 与未绑定 APP 的 `full` Key 不接受该 Header；APP 终端用户不能访问产品 Thread 或 Workspace。密钥创建与权限见 [API Key 接入](./api-key-integration.md)。

## Thread、Turn、Run 与 Input

Thread 是长期对话，Turn 是一轮工作，Run 是其中一段有执行 owner 的运行。普通 `follow_up` 消息先保存为 Input；线程空闲且队列未暂停时领取 FIFO 队头并创建 Turn/Run。`steer` 指向当前 Turn，多次输入可合并为一个待消费批次，安全接管后在同一 Turn 创建下一 Run。回答或审批消费明确等待点，也在同一 Turn 创建下一 Run。接收响应中的 `event_id`、`input_id` 和状态只证明持久接收；工作结果通过 Turn 查询。

`/api/v1/agents/threads` 是主协议。`/api/v1/agents/sessions` 是相同 Thread 的命名适配：`session_id` 等于 `thread_id`，认证、幂等键、调度和存储完全相同。Session 事件类型只在 HTTP 边界映射。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/agents`、`/api/v1/agents/{agent_id}` | 查询可见主 Agent |
| `POST`、`GET` | `/api/v1/agents/threads` | 创建空或带首批输入的 Thread；按 APP 作用域列出 active Thread |
| `GET`、`PATCH` | `/api/v1/agents/threads/{thread_id}` | 读取快照；修改标题、置顶及后续输入默认配置 |
| `POST` | `/api/v1/agents/threads/{thread_id}/archive` | 无活跃 Turn 和待处理 Input 时归档，保留历史 |
| `POST`、`GET` | `/api/v1/agents/threads/{thread_id}/events` | 提交单个输入或控制事件；订阅整段 Thread |
| `GET` | `/api/v1/agents/threads/{thread_id}/queue` | 查看待处理 Input 和暂停状态 |
| `GET` | `/api/v1/agents/threads/{thread_id}/inputs/{input_id}` | 查看接收、消费和消息归属 |
| `GET` | `/api/v1/agents/threads/{thread_id}/turns/{turn_id}` | 查看整轮状态、等待点、Run 和明确结果 |
| `GET` | `/api/v1/agents/threads/{thread_id}/turns/{turn_id}/items` | 按 `after_id`、`limit` 读取本轮消息 |
| `GET` | `/api/v1/agents/threads/{thread_id}/runs/{run_id}` | 查看指定执行段 |
| `GET` | `/api/v1/agents/threads/{thread_id}/history` | 查看持久历史及轻量 Run 列表 |

Thread 和 Session 的创建、事件提交都必须提供长度为 1–128 的 `Idempotency-Key`。同一身份、Thread 和键重复提交相同意图返回首次回执；改变命令、目标或内容返回 `409`。创建时同键经 Thread 或 Session 路径产生相同 Thread。未知字段、批量事件或无效内容返回 `422`。

## 创建与提交消息

创建可传 `agent_id`、`title`、`project_id`、`model_spec`、`tool_approval_mode`、`input` 和 `stream`。`agent_id` 使用可见 Agent slug；`input` 是有序的 `user` 消息数组，内容块支持 `input_text` 与内联 `data:image/...;base64,...` 的 `input_image`。每条消息最多 10 张图片，图片内容总量最多 80 MiB；内置 nginx 对 Thread/Session 创建和消息事件放行 100 MiB 请求体，外层代理也需配置相应上限。`stream=true` 要求同时提供输入。带输入创建把 Thread、Message、Input、回执和首个 Turn/Run 在同一数据库事务提交；提交后才投递 worker。

```bash
curl --fail "$BASE_URL/api/v1/agents/threads" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'X-End-User-Id: crm-user-42' \
  -H 'Idempotency-Key: crm-thread-0001' \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"default-chatbot","input":[{"role":"user","content":[{"type":"input_text","text":"你好"}]}]}'
```

向已有 Thread 提交后续消息时明确使用 `follow_up`；需要修正当前运行时使用 `steer` 并指定当前 `turn_id`。一次 POST 只接收一个事件，事件内可有多条有序消息。排队中的 `follow_up` 没有 Turn ID；尚未被安全接管的 `steer` 已绑定目标 Turn，但尚无消费 Run ID。模型与审批模式在接收时冻结，已排队 Input 不因 Thread 默认值变化而改变。

```bash
curl --fail -X POST "$BASE_URL/api/v1/agents/threads/$THREAD_ID/events" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'X-End-User-Id: crm-user-42' \
  -H 'Idempotency-Key: crm-message-0002' \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"type":"agent.thread.input.message","mode":"follow_up","input":[{"role":"user","content":[{"type":"input_text","text":"请继续"}]}]}]}'
```

## 等待、取消与队列

Turn `waiting` 时普通消息被拒绝。Turn 快照的 `waitpoint` 提供 `id`、`kind` 和应回答的问题或应决策的工具调用。恢复事件必须提供 `turn_id`、`waitpoint_id`，并按等待点完整提交 `answer` 或 `approval` 响应；旧等待点或重复改变意图返回 `409`。

```json
{"events":[{"type":"yuxi.thread.input.resume","turn_id":"<turn-id>","waitpoint_id":"<waitpoint-id>","response":{"type":"answer","answers":[{"question_id":"<question-id>","answer":"确认"}]}}]}
```

取消事件 `yuxi.thread.input.cancel` 必须指定 `turn_id`，可用 `expected_run_id` 防止取消已经切换的执行段。取消使当前 Turn 收敛，并暂停保留的后续 `follow_up`；等待点的 checkpoint 清理完成前，队列不会继续。`yuxi.thread.input.continue` 在清理完成后显式解除暂停。`yuxi.thread.input.cancel_input` 只移除指定的待处理 Input，不冒充尚未创建的 Turn。归档拒绝活跃 Turn 或待处理 Input；归档后的详情与历史仍可读。

## 读取与事件

`GET /threads/{thread_id}` 返回 Thread 状态、`current_turn`、`queue_paused` 和 `queued_input_count`。Turn 结果从 `result_run_id` 指向的顶层 Run 的 `output_message_id` 读取；`interrupted` 或 `yielded` Run 结束不表示 Turn 完成。历史包含原始用户消息、已交付输出及轻量 Run 归属；模型与工具审计另由超级管理员 JWT 查询。

`GET /threads/{thread_id}/events` 订阅整个 Thread。结构化 SSE 事件携带 `type`、`thread_id`、适用的 `turn_id`、`input_id`、`run_id`、`cursor` 和 `payload`。`Last-Event-ID` 用于续订。输入接收、输入消费、Run 结束与 Turn 结束是不同事件；Redis 增量短期保留，断线后的业务终态以 Input、Turn、Run 和历史查询为准。Session 路径输出 `session_id` 和 `agent.session.*` 类型，不建立另一条事件流。
