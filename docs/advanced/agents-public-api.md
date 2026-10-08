# Agents Public API

本页供 Web 客户端和外部应用查找 Agent 对话接口、输入格式及状态读取方式。运行时状态与恢复规则见 [Agent 输入队列与调度](../mechanisms/agent-request-queue.md)。公开流以 [OpenAI Agents streaming events](https://developers.openai.com/api/reference/resources/beta/subresources/agents/streaming-events) 为基线。标准事件保留官方字段和语义，业务扩展放入 `yuxi`，专有事件使用 `yuxi.*`；尚未支持的官方输入动作返回 `422`。

## 身份与作用域

登录用户用 JWT 调用 Public API；CLI 或产品集成可用未绑定 APP 的 `full` Key，以密钥所属用户身份访问同一产品作用域。外部应用使用绑定 `app_id` 的 `agents` API Key。服务端从 Key 决定 APP，客户端的 `X-App-Id` 不改变作用域。绑定 APP 的 Key 可提供 `X-End-User-Id`，服务端在 Key 所属用户和 APP 内解析独立终端用户；未提供时使用该 APP 的默认终端用户。后续查询和订阅使用相同 Header。JWT 与未绑定 APP 的 `full` Key 不接受该 Header；APP 终端用户不能访问产品 Thread 或 Workspace。密钥创建与权限见 [API Key 接入](./api-key-integration.md)。

所有 APP 终端用户，包括默认终端用户，都无法发现、读取或运行私有 Agent。JWT 和未绑定 APP 的个人 `full` Key 可使用自己的私有 Agent；系统管理员治理其他人的私有定义使用产品管理接口。完整规则见[资源权限](../mechanisms/resource-permissions.md)。

系统的用户列表和共享候选默认只显示系统用户。管理员在用户管理中通过“用户类型”筛选显式查看 APP 终端用户或全部类型，“所属部门/实例”列显示终端用户的 `app_id`。长用户名和 ID 在列表中缩略，点击用户信息可查看完整值。

## Thread、Turn、Run 与 Input

Thread 是长期对话，Turn 是一轮工作，Run 是其中一段有执行 owner 的运行。普通消息先保存为 Input，`follow_up` 彼此 FIFO，`steer` 合并为唯一的待消费优先批次。线程空闲且队列未暂停时领取优先队头并创建 Turn/Run；运行中在安全边界消费 steer，在同一 Turn 创建下一 Run。回答或审批消费明确等待点，也在同一 Turn 创建下一 Run。接收响应中的 `event_id`、`input_id` 和状态只证明持久接收；工作结果通过 Turn 查询。

`/api/v1/agents/threads` 是主协议。

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
| `GET` | `/api/v1/agents/threads/{thread_id}/turns/{turn_id}/items` | 按 `after_id`、`limit` 读取本轮公开 items |
| `GET` | `/api/v1/agents/threads/{thread_id}/runs/{run_id}` | 查看指定执行段 |
| `GET` | `/api/v1/agents/threads/{thread_id}/history` | 查看持久历史及轻量 Run 列表 |

Thread 的创建、事件提交都必须提供长度为 1–128 的 `Idempotency-Key`。同一身份、Thread 和键重复提交相同意图返回首次回执；改变命令、目标或内容返回 `409`。未知字段、批量事件或无效内容返回 `422`。

## 创建与提交消息

创建可传 `agent_id`、`title`、`project_id`、`model_spec`、`tool_approval_mode`、`input` 和 `stream`。`agent_id` 使用可见 Agent slug；`input` 是有序的 `user` 消息数组，内容块支持 `input_text` 与内联 `data:image/...;base64,...` 的 `input_image`。每条消息最多 10 张图片，图片内容总量最多 80 MiB；内置 nginx 对 Thread 创建和消息事件放行 100 MiB 请求体，外层代理也需配置相应上限。`stream=true` 要求同时提供输入。带输入创建把 Thread、Message、Input、回执和首个 Turn/Run 在同一数据库事务提交；提交后才投递 worker。

```bash
curl --fail "$BASE_URL/api/v1/agents/threads" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'X-End-User-Id: crm-user-42' \
  -H 'Idempotency-Key: crm-thread-0001' \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"default-chatbot","input":[{"role":"user","content":[{"type":"input_text","text":"你好"}]}]}'
```

向已有 Thread 提交普通排队消息使用 `yuxi.mode=follow_up`；优先处理使用 `yuxi.mode=steer`。steer 位于所有 follow-up 前面，待领取期间的新 steer 消息合并到同一个 Input。普通消息不接受 `yuxi.turn_id`；Input 在消费时固定 Turn/Run，接收回执不返回 mode。即使发送期间当前 Turn 已结束，steer 仍会优先开启新 Turn。

未指定 mode 时，服务在 Thread 锁内按运行中 steer、空闲 follow-up 选择；等待用户回答、审批或取消清理时拒绝普通消息；协作等待时默认为 follow-up，只进入 FIFO 队列。一次 POST 只接收一个事件，事件内可有多条有序消息。幂等重试返回同一回执与 Input，不重新入队；已消费后返回实际 Turn/Run 归属。两类 pending Input 的 Turn/Run ID 均为空。

新 Input 的模型与审批配置在接收时冻结。steer 不接受显式模型或审批配置；空闲调度使用批次创建时冻结的默认配置，运行中安全接管沿用当前 Run 配置。调度、暂停和取消批次的详细规则见[输入队列机制](../mechanisms/agent-request-queue.md)。

```bash
curl --fail -X POST "$BASE_URL/api/v1/agents/threads/$THREAD_ID/events" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'X-End-User-Id: crm-user-42' \
  -H 'Idempotency-Key: crm-message-0002' \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"type":"agent.session.input.message","yuxi":{"mode":"follow_up"},"input":[{"role":"user","content":[{"type":"input_text","text":"请继续"}]}]}]}'
```

## 等待、取消与队列

Turn 等待用户回答或审批时普通消息被拒绝，协作等待时 follow-up 只进入 FIFO 队列。Turn 快照的 `waitpoint` 提供 `id`、`kind` 和应回答的问题或应决策的工具调用。恢复事件必须提供 `turn_id`、`waitpoint_id`，并按等待点完整提交 `answer` 或 `approval` 响应；旧等待点或重复改变意图返回 `409`。

```json
{"events":[{"type":"yuxi.session.input.resume","turn_id":"<turn-id>","waitpoint_id":"<waitpoint-id>","response":{"type":"answer","answers":[{"question_id":"<question-id>","answer":"确认"}]}}]}
```

官方取消事件为 `agent.session.input.cancel`，可用 `yuxi.turn_id`、`yuxi.expected_run_id` 固定目标。省略目标时，事务内选择当前 Turn；幂等重试仍取消首次选定的 Turn。取消使当前 Turn 收敛，并暂停保留的后续 `follow_up`；等待点的 checkpoint 清理完成前，队列不会继续。`yuxi.session.input.continue` 在清理完成后显式解除暂停。`yuxi.session.input.cancel_input` 只移除指定的待处理 Input，不冒充尚未创建的 Turn。归档拒绝活跃 Turn 或待处理 Input；归档后的详情与历史仍可读。

## 读取与事件

`GET /threads/{thread_id}` 返回 Thread 状态、`current_turn`、`queue_paused` 和 `queued_input_count`。Turn 结果只来自 `result_run_id` 指向的当前 Turn Run；模型正文结束、`interrupted` 和 `yielded` 均不表示 Turn 完成。`/history` 返回 `thread`、`runs`、`items`，`/turns/{turn_id}/items` 复用相同公开投影。普通用户可读取已经展示的工具参数、结果和执行状态；内部 prompt、checkpoint 和完整审计不进入普通历史，审计仍仅允许超级管理员 JWT。

`GET /threads/{thread_id}/events` 订阅整个 Thread，每条 SSE `data` 就是一个公开事件，`event` 等于其 `type`，`id` 是订阅 cursor。`event_id` 标识逻辑事件，Redis 重放保持稳定；它与 cursor 分开。`session_id` 是真实 Thread ID，`yuxi.run_id` 是业务执行段。协作成员通过自己的 Thread 入口读取历史和事件；父页面通过 `/state` 的 `agent_state.cooperation` 读取树内持久状态。

| 内容 | 公开事件 |
| --- | --- |
| 正文 | `agent.session.turn.item.added/done`、`content_part.added/done`、`output_text.delta/done` |
| 实际工具 | `function_call` 与 `function_call_output` item，通过 `call_id` 关联；完整参数和执行完成分别通知 |
| 业务整轮 | `agent.session.turn.created/in_progress/completed/failed/cancelled`，主体来自已提交 Turn |
| 能力受限 | `yuxi.session.turn.capability_limited`，只有通用提示，不包含无权资源元信息 |
| 原始推理 | `yuxi.session.turn.reasoning.delta/done`，关联对应 message item，不表达 reasoning summary |
| 人工等待 | `yuxi.session.turn.waiting`，等待点绑定真实 Turn/Run |
| 执行段、状态、恢复 | `yuxi.session.run.*`、`yuxi.session.turn.state`、`yuxi.session.turn.context_compression`、`yuxi.session.resync` |

客户端按 `item_id`、`output_index`、`content_index` 合并，delta 追加、done 完整值替换；终态 item 拒绝迟到的增量。只有 Turn 的 `result_run_id` 对应输出使用 `phase=final_answer`，其他输出为 commentary。工具不会因名称被伪装为 OpenAI 托管搜索、命令或 MCP，也不生成上游未提供的参数增量。

活动 message 的 `yuxi.completed_content_indices` 与 `completed_reasoning_indices` 表示已持久化的块完成边界。resync 时用这些块的完整值补回漏收的 done，保留其他块尚未持久化的本地增量，忽略已完成块的迟到 delta，并清除旧展示平滑缓存。取消时由当前有效执行 Owner 保存已展示的部分正文，随后以 incomplete item 回读。

`Last-Event-ID` 使用 v2 cursor 续订。Redis 增量过期时发送 `yuxi.session.resync`，客户端缓冲新事件、重读 items 和快照，再按稳定 ID 合并。已完成正文与工具结果先持久化再发送完成边界；进行中的内容依赖短期 Redis 重放，不承诺逐 token 落库。

协作成员拥有独立 Thread、Turn、Run、等待点和结果，全部使用相同公开协议。父轮次完成、失败或取消均保留后代工作；普通取消固定指定 Turn。“停止全部”通过 POST `/threads/{thread_id}/events` 提交 `yuxi.session.tree.stop`，停止所有成员在途 Turn 与队列消费；`yuxi.session.tree.continue` 在在途工作收敛后重新消费保留队列，已取消轮次不恢复。这两种控制输入本身不作为 SSE 输出事件发布，结果通过持久树状态和各 Turn 观察。整树共享实际沙盒与 Project Workdir，各 Run 的 lease 和 heartbeat 独立。工具与用户操作见[会话协作](../agents/session-cooperation.md)。

该协议使用 business schema v5、Redis 事件格式 v2 和 cursor v2，只支持全新数据初始化。没有旧数据迁移、旧事件 reader 或双格式消费。
