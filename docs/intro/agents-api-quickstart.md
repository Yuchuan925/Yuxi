# 运行一次公开 Agent 对话

本教程供外部应用开发者使用 cURL 完成创建、接收、读取结果和继续对话。先由管理员配置可用聊天模型与 Agent，并创建绑定 APP 的 `agents` API Key；身份和权限规则见[API Key 接入](../advanced/api-key-integration.md)。示例使用 `jq` 读取 JSON。

## 准备调用身份

将部署地址、Key 和 APP 内的终端用户标识放入进程环境变量。以下示例始终携带相同终端身份。

```bash
export BASE_URL='https://yuxi.example.com'
export API_KEY='<api-key>'
export END_USER_ID='integration-user-42'
curl --fail-with-body "$BASE_URL/api/v1/agents" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID"
```

确认响应 `data` 中存在要使用的 Agent `id`，将其填入下一步的 `agent_id`。

## 创建并接收初始输入

每个逻辑创建请求生成并保存一个幂等键。网络超时重试时复用同一键和请求体。

```bash
CREATE_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')
CREATED=$(curl --fail-with-body "$BASE_URL/api/v1/agents/sessions" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID" \
  -H "Idempotency-Key: $CREATE_KEY" -H 'Content-Type: application/json' \
  -d '{"agent_id":"default-chatbot","input":[{"role":"user","content":[{"type":"input_text","text":"用一句话介绍你的能力"}]}]}')
SESSION_ID=$(printf '%s' "$CREATED" | jq -r '.id')
INPUT_ID=$(printf '%s' "$CREATED" | jq -r '.input_id')
printf '%s\n' "$CREATED" | jq .
```

观察 HTTP `200`、`object=agent.session` 和非空的 Input ID。接收成功后工作异步执行；排队时创建回执的 `turn_id/run_id` 可为空。

## 定位本轮并读取结果

用 Input 查询消费归属，直到 `turn_id` 非空。下面每次执行读取当前持久状态。

```bash
INPUT=$(curl --fail-with-body "$BASE_URL/api/v1/agents/sessions/$SESSION_ID/inputs/$INPUT_ID" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID")
TURN_ID=$(printf '%s' "$INPUT" | jq -r '.turn_id // empty')
printf '%s\n' "$INPUT" | jq .
```

取得 Turn ID 后，重复读取 Turn，直到状态为 `completed`、`failed` 或 `cancelled`；等待回答或审批时按[等待点协议](../advanced/agents-public-api.md#等待取消与队列)提交结构化响应。

```bash
curl --fail-with-body "$BASE_URL/api/v1/agents/sessions/$SESSION_ID/turns/$TURN_ID" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID" | jq .
curl --fail-with-body "$BASE_URL/api/v1/agents/sessions/$SESSION_ID/history" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID" | jq .
```

`completed` 时检查 Turn 的 `result_run_id` 和结果。Turn 的 `output` 提供本次结果；历史的 `items` 展示持久消息和工具过程，`phase=final_answer` 标识对应 Turn 明确结果的助手正文。

## 继续对话与观察增量

新消息生成新的幂等键。显式 `follow_up` 按 FIFO 排队。

```bash
MESSAGE_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body "$BASE_URL/api/v1/agents/sessions/$SESSION_ID/events" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID" \
  -H "Idempotency-Key: $MESSAGE_KEY" -H 'Content-Type: application/json' \
  -d '{"events":[{"type":"agent.session.input.message","input":[{"role":"user","content":[{"type":"input_text","text":"再详细解释一下"}]}],"yuxi":{"mode":"follow_up"}}]}'
```

HTTP `202` 返回输入回执，复用前面的 Input → Turn 查询步骤读取本次结果。实时展示可在另一个终端、提交前订阅：

```bash
curl --no-buffer --fail-with-body "$BASE_URL/api/v1/agents/sessions/$SESSION_ID/events" \
  -H "Authorization: Bearer $API_KEY" -H "X-End-User-Id: $END_USER_ID"
```

SSE 的 `data` 是事件 JSON，`id` 是恢复 cursor。连接覆盖多轮工作；观察目标 Turn 终态、回读结果后主动关闭。断线重连携带 `Last-Event-ID`；收到 `yuxi.session.resync` 时回读历史和 Session，再按稳定 item ID 合并。完整限制与差异见[公开 API 参考](../advanced/agents-public-api.md)。
